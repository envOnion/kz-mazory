from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.views.decorators.http import require_POST
from .models import WhatsAppConfig
from .access import integration_allowed
from .whatsapp_views import enqueue_control


@staff_member_required
def waha_dashboard_view(request):
    if not integration_allowed(request.user):
        raise PermissionDenied()
    config = WhatsAppConfig.objects.filter(is_active=True).first()
    return render(
        request,
        "admin/waha_dashboard.html",
        {
            "config": config,
            "status": config.status if config else "DISABLED",
            "qr_image": None,
            "me": None,
            "chats": [],
        },
    )


@staff_member_required
@require_POST
def waha_action_view(request, action):
    if not integration_allowed(request.user) or action not in (
        "status",
        "qr",
        "restart",
    ):
        raise PermissionDenied()
    config = get_object_or_404(
        WhatsAppConfig, pk=request.POST.get("config_id"), is_active=True
    )
    enqueue_control(request.user, config, action)
    messages.success(
        request, "Операция поставлена в очередь. Статус обновится после выполнения."
    )
    return redirect("admin:waha_dashboard")
