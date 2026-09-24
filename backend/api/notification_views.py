from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django_q.tasks import async_task
from .notifications import fetch_user_notifications, mark_all_notifications_as_read

class NotificationListView(APIView):
    """
    Returns real targeted notifications for the authenticated user only.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        phone = request.user.username
        notifications, unread_count = fetch_user_notifications(phone)
        return Response({
            "status": "success",
            "phone": phone,
            "unread_count": unread_count,
            "notifications": notifications
        })

class NotificationMarkAllReadView(APIView):
    """
    Marks all notifications for the authenticated user as read.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        phone = request.user.username
        updated = mark_all_notifications_as_read(phone)
        return Response({
            "status": "success",
            "message": "Все уведомления помечены как прочитанные",
            "unread_count": 0,
            "notifications": updated
        })

class DispatchNotificationView(APIView):
    """
    Targeted business notification dispatcher.
    Sends notifications to specific users (single phone, list of phones, or all company users).
    Enqueues delivery via Django Q and optionally sends WhatsApp message via WAHA.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        target_phones = request.data.get("phones") or request.data.get("phone")
        title = request.data.get("title", "").strip()
        message = request.data.get("message", "").strip()
        notif_type = request.data.get("type", "info")
        send_whatsapp = bool(request.data.get("send_whatsapp", False))

        if not title or not message:
            return Response(
                {"error": "Поля 'title' (заголовок) и 'message' (сообщение) обязательны"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if isinstance(target_phones, str):
            if target_phones.lower() == "all":
                from django.contrib.auth.models import User
                phones_list = list(User.objects.values_list('username', flat=True))
            else:
                phones_list = [target_phones]
        elif isinstance(target_phones, list):
            phones_list = target_phones
        else:
            phones_list = [request.user.username]

        if not phones_list:
            phones_list = [request.user.username]

        async_task(
            'api.tasks.dispatch_targeted_notification_task',
            phones_list,
            title,
            message,
            notif_type,
            send_whatsapp,
            request.user.username
        )

        return Response({
            "status": "queued",
            "message": f"Адресная рассылка отправлена в очередь Django Q для {len(phones_list)} получателей",
            "targets": phones_list,
            "send_whatsapp": send_whatsapp
        })

