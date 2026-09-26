from django.urls import path
from .views import (
    KpiSummaryView,
    ChatQueryView,
    MessageIngestView,
    ProjectListView,
    ProjectVerifyView,
    BitrixWebhookView
)
from .auth_views import (
    SendVerificationCodeView,
    VerifyCodeView,
    CurrentUserView,
    SendWhatsAppView
)
from .profile_views import ProfileView
from .notification_views import (
    NotificationListView,
    NotificationMarkAllReadView,
    DispatchNotificationView
)
from .whatsapp_views import (
    WhatsAppStatusView,
    WhatsAppQrView,
    WhatsAppRestartView
)

urlpatterns = [
    # Auth & Profile
    path('auth/send-code/', SendVerificationCodeView.as_view(), name='auth-send-code'),
    path('auth/verify-code/', VerifyCodeView.as_view(), name='auth-verify-code'),
    path('auth/me/', CurrentUserView.as_view(), name='auth-me'),
    path('profile/', ProfileView.as_view(), name='user-profile'),
    
    # Notifications (Redis + Django Q)
    path('notifications/', NotificationListView.as_view(), name='notifications-list'),
    path('notifications/read-all/', NotificationMarkAllReadView.as_view(), name='notifications-read-all'),
    path('notifications/dispatch/', DispatchNotificationView.as_view(), name='notifications-dispatch'),

    # WhatsApp (WAHA)
    path('whatsapp/send/', SendWhatsAppView.as_view(), name='whatsapp-send'),
    path('whatsapp/status/', WhatsAppStatusView.as_view(), name='whatsapp-status'),
    path('whatsapp/qr/', WhatsAppQrView.as_view(), name='whatsapp-qr'),
    path('whatsapp/restart/', WhatsAppRestartView.as_view(), name='whatsapp-restart'),
    
    # Dashboard & Chat & Projects
    path('kpi/summary/', KpiSummaryView.as_view(), name='kpi-summary'),
    path('projects/', ProjectListView.as_view(), name='project-list'),
    path('projects/<int:pk>/verify/', ProjectVerifyView.as_view(), name='project-verify'),
    path('chat/query/', ChatQueryView.as_view(), name='chat-query'),
    # WhatsApp & Bitrix Webhooks
    path('messages/ingest/', MessageIngestView.as_view(), name='messages-ingest'),
    path('whatsapp/webhook/', MessageIngestView.as_view(), name='whatsapp-webhook'),
    path('bitrix/webhook/', BitrixWebhookView.as_view(), name='bitrix-webhook'),
]
