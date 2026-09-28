import uuid
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from .notifications import (
    visible_notifications,
    serialize_notification,
    create_notification,
)
from .models import Project, AuditEvent
from . import access


class DispatchInput(serializers.Serializer):
    recipient_id = serializers.IntegerField(min_value=1)
    title = serializers.CharField(max_length=255)
    message = serializers.CharField(max_length=4000)
    project_id = serializers.IntegerField(min_value=1, required=False)
    type = serializers.ChoiceField(
        choices=["info", "warning", "urgent", "deal", "kpi"], default="info"
    )
    send_whatsapp = serializers.BooleanField(default=False)
    idempotency_key = serializers.CharField(max_length=64)


def dispatch(request, force_whatsapp=False):
    data = DispatchInput(data=request.data)
    data.is_valid(raise_exception=True)
    values = data.validated_data
    recipient = get_object_or_404(User, id=values["recipient_id"], is_active=True)
    project = (
        get_object_or_404(access.projects_for(request.user), pk=values["project_id"])
        if values.get("project_id")
        else None
    )
    allowed = (
        request.user.is_superuser
        or access.memberships(recipient)
        .filter(team_id__in=access.team_ids(request.user, ["team_lead", "finance"]))
        .exists()
    )
    if not allowed or not access.has_access(recipient):
        raise PermissionDenied("Нет права отправки выбранному получателю.")
    if project and not access.projects_for(recipient).filter(pk=project.pk).exists():
        raise PermissionDenied("Получателю недоступен проект.")
    with transaction.atomic():
        n = create_notification(
            recipient,
            values["title"],
            values["message"],
            values["type"],
            f"manual:{request.user.id}:{values['idempotency_key']}",
            project=project,
            whatsapp=force_whatsapp or values["send_whatsapp"],
        )
        AuditEvent.objects.get_or_create(
            actor=request.user,
            target_type="Notification",
            target_id=n.id,
            action="dispatch",
            defaults={"before_after": {"recipient_id": recipient.id}},
        )
    return Response({"notification_id": n.id, "status": "queued"}, status=202)


class NotificationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from rest_framework.pagination import PageNumberPagination

        pagination = PageNumberPagination()
        pagination.page_size = 50
        qs = visible_notifications(request.user).prefetch_related("deliveries")
        page = pagination.paginate_queryset(qs, request)
        return Response(
            {
                "notifications": [serialize_notification(n) for n in page],
                "count": qs.count(),
                "next": pagination.get_next_link(),
                "unread_count": qs.filter(read_at__isnull=True).count(),
            }
        )


class NotificationMarkAllReadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        visible_notifications(request.user).filter(read_at__isnull=True).update(
            read_at=timezone.now()
        )
        return Response({"unread_count": 0})


class NotificationActionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk, action):
        if action not in ("read", "ack"):
            raise serializers.ValidationError("Неизвестное действие.")
        n = get_object_or_404(visible_notifications(request.user), pk=pk)
        n.read_at = n.read_at or timezone.now()
        if action == "ack":
            n.acknowledged_at = n.acknowledged_at or timezone.now()
        n.save()
        return Response(serialize_notification(n))


class DispatchNotificationView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        return dispatch(request)
