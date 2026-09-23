from django.urls import path
from .views import KpiSummaryView, ChatQueryView
from .auth_views import (
    SendVerificationCodeView,
    VerifyCodeView,
    CurrentUserView,
    SendWhatsAppView
)

urlpatterns = [
    # Auth
    path('auth/send-code/', SendVerificationCodeView.as_view(), name='auth-send-code'),
    path('auth/verify-code/', VerifyCodeView.as_view(), name='auth-verify-code'),
    path('auth/me/', CurrentUserView.as_view(), name='auth-me'),
    
    # WhatsApp (WAHA)
    path('whatsapp/send/', SendWhatsAppView.as_view(), name='whatsapp-send'),
    
    # Dashboard & Chat
    path('kpi/summary/', KpiSummaryView.as_view(), name='kpi-summary'),
    path('chat/query/', ChatQueryView.as_view(), name='chat-query'),
]
