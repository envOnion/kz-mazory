from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from django.db import transaction
from rest_framework import serializers
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from .models import UserProfile
from .datamart import datamart
from .avatars import delete_avatar, normalize_avatar, store_avatar

EDITABLE = (
    "full_name",
    "email",
    "timezone",
    "notification_preferences",
    "whatsapp_daily_digest",
    "whatsapp_stalled_deals",
    "whatsapp_critical_kpi",
    "ai_response_mode",
    "ai_auto_suggest_next_actions",
)


class ProfileUpdate(serializers.ModelSerializer):
    avatar = serializers.FileField(required=False, write_only=True)
    remove_avatar = serializers.BooleanField(required=False, write_only=True)

    class Meta:
        model = UserProfile
        fields = (*EDITABLE, "avatar", "remove_avatar")

    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
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

    def validate_avatar(self, value):
        return normalize_avatar(value)

    def validate(self, attrs):
        if "avatar" in attrs and attrs.get("remove_avatar"):
            raise serializers.ValidationError(
                {"avatar": "Нельзя одновременно загрузить и удалить фото."}
            )
        return attrs

    def update(self, instance, validated_data):
        content = validated_data.pop("avatar", None)
        remove = validated_data.pop("remove_avatar", False)
        previous = instance.avatar_url
        new_url = ""
        try:
            if content is not None:
                new_url = store_avatar(content)
                validated_data["avatar_url"] = new_url
            elif remove:
                validated_data["avatar_url"] = ""
            updated = super().update(instance, validated_data)
        except Exception:
            if new_url:
                delete_avatar(new_url)
            raise
        if previous and previous != updated.avatar_url:
            transaction.on_commit(lambda: delete_avatar(previous))
        return updated

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
            "avatar_url": profile.avatar_url,
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
    parser_classes = [JSONParser, MultiPartParser]

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
        new_url = ""
        previous = profile.avatar_url
        try:
            with transaction.atomic():
                profile = UserProfile.objects.select_for_update().get(pk=profile.pk)
                previous = profile.avatar_url
                schema = ProfileUpdate(profile, data=request.data, partial=True)
                schema.is_valid(raise_exception=True)
                schema.save()
                new_url = profile.avatar_url if profile.avatar_url != previous else ""
        except Exception:
            if new_url:
                delete_avatar(new_url)
            raise
        return Response(profile_data(request.user, profile))

    patch = put
