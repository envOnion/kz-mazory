from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from django.utils import timezone
from rest_framework import serializers
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from .models import UserProfile
from .datamart import datamart

EDITABLE = (
    "full_name",
    "email",
    "avatar_url",
    "timezone",
    "notification_preferences",
    "whatsapp_daily_digest",
    "whatsapp_stalled_deals",
    "whatsapp_critical_kpi",
    "ai_response_mode",
    "ai_auto_suggest_next_actions",
)


class ProfileUpdate(serializers.ModelSerializer):
    class Meta:
        model = UserProfile
        fields = EDITABLE

    def to_internal_value(self, data):
        unknown = set(data) - set(EDITABLE)
        if unknown:
            raise serializers.ValidationError(
                {key: "Поле доступно только для чтения." for key in sorted(unknown)}
            )
        return super().to_internal_value(data)

    def validate_timezone(self, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise serializers.ValidationError("Неизвестный часовой пояс.")
        return value

    def validate_avatar_url(self, value):
        # No arbitrary external image tracking or active URL schemes.
        if value and not value.startswith("/avatars/"):
            raise serializers.ValidationError("Используйте локальный аватар.")
        return value

    def validate_notification_preferences(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Нужен объект настроек.")
        allowed = {
            "quiet_start",
            "quiet_end",
            "digest_time",
            "whatsapp",
            "reminder",
            "daily_digest",
            "stalled_deals",
            "critical_kpi",
            "info",
            "warning",
            "urgent",
            "deal",
            "kpi",
            "help",
        }
        if set(value) - allowed:
            raise serializers.ValidationError("Неизвестная настройка уведомлений.")
        for key, val in value.items():
            if key in ("quiet_start", "quiet_end", "digest_time"):
                try:
                    if not isinstance(val, str) or len(val) != 5:
                        raise ValueError()
                    time.fromisoformat(val)
                except ValueError:
                    raise serializers.ValidationError("Укажите время HH:MM.")
            elif not isinstance(val, bool):
                raise serializers.ValidationError("Ожидается true или false.")
        return value


def profile_data(user, profile):
    data = {field: getattr(profile, field) for field in EDITABLE}
    data.update(
        {
            "id": profile.id,
            "phone": profile.phone,
            "role": profile.role,
            "department": profile.department,
            "updated_at": profile.updated_at,
        }
    )
    mart = datamart.get_sales_kpi_mart(user)
    row = next((m for m in mart["managers"] if m["id"] == profile.id), None)
    data.update(
        {
            "monthly_target": row["targetAmount"] if row else None,
            "monthly_target_formatted": row["targetFormatted"]
            if row
            else "План не задан",
            "current_sales": row["fact"] if row else "0.00",
            "current_sales_formatted": row["salesAmount"] if row else "0.00 ₸",
            "deals_count": row["dealsCount"] if row else 0,
            "rank_in_team": None,
            "conversion_rate": None,
            "kpi_percent": row["kpiPercent"] if row else None,
            "coverage": mart["coverage"],
        }
    )
    return data


class ProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile, _ = UserProfile.objects.get_or_create(
            user=request.user,
            defaults={
                "phone": request.user.username,
                "full_name": request.user.first_name,
            },
        )
        return Response(profile_data(request.user, profile))

    def put(self, request):
        profile, _ = UserProfile.objects.get_or_create(
            user=request.user, defaults={"phone": request.user.username}
        )
        schema = ProfileUpdate(profile, data=request.data, partial=True)
        schema.is_valid(raise_exception=True)
        schema.save()
        return Response(profile_data(request.user, profile))

    patch = put
