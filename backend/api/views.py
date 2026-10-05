import hashlib
import hmac
from datetime import datetime, timedelta, timezone as dt_timezone
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, Throttled, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.throttling import UserRateThrottle
from . import access
from .models import (
    RawMessage,
    WhatsAppConfig,
    OutboxEvent,
    AsyncOperation,
    NotificationDelivery,
    BitrixSettings,
    Project,
    FactCandidate,
    AuditEvent,
)
from .datamart import datamart, project_row, scoped_projects
from .security import Conflict, Unavailable
from .project_workspace import ProjectFilters, workspace_projects, workspace_rows


class Filters(serializers.Serializer):
    period = serializers.ChoiceField(
        choices=["this_month", "last_month", "quarter", "year"], default="this_month"
    )
    team_id = serializers.IntegerField(min_value=1, required=False)
    manager_id = serializers.IntegerField(min_value=1, required=False)
    project_id = serializers.IntegerField(min_value=1, required=False)
    currency = serializers.ChoiceField(
        choices=["KZT", "USD", "EUR", "RUB"], default="KZT"
    )


def filters_for(request):
    schema = Filters(data=request.query_params)
    schema.is_valid(raise_exception=True)
    return schema.validated_data


class KpiSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if access.is_client(request.user):
            raise PermissionDenied()
        filters = filters_for(request)
        return Response(
            datamart.get_sales_kpi_mart(request.user, filters["period"], filters)
        )


class ChatInput(Filters):
    prompt = serializers.CharField(max_length=4000)
    idempotency_key = serializers.CharField(max_length=64)


def create_operation(user, values, kind="chat"):
    if (
        AsyncOperation.objects.filter(
            requested_by=user, status__in=["queued", "running"]
        ).count()
        >= 5
    ):
        raise Throttled(wait=15)
    with transaction.atomic():
        op, created = AsyncOperation.objects.get_or_create(
            requested_by=user,
            idempotency_key=values["idempotency_key"],
            defaults={
                "request": values,
                "operation_type": kind,
                "expires_at": timezone.now() + timedelta(hours=1),
                "access_fingerprint": access.fingerprint(user),
            },
        )
        if op.request != values or op.operation_type != kind:
            raise Conflict("Этот ключ уже использован для другого запроса.")
        if created:
            OutboxEvent.objects.create(
                event_type="operation",
                deduplication_key=f"operation:{op.id}",
                payload={"operation_id": op.id},
            )
    return op


class ChatQueryView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if access.is_client(request.user):
            raise PermissionDenied(
                "Клиенту доступны только опубликованные материалы в кабинете."
            )
        schema = ChatInput(data=request.data)
        schema.is_valid(raise_exception=True)
        op = create_operation(request.user, schema.validated_data)
        return Response(
            {
                "operation_id": op.id,
                "status": op.status,
                "status_url": f"/api/operations/{op.id}/",
            },
            status=202,
        )


class OperationView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        op = get_object_or_404(AsyncOperation, pk=pk, requested_by=request.user)
        if (
            op.expires_at <= timezone.now()
            or op.access_fingerprint != access.fingerprint(request.user)
        ):
            op.status, op.result, op.error_code = (
                "expired",
                {},
                "access_or_lifetime_changed",
            )
            op.save(update_fields=["status", "result", "error_code"])
        return Response(
            {
                "id": op.id,
                "status": op.status,
                "result": op.result if op.status == "succeeded" else None,
                "error_code": op.error_code,
                "expires_at": op.expires_at,
            }
        )

    def delete(self, request, pk):
        op = get_object_or_404(AsyncOperation, pk=pk, requested_by=request.user)
        AsyncOperation.objects.filter(
            pk=op.id, status__in=["queued", "running"]
        ).update(status="cancelled", result={})
        return Response(status=204)


class ProjectListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if access.is_client(request.user):
            return Response(
                {
                    "results": list(
                        access.projects_for(request.user, include_client=True)
                        .filter(is_verified=True)
                        .values("id", "name", "status", "version")
                    )
                }
            )
        schema = ProjectFilters(data=request.query_params)
        schema.is_valid(raise_exception=True)
        qs, counts = workspace_projects(request.user, schema.validated_data)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(
            qs.select_related("company", "manager"), request
        )
        response = paginator.get_paginated_response(workspace_rows(request.user, page))
        response.data['group_counts'] = counts
        return response


class ProjectVerifyView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        get_object_or_404(access.projects_for(request.user), pk=pk)
        raise Conflict("Подтвердите конкретное предложение на экране проверки фактов.")


class SourceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        raw = get_object_or_404(access.messages_for(request.user), pk=pk)
        return Response(
            {
                "id": raw.id,
                "content": raw.content,
                "sender_name": raw.sender_name,
                "sent_at": raw.timestamp if raw.sent_at_known else None,
                "received_at": raw.received_at,
                "processing_state": raw.processing_state,
                "revision": raw.source_revision,
            }
        )

    def post(self, request, pk):
        raw = get_object_or_404(access.messages_for(request.user), pk=pk)
        access.require_team_role(request.user, raw.config.team_id, ["team_lead"])
        with transaction.atomic():
            raw = RawMessage.objects.select_for_update().get(pk=raw.id)
            if raw.processing_state != "failed":
                raise Conflict("Повтор доступен только после ошибки обработки.")
            raw.processing_state = "received"
            raw.save(update_fields=["processing_state"])
            event = get_object_or_404(
                OutboxEvent, deduplication_key=f"extract:{raw.id}"
            )
            if event.state in ("processing", "enqueued"):
                raise Conflict("Обработка ещё выполняется.")
            event.state, event.attempt_count, event.next_attempt_at = (
                "pending",
                0,
                timezone.now(),
            )
            event.save()
            AuditEvent.objects.create(
                actor=request.user,
                target_type="RawMessage",
                target_id=raw.id,
                action="retry",
            )
        return Response({"status": "queued"}, status=202)


def verify_waha(request):
    secret = settings.WAHA_WEBHOOK_SECRET
    if not secret:
        raise Unavailable("Приём вебхуков выключен.")
    signature = request.headers.get("X-Webhook-Hmac", "")
    if not signature or not hmac.compare_digest(
        signature, hmac.new(secret.encode(), request.body, hashlib.sha512).hexdigest()
    ):
        raise PermissionDenied("Подпись вебхука недействительна.")


class WebhookPayload(serializers.Serializer):
    id = serializers.CharField(max_length=128)
    body = serializers.CharField(
        max_length=32000,
        required=False,
        allow_blank=True,
        default="",
        trim_whitespace=False,
    )
    timestamp = serializers.IntegerField(min_value=0, required=False)
    fromMe = serializers.BooleanField(default=False)
    participant = serializers.CharField(
        max_length=128, required=False, allow_blank=True, allow_null=True, default=""
    )
    notifyName = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )
    hasMedia = serializers.BooleanField(default=False)
    media = serializers.DictField(required=False, allow_null=True)
    replyTo = serializers.DictField(required=False, allow_null=True)
    ack = serializers.IntegerField(min_value=-1, max_value=4, required=False)


class WebhookInput(serializers.Serializer):
    event = serializers.ChoiceField(
        choices=["message", "message.any", "message.edited", "message.ack"]
    )
    session = serializers.RegexField(r"^[A-Za-z0-9_-]{1,64}$")
    payload = WebhookPayload()

    def validate(self, data):
        original = self.initial_data.get("payload", {})
        if data["event"] == "message.ack":
            if "ack" not in data["payload"]:
                raise ValidationError("Не указан статус доставки.")
            return data
        chat = original.get("to", "") if data["payload"]["fromMe"] else original.get("from", "")
        if not isinstance(chat, str) or not chat or len(chat) > 128:
            raise ValidationError("Не указан чат.")
        data["chat_id"] = chat
        own_sender = original.get("from", "") if data["payload"]["fromMe"] else ""
        if isinstance(own_sender, str) and own_sender.endswith(("@c.us", "@s.whatsapp.net")):
            data["payload"]["participant"] = data["payload"]["participant"] or own_sender
        return data


class WebhookThrottle(UserRateThrottle):
    rate = "2400/min"
    scope = "webhook"


class MessageIngestView(APIView):
    throttle_classes = [WebhookThrottle]
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        verify_waha(request)
        schema = WebhookInput(data=request.data)
        schema.is_valid(raise_exception=True)
        data = schema.validated_data
        payload = data["payload"]
        if data["event"] == "message.ack":
            if data["session"] != "default" or not payload["fromMe"]:
                return Response({"status": "ignored"})
            digest = hashlib.sha256(payload["id"].encode()).hexdigest()
            OutboxEvent.objects.get_or_create(
                deduplication_key=f"delivery_ack:{data['session']}:{digest}:{payload['ack']}",
                defaults={
                    "event_type": "delivery_ack",
                    "payload": {
                        "message_id": payload["id"],
                        "ack": payload["ack"],
                        "session": data["session"],
                    },
                },
            )
            return Response({"status": "accepted"}, status=202)
        cfg = WhatsAppConfig.objects.filter(
            session_name=data["session"],
            group_jid=data["chat_id"],
            is_active=True,
            team__is_active=True,
        ).first()
        if not cfg:
            raise PermissionDenied("Источник не разрешён.")
        payload = data["payload"]
        if NotificationDelivery.objects.filter(provider_message_id=payload["id"]).exclude(provider_message_id="").exists():
            return Response({"status": "ignored"})
        if not payload["body"].strip() and not payload["hasMedia"]:
            return Response({"status": "ignored"})
        stamp = payload.get("timestamp")
        try:
            sent = (
                datetime.fromtimestamp(stamp, dt_timezone.utc)
                if stamp
                else timezone.now()
            )
        except (ValueError, OverflowError, OSError):
            raise ValidationError("Некорректное время сообщения.")
        if sent > timezone.now() + timedelta(minutes=5):
            raise ValidationError("Время сообщения находится в будущем.")
        revision = hashlib.sha256(payload["body"].encode()).hexdigest()
        sender = (payload["participant"] or "").split("@")[0]
        with transaction.atomic():
            from .models import (
                WhatsAppHistoryJob,
                WhatsAppHistoryRun,
                HISTORY_ACTIVE_STATES,
            )

            job = (
                WhatsAppHistoryJob.objects.select_for_update()
                .filter(config_id=cfg.pk)
                .first()
            )
            cfg = WhatsAppConfig.objects.select_for_update().get(pk=cfg.pk)
            if (
                not cfg.is_active
                or not cfg.team_id
                or not cfg.team.is_active
                or cfg.session_name != data["session"]
                or cfg.group_jid != data["chat_id"]
            ):
                raise PermissionDenied("Источник изменился или выключен.")
            raw, created = RawMessage.objects.get_or_create(
                source="waha",
                session_name=data["session"],
                message_id=payload["id"],
                source_revision=revision,
                defaults={
                    "config": cfg,
                    "team_id": cfg.team_id,
                    "chat_id": data["chat_id"],
                    "sender_phone": sender,
                    "sender_name": payload["notifyName"],
                    "timestamp": sent,
                    "sent_at_known": bool(stamp),
                    "content": payload["body"],
                    "raw_payload": {
                        "event": data["event"],
                        "session": data["session"],
                        "payload": payload,
                    },
                },
            )
            from .message_artifacts import register
            register(raw)
            if raw.config_id != cfg.id:
                raise Conflict("Идентификатор источника уже связан с другим чатом.")
            analyze = not job or not job.only_new or job.analyze_after_import
            if created and analyze:
                OutboxEvent.objects.create(
                    event_type="extract_message",
                    deduplication_key=f"extract:{raw.id}",
                    payload={"raw_id": raw.id},
                )
            if created and job and job.only_new:
                run = (
                    WhatsAppHistoryRun.objects.select_for_update()
                    .filter(job=job, state__in=HISTORY_ACTIVE_STATES)
                    .first()
                )
                if run:
                    run.messages.add(raw)
                    run.imported_count += 1
                    run.fetched_count += 1
                    run.scheduled_count += int(analyze)
                    run.save(
                        update_fields=[
                            "imported_count",
                            "fetched_count",
                            "scheduled_count",
                        ]
                    )
        return Response(
            {
                "status": ("queued" if analyze else "saved")
                if created
                else "duplicate",
                "source_id": raw.id,
            },
            status=202 if created else 200,
        )


class BitrixWebhookView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        body = request.body
        expected = settings.BITRIX_INBOUND_TOKEN
        if not expected or not BitrixSettings.objects.filter(is_active=True).exists():
            raise Unavailable("Интеграция выключена.")
        incoming = request.headers.get("X-Bitrix-Token", "") or request.data.get(
            "auth[application_token]", ""
        )
        if not incoming and isinstance(request.data.get("auth"), dict):
            incoming = request.data["auth"].get("application_token", "")
        if not isinstance(incoming, str) or not hmac.compare_digest(incoming, expected):
            raise PermissionDenied()
        event = request.data.get("event")
        deal_id = request.data.get("data[FIELDS][ID]")
        if not deal_id and isinstance(request.data.get("data"), dict):
            deal_id = request.data["data"].get("FIELDS", {}).get("ID")
        if (
            event not in ("ONCRMDEALADD", "ONCRMDEALUPDATE")
            or not str(deal_id).isdigit()
        ):
            raise ValidationError("Неподдерживаемое событие CRM.")
        event_key = hashlib.sha256(body).hexdigest()
        outbox, _ = OutboxEvent.objects.get_or_create(
            deduplication_key=f"crm_in:{event_key}",
            defaults={
                "event_type": "crm_import",
                "payload": {"deal_id": str(deal_id), "event_key": event_key},
            },
        )
        return Response({"status": outbox.state}, status=202)
