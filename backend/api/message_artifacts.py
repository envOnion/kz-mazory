"""WhatsApp media provenance; configured recognizers are optional resources."""

import hashlib
import time
import requests
from django.conf import settings
from django.db import transaction
from .models import MessageArtifact, OutboxEvent, RawMessage
from .providers import checked_url, ProviderUnavailable


def register(raw):
    payload = raw.raw_payload.get("payload", {})
    if not isinstance(payload, dict) or not payload.get("hasMedia"):
        return
    media = payload.get("media")
    media = media if isinstance(media, dict) else {}
    url = media.get("url")
    artifact, _ = MessageArtifact.objects.get_or_create(raw_message=raw)
    if artifact.state == "ready":
        return
    if not isinstance(url, str) or not url:
        artifact.state, artifact.error_code = "unavailable", "waha_media_not_downloaded"
        artifact.save(update_fields=["state", "error_code"])
        return
    OutboxEvent.objects.get_or_create(deduplication_key=f"whatsapp-artifact:{artifact.id}", defaults={"event_type": "whatsapp_artifact", "payload": {"artifact_id": artifact.id}})


def process(artifact_id):
    from .tasks import waha_download_media
    from .message_time import EXPORT_HEADER
    from .attachments import ALLOWED
    artifact = MessageArtifact.objects.select_related("raw_message__config").get(pk=artifact_id)
    if artifact.state == "ready":
        return
    raw = artifact.raw_message
    media = raw.raw_payload.get("payload", {}).get("media", {})
    mime = str(media.get("mimetype", "")).split(";")[0]
    provider = settings.TRANSCRIPTION_URL if mime.startswith("audio/") else settings.OCR_URL
    if mime not in ALLOWED or not provider:
        artifact.state, artifact.error_code = "unavailable", "recognizer_not_configured" if not provider else "unsupported_media_type"
        artifact.save(update_fields=["state", "error_code"])
        return
    try:
        content = waha_download_media(media.get("url"))
        if not content.startswith(ALLOWED[mime]):
            raise ProviderUnavailable("media_type_mismatch")
        checksum = hashlib.sha256(content).hexdigest()
        cached = MessageArtifact.objects.filter(checksum=checksum, state="ready", raw_message__config_id=raw.config_id).exclude(pk=artifact.pk).first()
        if cached:
            text = cached.extracted_text
        else:
            text = recognize(content, mime, provider)
        if not isinstance(text, str) or not text.strip() or len(text) > 32000:
            raise ProviderUnavailable("invalid_transcript")
        header = EXPORT_HEADER.match(raw.content)
        transcript = (header.group() if header else "") + text
        revision = hashlib.sha256((checksum + raw.source_revision + transcript).encode()).hexdigest()
        with transaction.atomic():
            artifact.checksum, artifact.extracted_text, artifact.state, artifact.error_code = checksum, text, "ready", ""
            artifact.save()
            derived, _ = RawMessage.objects.get_or_create(source="waha", session_name=raw.session_name, message_id="media:" + hashlib.sha256((str(raw.config_id) + raw.message_id).encode()).hexdigest(), source_revision=revision, defaults={"team_id": raw.team_id, "config_id": raw.config_id, "chat_id": raw.chat_id, "project_id": raw.project_id, "sender_name": raw.sender_name, "sender_phone": raw.sender_phone, "timestamp": raw.timestamp, "sent_at_known": raw.sent_at_known, "content": transcript, "raw_payload": {"artifact_id": artifact.id, "original_raw_id": raw.id, "recognition_precision": "unknown"}})
            OutboxEvent.objects.get_or_create(deduplication_key=f"extract:{derived.id}", defaults={"event_type": "extract_message", "payload": {"raw_id": derived.id, "priority": "live"}})
    except Exception as exc:
        artifact.state, artifact.error_code = "failed", str(exc)[:64] if isinstance(exc, ProviderUnavailable) else type(exc).__name__
        artifact.save(update_fields=["state", "error_code"])
        raise


def recognize(content, mime, provider):
    from .models import AISettings, ProviderUsage
    from .provider_reservations import reserve
    from .ai_service import usage_event_id
    operation = "transcription" if mime.startswith("audio/") else "ocr"
    reservation = reserve(AISettings.get_active(), {"media_bytes": len(content), "mime": mime}, operation)
    started, error = time.monotonic(), ""
    try:
        response = requests.post(checked_url(provider), files={"file": ("whatsapp-media", content, mime)}, headers={"Authorization": f"Bearer {settings.MEDIA_PROVIDER_KEY}"}, timeout=45, allow_redirects=False)
        response.raise_for_status()
        text = response.json().get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 32000:
            raise ProviderUnavailable("invalid_transcript")
        return text
    except Exception as exc:
        error = str(exc)[:64] if isinstance(exc, ProviderUnavailable) else type(exc).__name__
        raise
    finally:
        usage = ProviderUsage.objects.create(outbox_event_id=usage_event_id.get(), operation=operation, model_name="configured-recognizer", duration_ms=round((time.monotonic() - started) * 1000), succeeded=not error, error_code=error)
        if reservation:
            reservation.state, reservation.usage = "settled", usage
            reservation.save(update_fields=["state", "usage"])
