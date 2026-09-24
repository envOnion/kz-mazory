from rest_framework import serializers
from .models import UserProfile

class UserProfileSerializer(serializers.ModelSerializer):
    kpi_percent = serializers.ReadOnlyField()
    monthly_target_formatted = serializers.SerializerMethodField()
    current_sales_formatted = serializers.SerializerMethodField()

    class Meta:
        model = UserProfile
        fields = [
            'id',
            'full_name',
            'role',
            'department',
            'email',
            'phone',
            'avatar_url',
            'monthly_target',
            'monthly_target_formatted',
            'current_sales',
            'current_sales_formatted',
            'deals_count',
            'rank_in_team',
            'conversion_rate',
            'kpi_percent',
            'whatsapp_daily_digest',
            'whatsapp_stalled_deals',
            'whatsapp_critical_kpi',
            'ai_response_mode',
            'ai_auto_suggest_next_actions',
            'updated_at'
        ]

    def get_monthly_target_formatted(self, obj):
        val = int(obj.monthly_target)
        return f"{val:,}".replace(',', ' ') + " ₽"

    def get_current_sales_formatted(self, obj):
        val = int(obj.current_sales)
        return f"{val:,}".replace(',', ' ') + " ₽"
