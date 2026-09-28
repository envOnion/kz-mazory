from django.core.checks import register, Warning
from django.conf import settings


@register(deploy=True)
def integration_security(app_configs, **kwargs):
    if settings.MAZORY_ENV != "production":
        return []
    return [
        Warning(f"{name} отсутствует: webhook отключен.", id="mazory.W001")
        for name in ("WAHA_API_KEY", "BITRIX_INBOUND_TOKEN")
        if not getattr(settings, name, "")
    ]
