import hashlib
import uuid
import requests
from django.conf import settings
from django.http import FileResponse
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from .models import PrivateAttachment, OutboxEvent, RawMessage, AuditEvent
from . import access
from .providers import checked_url, ProviderUnavailable

ALLOWED = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
    "application/pdf": b"%PDF-",
    "audio/ogg": b"OggS",
    "audio/wav": b"RIFF",
    "audio/mpeg": b"ID3",
}


class AttachmentView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = PrivateAttachment.objects.filter(
            project__in=access.projects_for(request.user, include_client=True)
        )
        if access.is_client(request.user):
            qs = qs.filter(published_to_client=True)
        return Response(
            list(
                qs.order_by("-id").values(
                    "id",
                    "project_id",
                    "content_type",
                    "state",
                    "published_to_client",
                    "created_at",
                )[:100]
            )
        )

    def post(self, request):
        if access.is_client(request.user):
            raise PermissionDenied()
        project = get_object_or_404(
            access.projects_for(request.user),
            pk=request.data.get("project_id"),
            is_verified=True,
        )
        file = request.FILES.get("file")
        if not file or file.size > settings.MAX_ATTACHMENT_SIZE:
            raise ValidationError("Файл отсутствует или превышает 10 МБ.")
        if file.content_type not in ALLOWED or not file.read(16).startswith(
            ALLOWED.get(file.content_type, b"UNSUPPORTED")
        ):
            raise ValidationError(
                "Поддерживаются PNG, JPEG, PDF, OGG, WAV и MP3 с ID3. Тип и содержимое должны совпадать."
            )
        file.seek(0)
        digest = hashlib.sha256()
        for chunk in file.chunks():
            digest.update(chunk)
        file.seek(0)
        file.name = uuid.uuid4().hex
        with transaction.atomic():
            item = PrivateAttachment.objects.create(
                project=project,
                uploaded_by=request.user,
                file=file,
                content_type=file.content_type,
                sha256=digest.hexdigest(),
            )
            OutboxEvent.objects.create(
                event_type="attachment",
                deduplication_key=f"attachment:{item.id}",
                payload={"attachment_id": item.id},
            )
        return Response({"id": item.id, "state": item.state}, status=202)


class AttachmentDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        qs = PrivateAttachment.objects.filter(
            project__in=access.projects_for(request.user, include_client=True)
        )
        if access.is_client(request.user):
            qs = qs.filter(published_to_client=True)
        item = get_object_or_404(qs, pk=pk)
        if request.query_params.get("download") == "1":
            response = FileResponse(
                item.file.open("rb"),
                as_attachment=True,
                filename=f"document-{item.id}",
                content_type="application/octet-stream",
            )
            response["X-Content-Type-Options"] = "nosniff"
            response["Cache-Control"] = "private, no-store"
            return response
        return Response(
            {
                "id": item.id,
                "state": item.state,
                "transcript": item.transcript,
                "published_to_client": item.published_to_client,
            }
        )

    def post(self, request, pk):
        item = get_object_or_404(
            PrivateAttachment, pk=pk, project__in=access.projects_for(request.user)
        )
        access.require_team_role(request.user, item.project.team_id, ["team_lead"])
        value = request.data.get("published_to_client")
        if not isinstance(value, bool):
            raise ValidationError("Укажите published_to_client: true/false.")
        item.published_to_client = value
        item.save(update_fields=["published_to_client"])
        AuditEvent.objects.create(
            actor=request.user,
            target_type="PrivateAttachment",
            target_id=item.id,
            action="publish" if value else "unpublish",
        )
        return Response({"id": item.id, "published_to_client": value})


def extract_attachment(pk):
    item = PrivateAttachment.objects.select_related("project").get(pk=pk)
    if item.state == "ready":
        return
    url = (
        settings.TRANSCRIPTION_URL
        if item.content_type.startswith("audio/")
        else settings.OCR_URL
    )
    if not url:
        PrivateAttachment.objects.filter(pk=pk).update(state="unavailable")
        return
    try:
        with item.file.open("rb") as file:
            response = requests.post(
                checked_url(url),
                files={"file": ("document", file, item.content_type)},
                timeout=45,
                allow_redirects=False,
                headers={"Authorization": f"Bearer {settings.MEDIA_PROVIDER_KEY}"},
            )
        response.raise_for_status()
        text = response.json().get("text")
        if not isinstance(text, str) or len(text) > 32000:
            raise ProviderUnavailable("invalid_transcript")
        with transaction.atomic():
            item.transcript, item.state = text, "ready"
            item.save(update_fields=["transcript", "state"])
            # Transcript is untrusted evidence, never an automatically approved business fact.
            raw, _ = RawMessage.objects.get_or_create(
                source="attachment",
                session_name="private",
                message_id=str(item.id),
                source_revision=item.sha256,
                defaults={
                    "team": item.project.team,
                    "project": item.project,
                    "timestamp": item.created_at,
                    "sent_at_known": False,
                    "content": text,
                    "processing_state": "transcribed",
                },
            )
            OutboxEvent.objects.get_or_create(
                event_type="extract_message",
                deduplication_key=f"extract:{raw.id}",
                defaults={"payload": {"raw_id": raw.id}},
            )
    except Exception:
        PrivateAttachment.objects.filter(pk=pk).update(state="failed")
        raise
