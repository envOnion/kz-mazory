from rest_framework import serializers
from .models import UserProfile, Project

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
        return f"{val:,}".replace(',', ' ') + " ₸"

    def get_current_sales_formatted(self, obj):
        val = int(obj.current_sales)
        return f"{val:,}".replace(',', ' ') + " ₸"


class ProjectSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)
    manager_name = serializers.CharField(source='manager.full_name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    profit_amount = serializers.ReadOnlyField()

    class Meta:
        model = Project
        fields = [
            'id',
            'bitrix_id',
            'name',
            'company',
            'company_name',
            'manager',
            'manager_name',
            'project_type',
            'status',
            'status_display',
            'equipment_type',
            'contract_number',
            'deal_period',
            'contract_amount',
            'cost_amount',
            'profit_amount',
            'target_margin_percent',
            'actual_margin_percent',
            'paid_amount',
            'due_amount',
            'guarantee_amount',
            'barter_amount',
            'avr_status',
            'priority',
            'current_action',
            'next_action',
            'next_action_at',
            'decision_maker',
            'blocker',
            'created_at',
            'updated_at'
        ]
