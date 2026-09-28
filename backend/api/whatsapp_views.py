import uuid
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from .models import WhatsAppConfig, OutboxEvent, AuditEvent
from .access import integration_allowed


def config_for(request):
    if not integration_allowed(request.user):
        raise PermissionDenied()
    return get_object_or_404(
        WhatsAppConfig,
        is_active=True,
        pk=request.data.get("config_id") or request.query_params.get("config_id"),
    )


def enqueue_control(user, config, action):
    event = OutboxEvent.objects.create(
        event_type="waha_control",
        deduplication_key=f"waha:{uuid.uuid4().hex}",
        payload={"config_id": config.id, "action": action},
    )
    AuditEvent.objects.create(
        actor=user, target_type="WhatsAppConfig", target_id=config.id, action=action
    )
    return event


class WhatsAppStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        cfg = config_for(request)
        return Response(
            {"id": cfg.id, "status": cfg.status, "updated_at": cfg.updated_at}
        )

    def post(self, request):
        cfg = config_for(request)
        event = enqueue_control(request.user, cfg, "status")
        return Response({"event_id": event.id, "status": "queued"}, status=202)


class WhatsAppQrView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        cfg = config_for(request)
        return Response(
            {
                "status": cfg.status,
                "qr_value": cfg.last_qr_code,
                "updated_at": cfg.updated_at,
            }
        )

    def post(self, request):
        cfg = config_for(request)
        event = enqueue_control(request.user, cfg, "qr")
        return Response({"event_id": event.id, "status": "queued"}, status=202)


class WhatsAppRestartView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        cfg = config_for(request)
        event = enqueue_control(request.user, cfg, "restart")
        return Response({"event_id": event.id, "status": "queued"}, status=202)
