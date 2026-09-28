from django import forms
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from .models import WhatsAppConfig
from .access import integration_allowed
from .waha_control import enqueue_control as queue_control


class ConfigForm(forms.Form):
    config_id = forms.IntegerField(min_value=1, max_value=9223372036854775807)


def config_for(request):
    if not integration_allowed(request.user):
        raise PermissionDenied()
    form = ConfigForm(
        request.data if request.method == "POST" else request.query_params
    )
    if not form.is_valid():
        raise ValidationError(
            {"config_id": "Укажите положительный целочисленный ID конфигурации."}
        )
    return get_object_or_404(
        WhatsAppConfig,
        is_active=True,
        pk=form.cleaned_data["config_id"],
    )


def enqueue_control(user, config, action):
    try:
        return queue_control(user, config, action)
    except DjangoValidationError as exc:
        raise ValidationError({"error": exc.messages[0]}) from exc


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
