from .admin_access import ScopedReadOnlyAdmin, IntegrationAdmin, SuperuserAdmin
import json
import requests
from django.contrib import admin
from django.utils.html import format_html, format_html_join
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from .admin_forms import BitrixSettingsForm
from .bitrix_config import effective_webhook_url, masked_webhook_url
from .trace_context_ui import badge_text, render_context, context_view_data, ERRORS
from django.utils.safestring import mark_safe
from django.contrib import messages
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action
from api.models import (
    UserProfile,
    Company,
    Project,
    RawMessage,
    Commitment,
    FinancialRecord,
    BusinessEvent,
    WhatsAppConfig,
    AISettings,
    BitrixSettings,
    BitrixDealChangeLog,
    MessageProcessingTrace,
)


@admin.register(UserProfile)
class UserProfileAdmin(ScopedReadOnlyAdmin):
    list_display = (
        "full_name",
        "role",
        "department",
        "phone",
        "monthly_target",
        "current_sales",
        "kpi_badge",
    )
    search_fields = ("full_name", "email", "phone")
    list_filter = ("department", "role")

    def kpi_badge(self, obj):
        pct = obj.kpi_percent
        color = "emerald" if pct >= 100 else "amber" if pct >= 50 else "rose"
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-500">{}%</span>',
            color,
            color,
            pct,
        )

    kpi_badge.short_description = "KPI Выполнение"


@admin.register(Company)
class CompanyAdmin(ScopedReadOnlyAdmin):
    list_display = (
        "name",
        "client_type",
        "contact_person",
        "phone",
        "bitrix_company_id",
        "created_at",
    )
    list_filter = ("client_type",)
    search_fields = ("name", "contact_person", "phone", "bitrix_company_id")


class BitrixDealChangeLogInLine(TabularInline):
    model = BitrixDealChangeLog
    extra = 0
    can_delete = False
    readonly_fields = (
        "created_at_fmt",
        "action_badge",
        "status_badge",
        "changed_fields_summary",
        "duration_fmt",
        "bitrix_deal_link",
    )
    fields = (
        "created_at_fmt",
        "action_badge",
        "status_badge",
        "changed_fields_summary",
        "duration_fmt",
        "bitrix_deal_link",
    )

    def has_add_permission(self, request, obj=None):
        return False

    def created_at_fmt(self, obj):
        return obj.created_at.strftime("%d.%m.%Y %H:%M:%S") if obj.created_at else "—"

    created_at_fmt.short_description = "Дата и время"

    def action_badge(self, obj):
        color = "sky" if obj.action == "create" else "indigo"
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-500">{}</span>',
            color,
            color,
            obj.get_action_display(),
        )

    action_badge.short_description = "Действие"

    def status_badge(self, obj):
        color = "emerald" if obj.status == "success" else "rose"
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-500">{}</span>',
            color,
            color,
            obj.get_status_display(),
        )

    status_badge.short_description = "Статус"

    def changed_fields_summary(self, obj):
        if isinstance(obj.changed_fields, list) and obj.changed_fields:
            fields_str = ", ".join(obj.changed_fields[:4])
            if len(obj.changed_fields) > 4:
                fields_str += f" (+{len(obj.changed_fields) - 4})"
            return fields_str
        return "—"

    changed_fields_summary.short_description = "Измененные поля"

    def duration_fmt(self, obj):
        return f"{obj.duration_ms} мс" if obj.duration_ms else "—"

    duration_fmt.short_description = "Длительность"

    def bitrix_deal_link(self, obj):
        if obj.bitrix_deal_id:
            cfg = BitrixSettings.get_active()
            base_url = (
                cfg.webhook_url.split("/rest/")[0]
                if "/rest/" in cfg.webhook_url
                else "https://aquakip.bitrix24.kz"
            )
            url = f"{base_url}/crm/deal/details/{obj.bitrix_deal_id}/"
            return format_html(
                '<a href="{}" target="_blank" class="text-indigo-600 font-semibold underline">#{}</a>',
                url,
                obj.bitrix_deal_id,
            )
        return "—"

    bitrix_deal_link.short_description = "Bitrix24"


class MessageProcessingTraceInLine(TabularInline):
    model = MessageProcessingTrace
    extra = 0
    can_delete = False
    readonly_fields = (
        "created_at_fmt",
        "whatsapp_sender_fmt",
        "whatsapp_content_snippet",
        "earlier_messages_badge",
        "bitrix_matched_badge",
        "pipeline_action_badge",
        "trace_link",
    )
    fields = (
        "created_at_fmt",
        "whatsapp_sender_fmt",
        "whatsapp_content_snippet",
        "earlier_messages_badge",
        "bitrix_matched_badge",
        "pipeline_action_badge",
        "trace_link",
    )

    def has_add_permission(self, request, obj=None):
        return False

    def created_at_fmt(self, obj):
        return obj.created_at.strftime("%d.%m.%Y %H:%M:%S") if obj.created_at else "—"

    created_at_fmt.short_description = "Дата и время"

    def whatsapp_sender_fmt(self, obj):
        phone_part = (
            f" ({obj.whatsapp_sender_phone})" if obj.whatsapp_sender_phone else ""
        )
        return f"{obj.whatsapp_sender_name}{phone_part}"

    whatsapp_sender_fmt.short_description = "Отправитель"

    def whatsapp_content_snippet(self, obj):
        text = obj.whatsapp_content or ""
        return text[:60] + "..." if len(text) > 60 else text

    whatsapp_content_snippet.short_description = "Сообщение WhatsApp"

    def earlier_messages_badge(self, obj):
        return badge_text(obj)

    earlier_messages_badge.short_description = "Сообщений в контексте"

    def bitrix_matched_badge(self, obj):
        if obj.bitrix_matched_deal_id:
            return format_html(
                '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-blue-500/10 text-blue-500">Сделка #{}</span>',
                obj.bitrix_matched_deal_id,
            )
        return mark_safe('<span class="text-xs text-gray-400">В CRM нет</span>')

    bitrix_matched_badge.short_description = "Bitrix24"

    def pipeline_action_badge(self, obj):
        colors = {
            "created_deal": "emerald",
            "updated_deal": "sky",
            "matched_bitrix_imported": "indigo",
            "commitment_created": "amber",
            "financial_record_created": "emerald",
            "non_commercial": "gray",
            "error": "rose",
        }
        color = colors.get(obj.pipeline_action, "gray")
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-500">{}</span>',
            color,
            color,
            obj.get_pipeline_action_display(),
        )

    pipeline_action_badge.short_description = "Действие"

    def trace_link(self, obj):
        url = f"/admin/api/messageprocessingtrace/{obj.id}/change/"
        return format_html(
            '<a href="{}" class="text-indigo-600 font-semibold underline">Открыть трассировку &rarr;</a>',
            url,
        )

    trace_link.short_description = "Трассировка"


@admin.register(Project)
class ProjectAdmin(ScopedReadOnlyAdmin):
    list_display = (
        "name",
        "verified_badge",
        "source",
        "company",
        "manager",
        "contract_amount_fmt",
        "paid_amount_fmt",
        "due_amount_fmt",
        "margin_badge",
        "status",
        "needs_bitrix_sync",
        "priority",
        "bitrix_link",
    )
    list_filter = (
        "is_verified",
        "source",
        "needs_bitrix_sync",
        "status",
        "priority",
        "project_type",
        "manager",
    )
    search_fields = (
        "name",
        "normalized_name",
        "contract_number",
        "company__name",
        "decision_maker",
        "bitrix_id",
    )
    readonly_fields = (
        "normalized_name",
        "actual_margin_percent",
        "due_amount",
        "created_at",
        "updated_at",
    )
    inlines = [MessageProcessingTraceInLine, BitrixDealChangeLogInLine]
    actions = ["mark_as_verified", "mark_as_unverified"]

    def verified_badge(self, obj):
        if obj.is_verified:
            return mark_safe(
                '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-emerald-500/10 text-emerald-500">✓ Проверено</span>'
            )
        return mark_safe(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-amber-500/10 text-amber-500">⏳ Требует проверки</span>'
        )

    verified_badge.short_description = "Проверено"

    def contract_amount_fmt(self, obj):
        return f"{obj.contract_amount:,.2f} ₸" if obj.contract_amount else "—"

    contract_amount_fmt.short_description = "Сумма договора"

    def paid_amount_fmt(self, obj):
        return f"{obj.paid_amount:,.2f} ₸" if obj.paid_amount else "0.00 ₸"

    paid_amount_fmt.short_description = "Оплачено"

    def due_amount_fmt(self, obj):
        return f"{obj.due_amount:,.2f} ₸" if obj.due_amount else "0.00 ₸"

    due_amount_fmt.short_description = "Дебиторка"

    def margin_badge(self, obj):
        margin = float(obj.actual_margin_percent or 0)
        color = "emerald" if margin >= 20 else "amber" if margin >= 15 else "rose"
        margin_str = f"{margin:.1f}%"
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-500">{}</span>',
            color,
            color,
            margin_str,
        )

    margin_badge.short_description = "Маржа"

    def bitrix_link(self, obj):
        if obj.bitrix_id:
            cfg = BitrixSettings.get_active()
            base_url = (
                cfg.webhook_url.split("/rest/")[0]
                if "/rest/" in cfg.webhook_url
                else "https://aquakip.bitrix24.kz"
            )
            url = f"{base_url}/crm/deal/details/{obj.bitrix_id}/"
            return format_html(
                '<a href="{}" target="_blank" class="text-indigo-600 font-semibold underline">#{}</a>',
                url,
                obj.bitrix_id,
            )
        return "—"

    bitrix_link.short_description = "Bitrix24"


@admin.register(Commitment)
class CommitmentAdmin(ScopedReadOnlyAdmin):
    list_display = (
        "commitment_text_snippet",
        "verified_badge",
        "project",
        "manager",
        "deadline",
        "bitrix_task_id",
        "status_badge",
        "severity",
    )
    list_filter = ("is_verified", "status", "severity", "manager")
    search_fields = (
        "commitment_text",
        "counterparty_person",
        "project__name",
        "bitrix_task_id",
    )
    actions = ["mark_as_verified", "mark_as_unverified"]

    def verified_badge(self, obj):
        if obj.is_verified:
            return mark_safe(
                '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-emerald-500/10 text-emerald-500">✓ Проверено</span>'
            )
        return mark_safe(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-amber-500/10 text-amber-500">⏳ Требует проверки</span>'
        )

    verified_badge.short_description = "Проверено"

    def commitment_text_snippet(self, obj):
        return (
            obj.commitment_text[:60] + "..."
            if len(obj.commitment_text) > 60
            else obj.commitment_text
        )

    commitment_text_snippet.short_description = "Обещание / Задача"

    def status_badge(self, obj):
        colors = {
            "fulfilled": "emerald",
            "pending": "blue",
            "overdue": "rose",
            "cancelled": "gray",
        }
        color = colors.get(obj.status, "gray")
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-500">{}</span>',
            color,
            color,
            obj.get_status_display(),
        )

    status_badge.short_description = "Статус"


@admin.register(FinancialRecord)
class FinancialRecordAdmin(ScopedReadOnlyAdmin):
    list_display = (
        "project",
        "verified_badge",
        "amount_fmt",
        "payment_date",
        "payment_type",
        "status",
        "created_at",
    )
    list_filter = ("is_verified", "status", "payment_type", "payment_date")
    search_fields = ("project__name", "notes")
    actions = ["mark_as_verified", "mark_as_unverified"]

    def verified_badge(self, obj):
        if obj.is_verified:
            return mark_safe(
                '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-emerald-500/10 text-emerald-500">✓ Проверено</span>'
            )
        return mark_safe(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-amber-500/10 text-amber-500">⏳ Требует проверки</span>'
        )

    verified_badge.short_description = "Проверено"

    def amount_fmt(self, obj):
        return f"{obj.amount:,.2f} ₸"

    amount_fmt.short_description = "Сумма"


@admin.register(RawMessage)
class RawMessageAdmin(ScopedReadOnlyAdmin):
    list_display = (
        "sender_name",
        "sender_phone",
        "timestamp",
        "content_snippet",
        "processed_badge",
        "pipeline_trace_link",
    )
    list_filter = ("processed", "timestamp")
    search_fields = ("content", "sender_name", "sender_phone", "message_id")
    readonly_fields = ("timestamp", "created_at", "raw_payload", "pipeline_trace_link")
    actions = ["reprocess_message_pipeline"]

    def content_snippet(self, obj):
        return obj.content[:80] + "..." if len(obj.content) > 80 else obj.content

    content_snippet.short_description = "Текст сообщения"

    def processed_badge(self, obj):
        if obj.processed:
            return format_html(
                '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-emerald-500/10 text-emerald-500">{}</span>',
                "✓ Обработано",
            )
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-amber-500/10 text-amber-500">{}</span>',
            "Ожидает",
        )

    processed_badge.short_description = "Статус обработки"

    def pipeline_trace_link(self, obj):
        trace = getattr(obj, "traces", None)
        trace_obj = trace.first() if trace else None
        if trace_obj:
            url = f"/admin/api/messageprocessingtrace/{trace_obj.id}/change/"
            return format_html(
                '<a href="{}" class="px-2 py-0.5 text-xs font-semibold rounded-full bg-indigo-500/10 text-indigo-600 underline">Цепочка (#{}) &rarr;</a>',
                url,
                trace_obj.id,
            )
        return mark_safe('<span class="text-xs text-gray-400">Нет трассировки</span>')

    pipeline_trace_link.short_description = "Трассировка пайплайна"


@admin.register(BusinessEvent)
class BusinessEventAdmin(ScopedReadOnlyAdmin):
    list_display = ("title", "project", "manager", "timestamp", "severity")
    list_filter = ("severity", "event_type")
    search_fields = ("title", "description", "project__name")


@admin.register(WhatsAppConfig)
class WhatsAppConfigAdmin(IntegrationAdmin):
    list_display = (
        "name",
        "group_jid",
        "session_name",
        "waha_api_url",
        "status",
        "is_active",
        "updated_at",
    )
    list_editable = ("group_jid", "is_active")
    exclude = ("waha_api_key", "waha_api_url", "last_qr_code")


@admin.register(AISettings)
class AISettingsAdmin(IntegrationAdmin):
    list_display = (
        "name",
        "chat_model_name",
        "embedding_model_name",
        "embedding_dimension",
        "is_active",
        "updated_at",
    )
    list_editable = ("chat_model_name", "embedding_model_name", "is_active")
    exclude = ("chat_api_key", "embedding_api_key")


@admin.register(BitrixSettings)
class BitrixSettingsAdmin(IntegrationAdmin):
    form = BitrixSettingsForm
    search_fields = ("name",)

    class Media:
        css = {"all": ("mazory/css/admin_lists.css",)}

    list_display = (
        "name",
        "webhook_summary",
        "sync_summary",
        "last_hourly_sync_at",
    )
    readonly_fields = (
        "webhook_url_masked",
        "last_sync_at",
        "last_hourly_sync_at",
        "last_sync_status",
        "updated_at",
    )
    fieldsets = (
        (
            "Подключение",
            {"fields": ("name", "webhook_url_masked", "new_webhook_url", "is_active")},
        ),
        (
            "Синхронизация",
            {
                "fields": (
                    "hourly_sync_enabled",
                    "auto_create_deals",
                    "auto_import_deals",
                    "auto_create_tasks",
                    "sync_timeline_comments",
                    "deal_category_id",
                    "default_assigned_by_id",
                )
            },
        ),
        (
            "Последние операции",
            {
                "fields": (
                    "last_sync_at",
                    "last_hourly_sync_at",
                    "last_sync_status",
                    "updated_at",
                )
            },
        ),
    )

    def webhook_url_masked(self, obj):
        source = "Сохранён в настройках" if obj.webhook_url else "Из окружения сервера"
        return format_html(
            '<div class="mazory-list-stack"><span class="mazory-webhook-address">{}</span>'
            '<span class="mazory-list-meta">{}</span></div>',
            masked_webhook_url(obj),
            source if effective_webhook_url(obj) else "Укажите адрес ниже",
        )

    webhook_url_masked.short_description = "Webhook"
    exclude = ("webhook_url", "inbound_token")

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj is not None and not self.has_change_permission(request, obj):
            return tuple(
                (
                    title,
                    {
                        **options,
                        "fields": tuple(
                            field
                            for field in options["fields"]
                            if field != "new_webhook_url"
                        ),
                    },
                )
                for title, options in fieldsets
            )
        return fieldsets

    @admin.display(description="Webhook")
    def webhook_summary(self, obj):
        return format_html(
            '<div class="mazory-list-stack">{}<a class="mazory-list-link" href="{}">Изменить Webhook →</a></div>',
            self.webhook_url_masked(obj),
            reverse("admin:api_bitrixsettings_change", args=[obj.pk]),
        )

    @admin.display(description="Синхронизация")
    def sync_summary(self, obj):
        return format_html(
            '<div class="mazory-list-stack"><span class="mazory-admin-badge mazory-admin-badge--{}">{}</span>{}</div>',
            "good" if obj.is_active else "neutral",
            "Активна" if obj.is_active else "Выключена",
            format_html_join(
                "",
                '<span class="mazory-list-meta">{}: {}</span>',
                (
                    (label, "вкл." if enabled else "выкл.")
                    for label, enabled in (
                        ("Каждый час", obj.hourly_sync_enabled),
                        ("Импорт сделок", obj.auto_import_deals),
                        ("Задачи по дедлайнам", obj.auto_create_tasks),
                    )
                ),
            ),
        )

    @method_decorator(sensitive_post_parameters("new_webhook_url"))
    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        return super().changeform_view(request, object_id, form_url, extra_context)


@admin.register(BitrixDealChangeLog)
class BitrixDealChangeLogAdmin(ScopedReadOnlyAdmin):
    list_display = (
        "created_at_fmt",
        "bitrix_deal_link",
        "project_link",
        "action_badge",
        "status_badge",
        "changed_fields_summary",
        "duration_fmt",
        "triggered_by",
    )
    list_filter = ("status", "action", "created_at")
    search_fields = ("bitrix_deal_id", "project__name", "error_message", "triggered_by")
    readonly_fields = (
        "project",
        "bitrix_deal_id",
        "action",
        "status",
        "created_at",
        "duration_ms",
        "triggered_by",
        "error_message",
        "formatted_payload",
        "formatted_response",
        "changed_fields",
    )
    fields = (
        "created_at",
        "status",
        "action",
        "bitrix_deal_id",
        "project",
        "duration_ms",
        "triggered_by",
        "error_message",
        "formatted_payload",
        "formatted_response",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def created_at_fmt(self, obj):
        return obj.created_at.strftime("%d.%m.%Y %H:%M:%S") if obj.created_at else "—"

    created_at_fmt.short_description = "Время отправки"

    def bitrix_deal_link(self, obj):
        if obj.bitrix_deal_id:
            cfg = BitrixSettings.get_active()
            base_url = (
                cfg.webhook_url.split("/rest/")[0]
                if "/rest/" in cfg.webhook_url
                else "https://aquakip.bitrix24.kz"
            )
            url = f"{base_url}/crm/deal/details/{obj.bitrix_deal_id}/"
            return format_html(
                '<a href="{}" target="_blank" class="text-indigo-600 font-semibold underline">#{}</a>',
                url,
                obj.bitrix_deal_id,
            )
        return "—"

    bitrix_deal_link.short_description = "Сделка Bitrix24"

    def project_link(self, obj):
        if obj.project:
            url = f"/admin/api/project/{obj.project.id}/change/"
            return format_html(
                '<a href="{}" class="text-indigo-600 font-semibold underline">{}</a>',
                url,
                obj.project.name,
            )
        return "—"

    project_link.short_description = "Объект Mazory"

    def action_badge(self, obj):
        color = "sky" if obj.action == "create" else "indigo"
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-500">{}</span>',
            color,
            color,
            obj.get_action_display(),
        )

    action_badge.short_description = "Действие"

    def status_badge(self, obj):
        color = "emerald" if obj.status == "success" else "rose"
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-500">{}</span>',
            color,
            color,
            obj.get_status_display(),
        )

    status_badge.short_description = "Статус"

    def changed_fields_summary(self, obj):
        if isinstance(obj.changed_fields, list) and obj.changed_fields:
            fields_str = ", ".join(obj.changed_fields[:5])
            if len(obj.changed_fields) > 5:
                fields_str += f" (+{len(obj.changed_fields) - 5})"
            return fields_str
        return "—"

    changed_fields_summary.short_description = "Измененные поля"

    def duration_fmt(self, obj):
        return f"{obj.duration_ms} мс" if obj.duration_ms else "—"

    duration_fmt.short_description = "Длительность"

    def formatted_payload(self, obj):
        formatted = json.dumps(obj.payload or {}, indent=2, ensure_ascii=False)
        return format_html(
            '<pre class="bg-gray-900 text-gray-100 p-4 rounded-lg overflow-x-auto text-xs font-mono"><code>{}</code></pre>',
            formatted,
        )

    formatted_payload.short_description = "Отправленные данные (Payload)"

    def formatted_response(self, obj):
        formatted = json.dumps(obj.response_data or {}, indent=2, ensure_ascii=False)
        return format_html(
            '<pre class="bg-gray-900 text-gray-100 p-4 rounded-lg overflow-x-auto text-xs font-mono"><code>{}</code></pre>',
            formatted,
        )

    formatted_response.short_description = "Ответ Bitrix24 REST API"


@admin.register(MessageProcessingTrace)
class MessageProcessingTraceAdmin(ScopedReadOnlyAdmin):
    class Media:
        css = {"all": ("mazory/css/admin_trace.css", "mazory/css/admin_lists.css")}

    list_display = (
        "trace_source",
        "whatsapp_content_snippet",
        "trace_context",
        "trace_result",
        "project_link",
    )
    list_display_links = ("trace_source",)
    list_select_related = ("project",)
    list_filter = ("pipeline_action", "status", "created_at")
    search_fields = (
        "whatsapp_content",
        "whatsapp_sender_name",
        "whatsapp_sender_phone",
        "bitrix_matched_deal_id",
        "project__name",
        "whatsapp_message_id",
    )
    readonly_fields = (
        "pipeline_overview_banner",
        "created_at_fmt",
        "pipeline_action_badge",
        "status_badge",
        "result_summary_fmt",
        "stage_1_whatsapp_card",
        "stage_2_earlier_messages_card",
        "stage_3_bitrix_card",
        "stage_4_final_record_card",
    )
    fieldsets = (
        (
            "Сквозная цепочка пайплайна (WhatsApp → Сообщения ранее → Bitrix24 → Итог)",
            {"fields": ("pipeline_overview_banner", "result_summary_fmt")},
        ),
        ("Этап 1: «Входные данные WhatsApp»", {"fields": ("stage_1_whatsapp_card",)}),
        (
            "Этап 2: История сообщений",
            {"fields": ("stage_2_earlier_messages_card",)},
        ),
        (
            "Этап 3: «Зависимые данные из Bitrix24» (CRM Поиск & Синхронизация)",
            {"fields": ("stage_3_bitrix_card",)},
        ),
        (
            "Этап 4: «Итоговая запись» (AI Факты & Созданные сущности)",
            {"fields": ("stage_4_final_record_card",)},
        ),
    )
    change_form_template = "admin/message_trace_change.html"
    actions = None

    def get_urls(self):
        from django.urls import path

        return [
            path(
                "<int:object_id>/context/",
                self.admin_site.admin_view(self.context_view),
                name="api_messageprocessingtrace_context",
            ),
            path(
                "<int:object_id>/reanalyse/",
                self.admin_site.admin_view(self.reanalyse_view),
                name="api_messageprocessingtrace_reanalyse",
            ),
        ] + super().get_urls()

    def _trace_for_request(self, request, object_id):
        from django.shortcuts import get_object_or_404
        from django.core.exceptions import PermissionDenied

        obj = get_object_or_404(
            self.get_queryset(request).select_related(
                "raw_message__config__team", "raw_message__team", "raw_message__project"
            ),
            pk=object_id,
        )
        if not self.has_view_permission(request, obj):
            raise PermissionDenied()
        return obj

    def context_view(self, request, object_id):
        from django.template.response import TemplateResponse

        obj = self._trace_for_request(request, object_id)
        data = context_view_data(obj, request.user, request.GET.get("page", 1))
        return TemplateResponse(
            request,
            "admin/message_context_page.html",
            {
                **self.admin_site.each_context(request),
                **data,
                "opts": self.model._meta,
                "title": f"История — попытка {obj.attempt_no}",
                "context_full_page": True,
            },
        )

    def reanalyse_view(self, request, object_id):
        import uuid
        from django.http import HttpResponseNotAllowed, HttpResponseBadRequest
        from django.shortcuts import redirect
        from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
        from rest_framework.exceptions import PermissionDenied
        from .processing_attempts import schedule_reanalysis
        from .providers import ProviderUnavailable

        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        obj = self._trace_for_request(request, object_id)
        if not obj.raw_message_id:
            return HttpResponseBadRequest("Исходное сообщение недоступно.")
        try:
            key = str(uuid.UUID(request.POST.get("request_key", "")))
        except ValueError:
            return HttpResponseBadRequest("Некорректный идентификатор запроса.")
        try:
            attempt = schedule_reanalysis(obj.raw_message_id, request.user, key)
        except PermissionDenied as exc:
            raise DjangoPermissionDenied() from exc
        except ProviderUnavailable as exc:
            self.message_user(
                request,
                ERRORS.get(str(exc), "Повторный анализ недоступен."),
                level=messages.ERROR,
            )
            return redirect("admin:api_messageprocessingtrace_change", object_id)
        self.message_user(
            request,
            f"Попытка №{attempt.attempt_no} поставлена в очередь. Предыдущие результаты сохранены.",
        )
        return redirect("admin:api_messageprocessingtrace_change", attempt.pk)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        import uuid
        from rest_framework.exceptions import PermissionDenied
        from .processing_attempts import require_reanalysis
        from .providers import ProviderUnavailable

        obj = self._trace_for_request(request, object_id)
        allowed, reason = False, "Исходное сообщение недоступно."
        if obj.raw_message_id:
            try:
                require_reanalysis(request.user, obj.raw_message)
                allowed = True
            except PermissionDenied:
                reason = "Повторный анализ доступен руководителю с доступом к этому источнику."
            except ProviderUnavailable as exc:
                reason = ERRORS.get(str(exc), "Повторный анализ недоступен.")
        return super().change_view(
            request,
            object_id,
            form_url,
            {
                **(extra_context or {}),
                "can_reanalyse": allowed,
                "reanalysis_unavailable": reason,
                "reanalysis_request_key": str(uuid.uuid4()),
                "trace_attempts": self.get_queryset(request)
                .filter(raw_message_id=obj.raw_message_id)
                .order_by("attempt_no")
                if obj.raw_message_id
                else [obj],
            },
        )

    def has_add_permission(self, request):
        return False

    def created_at_fmt(self, obj):
        return obj.created_at.strftime("%d.%m.%Y %H:%M:%S") if obj.created_at else "—"

    created_at_fmt.short_description = "Время обработки"

    @admin.display(description="Время / отправитель", ordering="created_at")
    def trace_source(self, obj):
        return format_html(
            '<span class="mazory-list-stack"><span class="mazory-list-meta">{}</span>'
            '<strong>{}</strong><span class="mazory-list-meta mazory-sender-id">{}</span>'
            '<span class="mazory-list-link">Открыть трассировку →</span></span>',
            self.created_at_fmt(obj),
            obj.whatsapp_sender_name or "Без имени",
            obj.whatsapp_sender_phone or "—",
        )

    @admin.display(description="Контекст / CRM")
    def trace_context(self, obj):
        return format_html(
            '<div class="mazory-list-stack">{}{}</div>',
            self.earlier_messages_badge(obj),
            self.bitrix_matched_badge(obj),
        )

    @admin.display(description="Результат")
    def trace_result(self, obj):
        return format_html(
            '<div class="mazory-list-stack">{}{}</div>',
            self.pipeline_action_badge(obj),
            self.status_badge(obj),
        )

    def whatsapp_sender_fmt(self, obj):
        phone_part = (
            format_html(
                "<br><span class='text-xs text-gray-400 font-mono'>{}</span>",
                obj.whatsapp_sender_phone,
            )
            if obj.whatsapp_sender_phone
            else ""
        )
        return format_html(
            "<b>{}</b>{}", obj.whatsapp_sender_name, mark_safe(phone_part)
        )

    whatsapp_sender_fmt.short_description = "Отправитель"

    def whatsapp_content_snippet(self, obj):
        text = obj.whatsapp_content or ""
        snippet = text[:320] + "…" if len(text) > 320 else text
        return format_html(
            '<span class="mazory-message-preview">{}</span>', snippet or "Без текста"
        )

    whatsapp_content_snippet.short_description = "Текст сообщения"

    def earlier_messages_badge(self, obj):
        return format_html(
            '<span class="mazory-admin-badge mazory-admin-badge--info">{}</span>',
            badge_text(obj),
        )

    earlier_messages_badge.short_description = "Сообщений в контексте"

    def bitrix_matched_badge(self, obj):
        if obj.bitrix_matched_deal_id:
            cfg = BitrixSettings.get_active()
            base_url = (
                effective_webhook_url(cfg).split("/rest/")[0]
                if "/rest/" in effective_webhook_url(cfg)
                else "https://aquakip.bitrix24.kz"
            )
            url = f"{base_url}/crm/deal/details/{obj.bitrix_matched_deal_id}/"
            return format_html(
                '<a href="{}" target="_blank" rel="noopener noreferrer" class="mazory-admin-badge mazory-admin-badge--info">CRM #{} ↗</a>',
                url,
                obj.bitrix_matched_deal_id,
            )
        return mark_safe('<span class="mazory-list-meta">В CRM нет</span>')

    bitrix_matched_badge.short_description = "Bitrix24 CRM"

    def pipeline_action_badge(self, obj):
        colors = {
            "created_deal": ("good", "Создана сделка"),
            "updated_deal": ("info", "Обновлена сделка"),
            "matched_bitrix_imported": ("info", "Импорт из CRM"),
            "commitment_created": ("warn", "Обязательство"),
            "financial_record_created": ("good", "Оплата"),
            "non_commercial": ("neutral", "Инфо-сообщение"),
            "proposed_facts": ("warn", "Предложены факты"),
            "error": ("error", "Ошибка"),
        }
        color, label = colors.get(
            obj.pipeline_action, ("neutral", obj.get_pipeline_action_display())
        )
        return format_html(
            '<span class="mazory-admin-badge mazory-admin-badge--{}">{}</span>',
            color,
            label,
        )

    pipeline_action_badge.short_description = "Итоговое действие"

    def project_link(self, obj):
        if obj.project:
            return format_html(
                '<a href="{}" class="mazory-list-stack mazory-list-link"><span class="mazory-project-name" title="{}">{}</span>'
                '<span class="mazory-list-meta">{} ₸</span><span class="mazory-list-meta">{}</span></a>',
                reverse("admin:api_project_change", args=[obj.project_id]),
                obj.project.name,
                obj.project.name,
                f"{obj.project.contract_amount or 0:,.0f}",
                "Подтверждена" if obj.project.is_verified else "Ждёт проверки",
            )
        return format_html('<span class="mazory-list-meta">{}</span>', "Без сделки")

    project_link.short_description = "Сделка"

    def status_badge(self, obj):
        color = {"success": "good", "warning": "warn", "error": "error"}.get(
            obj.status, "neutral"
        )
        return format_html(
            '<span class="mazory-admin-badge mazory-admin-badge--{}">{}</span>',
            color,
            obj.get_status_display(),
        )

    status_badge.short_description = "Статус"

    def result_summary_fmt(self, obj):
        lines = (obj.result_summary or "").split("\n")
        items_html = "".join(
            [
                format_html(
                    "<li class='py-1 flex items-start gap-2'><span class='text-indigo-500 font-bold'>•</span><span class='mazory-trace-text'>{}</span></li>",
                    line,
                )
                for line in lines
                if line.strip()
            ]
        )
        return format_html(
            '<div class="p-4 mazory-trace-card rounded-xl border font-sans text-sm mazory-trace-text shadow-sm"><ul class="space-y-1">{}</ul></div>',
            mark_safe(items_html)
            if items_html
            else mark_safe("<em>Нет текстового резюме</em>"),
        )

    result_summary_fmt.short_description = "Резюме цепочки принятия решений"

    def pipeline_overview_banner(self, obj):
        """Интерактивный 4-шаговый визуальный прогресс пайплайна"""
        s1_title = f"{obj.whatsapp_sender_name}"
        s2_sub = badge_text(obj)
        s3_sub = (
            f"CRM #{obj.bitrix_matched_deal_id}"
            if obj.bitrix_matched_deal_id
            else "В CRM отсутствует"
        )
        s4_sub = obj.get_pipeline_action_display()
        html = format_html(
            '\n        <div class="mazory-trace-overview w-full my-3 p-5 rounded-2xl bg-gradient-to-r from-gray-900 via-indigo-950 to-gray-900 text-white shadow-lg border border-indigo-900/40">\n            <div class="text-xs font-mono uppercase tracking-wider text-indigo-400 mb-3 flex items-center justify-between">\n                <span>Data Lineage Audit Trail</span>\n                <span>ID Трассировки: #{}</span>\n            </div>\n            <div class="grid grid-cols-1 md:grid-cols-4 gap-4 relative">\n                <!-- Step 1 -->\n                <div class="p-3 rounded-xl bg-white/5 border border-emerald-500/30 flex flex-col justify-between">\n                    <div class="flex items-center gap-2 text-emerald-400 font-semibold text-xs uppercase">\n                        <span class="w-5 h-5 rounded-full bg-emerald-500/20 flex items-center justify-center text-xs">1</span>\n                        WhatsApp Вход\n                    </div>\n                    <div class="mt-2 text-sm font-bold truncate text-white">{}</div>\n                    <div class="text-xs text-gray-400 font-mono mt-0.5">{}</div>\n                </div>\n\n                <!-- Step 2 -->\n                <div class="p-3 rounded-xl bg-white/5 border border-purple-500/30 flex flex-col justify-between">\n                    <div class="flex items-center gap-2 text-purple-400 font-semibold text-xs uppercase">\n                        <span class="w-5 h-5 rounded-full bg-purple-500/20 flex items-center justify-center text-xs">2</span>\n                        Контекст ранее\n                    </div>\n                    <div class="mt-2 text-sm font-bold text-white">{}</div>\n                    <div class="text-xs text-purple-300 mt-0.5">Сохранённый контекст</div>\n                </div>\n\n                <!-- Step 3 -->\n                <div class="p-3 rounded-xl bg-white/5 border border-sky-500/30 flex flex-col justify-between">\n                    <div class="flex items-center gap-2 text-sky-400 font-semibold text-xs uppercase">\n                        <span class="w-5 h-5 rounded-full bg-sky-500/20 flex items-center justify-center text-xs">3</span>\n                        Данные Bitrix24\n                    </div>\n                    <div class="mt-2 text-sm font-bold text-white truncate">{}</div>\n                    <div class="text-xs text-sky-300 mt-0.5 truncate">{}</div>\n                </div>\n\n                <!-- Step 4 -->\n                <div class="p-3 rounded-xl bg-white/5 border border-amber-500/30 flex flex-col justify-between">\n                    <div class="flex items-center gap-2 text-amber-400 font-semibold text-xs uppercase">\n                        <span class="w-5 h-5 rounded-full bg-amber-500/20 flex items-center justify-center text-xs">4</span>\n                        Итоговая запись\n                    </div>\n                    <div class="mt-2 text-sm font-bold text-white truncate">{}</div>\n                    <div class="text-xs text-amber-300 mt-0.5 truncate">{}</div>\n                </div>\n            </div>\n        </div>\n        ',
            obj.id,
            s1_title,
            obj.whatsapp_sender_phone or "Прямой вебхук",
            s2_sub,
            s3_sub,
            obj.bitrix_deal_title or "Поиск по объекту",
            s4_sub,
            obj.project.name if obj.project else "Связанная сущность",
        )
        return mark_safe(html)

    pipeline_overview_banner.short_description = "Сквозной процесс обработки"

    def stage_1_whatsapp_card(self, obj):
        """Рендеринг Этапа 1: «Входные данные WhatsApp»"""
        raw_json = json.dumps(
            obj.whatsapp_raw_payload or {}, indent=2, ensure_ascii=False
        )
        ts_str = (
            obj.whatsapp_timestamp.strftime("%d.%m.%Y %H:%M:%S")
            if obj.whatsapp_timestamp
            else "—"
        )
        html = format_html(
            '\n        <style>\n        .mazory-trace-content {{ background-color: #ffffff !important; border: 1px solid #e2e8f0 !important; color: #0f172a !important; }}\n        html.dark .mazory-trace-content, body.dark .mazory-trace-content, .dark .mazory-trace-content {{ background-color: #0f172a !important; border-color: #334155 !important; color: #f8fafc !important; }}\n        .mazory-trace-card {{ background-color: #ffffff !important; border: 1px solid #e2e8f0 !important; color: #0f172a !important; }}\n        html.dark .mazory-trace-card, body.dark .mazory-trace-card, .dark .mazory-trace-card {{ background-color: #1e293b !important; border-color: #334155 !important; color: #f8fafc !important; }}\n        .mazory-trace-text {{ color: #0f172a !important; }}\n        html.dark .mazory-trace-text, body.dark .mazory-trace-text, .dark .mazory-trace-text {{ color: #f8fafc !important; }}\n        .mazory-trace-meta {{ color: #475569 !important; }}\n        html.dark .mazory-trace-meta, body.dark .mazory-trace-meta, .dark .mazory-trace-meta {{ color: #94a3b8 !important; }}\n        .mazory-trace-label {{ color: #64748b !important; }}\n        html.dark .mazory-trace-label, body.dark .mazory-trace-label, .dark .mazory-trace-label {{ color: #94a3b8 !important; }}\n        </style>\n        <div class="p-5 rounded-xl border border-emerald-500/30 bg-emerald-50/10 dark:bg-emerald-950/10 space-y-4">\n            <div class="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-emerald-500/20">\n                <div class="flex items-center gap-3">\n                    <span class="p-2 rounded-lg bg-emerald-500/20 text-emerald-600 font-bold text-sm">WhatsApp</span>\n                    <div>\n                        <div class="text-sm font-bold mazory-trace-text">{}</div>\n                        <div class="text-xs mazory-trace-meta font-mono">{}</div>\n                    </div>\n                </div>\n                <div class="text-right text-xs mazory-trace-meta">\n                    <div><b>Время получения:</b> {}</div>\n                    <div><b>Чат / Группа:</b> <span class="font-mono text-indigo-500 font-semibold">{}</span></div>\n                    <div><b>Message ID:</b> <span class="font-mono mazory-trace-label">{}</span></div>\n                </div>\n            </div>\n\n            <div>\n                <div class="text-xs font-semibold mazory-trace-meta uppercase tracking-wider mb-1">Исходный текст сообщения WhatsApp:</div>\n                <div class="p-4 rounded-lg text-sm font-sans whitespace-pre-wrap leading-relaxed shadow-sm mazory-trace-content">\n{}\n                </div>\n            </div>\n\n            <details class="text-xs mazory-trace-meta cursor-pointer pt-2">\n                <summary class="font-semibold text-emerald-600 hover:underline">Показать сырой payload сообщения (WAHA Webhook JSON)</summary>\n                <pre class="mt-2 p-3 bg-gray-950 text-gray-200 rounded-lg overflow-x-auto font-mono text-xs max-h-60"><code>{}</code></pre>\n            </details>\n        </div>\n        ',
            obj.whatsapp_sender_name,
            obj.whatsapp_sender_phone or "Номер скрыт",
            ts_str,
            obj.whatsapp_chat_id or "sales-group",
            obj.whatsapp_message_id,
            obj.whatsapp_content or "—",
            raw_json,
        )
        return mark_safe(html)

    stage_1_whatsapp_card.short_description = "1. Входные данные WhatsApp"

    def stage_2_earlier_messages_card(self, obj):
        return mark_safe(render_context(obj))

    stage_2_earlier_messages_card.short_description = "2. История сообщений"

    def stage_3_bitrix_card(self, obj):
        """Рендеринг Этапа 3: «Зависимые данные из Bitrix24»"""
        matched_id = obj.bitrix_matched_deal_id
        cfg = BitrixSettings.get_active()
        base_url = (
            effective_webhook_url(cfg).split("/rest/")[0]
            if "/rest/" in effective_webhook_url(cfg)
            else "https://aquakip.bitrix24.kz"
        )
        deal_url = f"{base_url}/crm/deal/details/{matched_id}/" if matched_id else "#"
        opp_str = (
            f"{obj.bitrix_deal_opportunity:,.2f} ₸"
            if obj.bitrix_deal_opportunity
            else "—"
        )
        raw_deal_json = json.dumps(
            obj.bitrix_raw_deal or {}, indent=2, ensure_ascii=False
        )
        if matched_id:
            deal_block = format_html(
                '\n            <div class="p-4 rounded-xl mazory-trace-card border border-sky-500/30 shadow-sm space-y-3">\n                <div class="flex items-center justify-between">\n                    <div class="flex items-center gap-2">\n                        <span class="px-2.5 py-1 text-xs font-bold rounded-lg bg-sky-500/20 text-sky-600">\n                            Сделка в Bitrix24 найдена\n                        </span>\n                        <a href="{}" target="_blank" class="text-sm font-bold text-sky-600 hover:underline">\n                            #{} — {} ↗\n                        </a>\n                    </div>\n                    <span class="text-xs font-mono mazory-trace-meta">Стадия CRM: {}</span>\n                </div>\n\n                <div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">\n                    <div class="p-2.5 rounded mazory-trace-content">\n                        <span class="mazory-trace-meta block text-[11px]">Сумма в CRM:</span>\n                        <span class="font-bold mazory-trace-text">{}</span>\n                    </div>\n                    <div class="p-2.5 rounded mazory-trace-content">\n                        <span class="mazory-trace-meta block text-[11px]">Компания в CRM:</span>\n                        <span class="font-bold mazory-trace-text">{}</span>\n                    </div>\n                    <div class="p-2.5 rounded mazory-trace-content">\n                        <span class="mazory-trace-meta block text-[11px]">Поисковый запрос:</span>\n                        <span class="font-bold text-indigo-500 font-mono truncate block">{}</span>\n                    </div>\n                    <div class="p-2.5 rounded mazory-trace-content">\n                        <span class="mazory-trace-meta block text-[11px]">Защита от дублей:</span>\n                        <span class="font-bold text-emerald-600">Связана существующая</span>\n                    </div>\n                </div>\n\n                <details class="text-xs mazory-trace-meta cursor-pointer pt-1">\n                    <summary class="font-semibold text-sky-600 hover:underline">Показать сырой ответ crm.deal.list/get (JSON)</summary>\n                    <pre class="mt-2 p-3 bg-gray-950 text-gray-200 rounded-lg overflow-x-auto font-mono text-xs max-h-56"><code>{}</code></pre>\n                </details>\n            </div>\n            ',
                deal_url,
                matched_id,
                obj.bitrix_deal_title or "Сделка Bitrix24",
                obj.bitrix_deal_stage or "PREPARATION",
                opp_str,
                obj.bitrix_company_data.get("company_name")
                or obj.bitrix_company_data.get("TITLE")
                or obj.bitrix_company_data.get("name")
                or "ТОО / Не привязана",
                obj.bitrix_search_query or "—",
                raw_deal_json,
            )
        else:
            deal_block = format_html(
                '\n            <div class="p-4 rounded-xl mazory-trace-card border border-amber-500/30 shadow-sm space-y-2">\n                <div class="flex items-center gap-2 text-xs font-semibold text-amber-600">\n                    <span>⚠ В Bitrix24 CRM сделка по объекту «{}» не найдена</span>\n                </div>\n                <div class="text-xs mazory-trace-meta">\n                    Система проверила наличие сделки через <code>BitrixService.find_deal_by_name</code> по полям TITLE и кастомным свойствам объекта.\n                    Так как совпадений нет, сделка создана локально как новая и подготовлена к первичной регистрации.\n                </div>\n            </div>\n            ',
                obj.bitrix_search_query or "—",
            )
        summary_text = obj.bitrix_known_deals_summary or "—"
        html = format_html(
            '\n        <div class="p-5 rounded-xl border border-sky-500/30 bg-sky-50/10 dark:bg-sky-950/10 space-y-3">\n            <div class="flex items-center justify-between pb-2 border-b border-sky-500/20">\n                <div class="text-xs font-semibold text-sky-600 uppercase tracking-wider">\n                    Состояние Bitrix24 CRM на момент обработки\n                </div>\n                <div class="text-xs mazory-trace-meta">\n                    Webhook: <span class="font-mono text-sky-600">{}</span>\n                </div>\n            </div>\n\n            {}\n\n            <details class="text-xs mazory-trace-meta cursor-pointer pt-1">\n                <summary class="font-semibold text-sky-600 hover:underline">Сводка известных сделок компании, переданная в контекст AI (промпт)</summary>\n                <div class="mt-2 p-3 rounded-lg mazory-trace-content text-xs font-mono max-h-48 overflow-y-auto whitespace-pre-wrap">\n{}\n                </div>\n            </details>\n        </div>\n        ',
            cfg.name,
            deal_block,
            summary_text,
        )
        return mark_safe(html)

    stage_3_bitrix_card.short_description = "3. Зависимые данные из Bitrix24"

    def stage_4_final_record_card(self, obj):
        """Рендеринг Этапа 4: «Итоговая запись»"""
        facts = obj.ai_extracted_facts or {}
        facts_json = json.dumps(facts, indent=2, ensure_ascii=False)
        conf_pct = f"{obj.ai_confidence * 100:.0f}%"
        project_html = (
            '<em class="mazory-trace-meta">Сделка не создавалась/не привязана</em>'
        )
        if obj.project:
            p = obj.project
            p_url = f"/admin/api/project/{p.id}/change/"
            verified_badge = (
                '<span class="text-emerald-500 font-bold">✓ Проверено</span>'
                if p.is_verified
                else '<span class="text-amber-500 font-bold">⏳ Ожидает проверки</span>'
            )
            project_html = format_html(
                '\n            <div class="p-3 rounded-lg mazory-trace-card border border-emerald-500/30 space-y-2">\n                <div class="flex items-center justify-between">\n                    <a href="{}" class="text-sm font-bold text-indigo-600 hover:underline">\n                        Проект #{}: {} ↗\n                    </a>\n                    {}\n                </div>\n                <div class="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">\n                    <div><span class="mazory-trace-meta">Сумма:</span> <b class="mazory-trace-text">{} ₸</b></div>\n                    <div><span class="mazory-trace-meta">Маржа:</span> <b class="mazory-trace-text">{}%</b></div>\n                    <div><span class="mazory-trace-meta">Статус:</span> <b class="mazory-trace-text">{}</b></div>\n                    <div><span class="mazory-trace-meta">Синхр. Bitrix:</span> <b class="mazory-trace-text">{}</b></div>\n                </div>\n            </div>\n            ',
                p_url,
                p.id,
                p.name,
                verified_badge,
                f"{p.contract_amount:,.2f}",
                p.actual_margin_percent,
                p.get_status_display(),
                "Да" if p.needs_bitrix_sync else "Актуально",
            )
        commitment_html = ""
        if obj.commitment:
            c = obj.commitment
            c_url = f"/admin/api/commitment/{c.id}/change/"
            commitment_html = format_html(
                '\n            <div class="p-3 rounded-lg mazory-trace-card border border-amber-500/30 space-y-1 text-xs">\n                <div class="flex items-center justify-between">\n                    <span class="font-bold text-amber-600">Создано обязательство (SLA):</span>\n                    <a href="{}" class="text-indigo-600 hover:underline font-semibold">#{} ↗</a>\n                </div>\n                <div class="mazory-trace-text font-medium">«{}»</div>\n                <div class="mazory-trace-meta flex gap-4">\n                    <span>Дедлайн: <b class="mazory-trace-text">{}</b></span>\n                    <span>Статус: <b class="mazory-trace-text">{}</b></span>\n                    <span>Срочность: <b class="mazory-trace-text">{}</b></span>\n                </div>\n            </div>\n            ',
                c_url,
                c.id,
                c.commitment_text,
                c.deadline or "—",
                c.get_status_display(),
                c.get_severity_display(),
            )
        fin_html = ""
        if obj.financial_record:
            f = obj.financial_record
            f_url = f"/admin/api/financialrecord/{f.id}/change/"
            fin_html = format_html(
                '\n            <div class="p-3 rounded-lg mazory-trace-card border border-teal-500/30 space-y-1 text-xs">\n                <div class="flex items-center justify-between">\n                    <span class="font-bold text-teal-600">Зафиксирована финансовая запись:</span>\n                    <a href="{}" class="text-indigo-600 hover:underline font-semibold">#{} ↗</a>\n                </div>\n                <div class="mazory-trace-text font-medium">Сумма: <b class="mazory-trace-text">{} ₸</b> ({})</div>\n                <div class="mazory-trace-meta">Дата: <b class="mazory-trace-text">{}</b> | Статус: <b class="mazory-trace-text">{}</b></div>\n            </div>\n            ',
                f_url,
                f.id,
                f"{f.amount:,.2f}",
                f.get_payment_type_display(),
                f.payment_date,
                f.get_status_display(),
            )
        html = format_html(
            '\n        <div class="p-5 rounded-xl border border-emerald-500/30 bg-emerald-50/10 dark:bg-emerald-950/10 space-y-4">\n            <div class="flex items-center justify-between pb-2 border-b border-emerald-500/20">\n                <div class="text-xs font-semibold text-emerald-600 uppercase tracking-wider">\n                    Результат обработки нейросетью и созданные записи\n                </div>\n                <div class="text-xs">\n                    Уверенность AI: <span class="px-2 py-0.5 rounded font-bold bg-emerald-500/20 text-emerald-600">{}</span>\n                </div>\n            </div>\n\n            <!-- AI Facts table -->\n            <div>\n                <div class="text-xs font-semibold mazory-trace-meta uppercase tracking-wider mb-2">Извлеченные структурированные факты AI:</div>\n                <div class="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">\n                    <div class="p-2.5 rounded mazory-trace-content">\n                        <span class="mazory-trace-meta block text-[11px]">Объект:</span>\n                        <span class="font-bold mazory-trace-text">{}</span>\n                    </div>\n                    <div class="p-2.5 rounded mazory-trace-content">\n                        <span class="mazory-trace-meta block text-[11px]">Сумма договора:</span>\n                        <span class="font-bold text-emerald-600">{} ₸</span>\n                    </div>\n                    <div class="p-2.5 rounded mazory-trace-content">\n                        <span class="mazory-trace-meta block text-[11px]">Оборудование:</span>\n                        <span class="font-bold mazory-trace-text">{}</span>\n                    </div>\n                    <div class="p-2.5 rounded mazory-trace-content">\n                        <span class="mazory-trace-meta block text-[11px]">Стадия:</span>\n                        <span class="font-bold mazory-trace-text">{}</span>\n                    </div>\n                    <div class="p-2.5 rounded mazory-trace-content">\n                        <span class="mazory-trace-meta block text-[11px]">Следующий шаг:</span>\n                        <span class="font-bold mazory-trace-text">{}</span>\n                    </div>\n                    <div class="p-2.5 rounded mazory-trace-content">\n                        <span class="mazory-trace-meta block text-[11px]">Дедлайн:</span>\n                        <span class="font-bold mazory-trace-text">{}</span>\n                    </div>\n                    <div class="p-2.5 rounded mazory-trace-content">\n                        <span class="mazory-trace-meta block text-[11px]">Компания:</span>\n                        <span class="font-bold mazory-trace-text">{}</span>\n                    </div>\n                    <div class="p-2.5 rounded mazory-trace-content">\n                        <span class="mazory-trace-meta block text-[11px]">Приоритет:</span>\n                        <span class="font-bold mazory-trace-text">{}</span>\n                    </div>\n                </div>\n            </div>\n\n            <!-- Entities created -->\n            <div class="space-y-2 pt-1">\n                <div class="text-xs font-semibold mazory-trace-meta uppercase tracking-wider">Связанные сущности в БД Mazory:</div>\n                {}\n                {}\n                {}\n            </div>\n\n            <details class="text-xs mazory-trace-meta cursor-pointer pt-1">\n                <summary class="font-semibold text-emerald-600 hover:underline">Показать полный JSON ответ нейросети (facts)</summary>\n                <pre class="mt-2 p-3 bg-gray-950 text-gray-200 rounded-lg overflow-x-auto font-mono text-xs max-h-56"><code>{}</code></pre>\n            </details>\n        </div>\n        ',
            conf_pct,
            facts.get("object_name") or "—",
            f"{float(facts.get('contract_amount') or 0):,.2f}",
            facts.get("direction") or facts.get("equipment_type") or "—",
            facts.get("stage") or "—",
            facts.get("next_action") or "—",
            facts.get("next_action_at") or "—",
            facts.get("company_name") or "—",
            facts.get("priority") or "standard",
            project_html,
            commitment_html,
            fin_html,
            facts_json,
        )
        return mark_safe(html)

    stage_4_final_record_card.short_description = "4. Итоговая запись"


from .models import (
    Team,
    TeamMembership,
    ChatAccess,
    ClientProjectAccess,
    AuditEvent,
    ProjectRevision,
    OutboxEvent,
    FactCandidate,
    NotificationDelivery,
)

for model in (Team, TeamMembership, ChatAccess, ClientProjectAccess):
    admin.site.register(model, SuperuserAdmin)
for model in (
    AuditEvent,
    ProjectRevision,
    OutboxEvent,
    FactCandidate,
    NotificationDelivery,
):
    admin.site.register(model, ScopedReadOnlyAdmin)
