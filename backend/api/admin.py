from django.contrib import admin
from django.utils.html import format_html
from django.contrib import messages
import requests
from unfold.admin import ModelAdmin
from unfold.decorators import action

from api.models import (
    UserProfile, Company, Project, RawMessage, Commitment,
    FinancialRecord, BusinessEvent, WhatsAppConfig, AISettings, BitrixSettings
)

@admin.register(UserProfile)
class UserProfileAdmin(ModelAdmin):
    list_display = ('full_name', 'role', 'department', 'phone', 'monthly_target', 'current_sales', 'kpi_badge')
    search_fields = ('full_name', 'email', 'phone')
    list_filter = ('department', 'role')

    def kpi_badge(self, obj):
        pct = obj.kpi_percent
        color = "emerald" if pct >= 100 else ("amber" if pct >= 50 else "rose")
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-500">{}%</span>',
            color, color, pct
        )
    kpi_badge.short_description = "KPI Выполнение"


@admin.register(Company)
class CompanyAdmin(ModelAdmin):
    list_display = ('name', 'client_type', 'contact_person', 'phone', 'bitrix_company_id', 'created_at')
    list_filter = ('client_type',)
    search_fields = ('name', 'contact_person', 'phone', 'bitrix_company_id')


@admin.register(Project)
class ProjectAdmin(ModelAdmin):
    list_display = (
        'name', 'source', 'company', 'manager', 'contract_amount_fmt',
        'paid_amount_fmt', 'due_amount_fmt', 'margin_badge',
        'status', 'needs_bitrix_sync', 'priority', 'bitrix_link'
    )
    list_filter = ('source', 'needs_bitrix_sync', 'status', 'priority', 'project_type', 'manager')
    search_fields = ('name', 'normalized_name', 'contract_number', 'company__name', 'decision_maker', 'bitrix_id')
    readonly_fields = ('normalized_name', 'actual_margin_percent', 'due_amount', 'created_at', 'updated_at')

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
        color = "emerald" if margin >= 20 else ("amber" if margin >= 15 else "rose")
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-500">{:.1f}%</span>',
            color, color, margin
        )
    margin_badge.short_description = "Маржа"

    def bitrix_link(self, obj):
        if obj.bitrix_id:
            cfg = BitrixSettings.get_active()
            base_url = cfg.webhook_url.split('/rest/')[0] if '/rest/' in cfg.webhook_url else 'https://aquakip.bitrix24.kz'
            url = f"{base_url}/crm/deal/details/{obj.bitrix_id}/"
            return format_html('<a href="{}" target="_blank" class="text-indigo-600 font-semibold underline">#{}</a>', url, obj.bitrix_id)
        return "—"
    bitrix_link.short_description = "Bitrix24"


@admin.register(Commitment)
class CommitmentAdmin(ModelAdmin):
    list_display = ('commitment_text_snippet', 'project', 'manager', 'deadline', 'bitrix_task_id', 'status_badge', 'severity')
    list_filter = ('status', 'severity', 'manager')
    search_fields = ('commitment_text', 'counterparty_person', 'project__name', 'bitrix_task_id')

    def commitment_text_snippet(self, obj):
        return (obj.commitment_text[:60] + '...') if len(obj.commitment_text) > 60 else obj.commitment_text
    commitment_text_snippet.short_description = "Обещание / Задача"

    def status_badge(self, obj):
        colors = {
            'fulfilled': 'emerald',
            'pending': 'blue',
            'overdue': 'rose',
            'cancelled': 'gray',
        }
        color = colors.get(obj.status, 'gray')
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-500">{}</span>',
            color, color, obj.get_status_display()
        )
    status_badge.short_description = "Статус"


@admin.register(FinancialRecord)
class FinancialRecordAdmin(ModelAdmin):
    list_display = ('project', 'amount_fmt', 'payment_date', 'payment_type', 'status', 'created_at')
    list_filter = ('status', 'payment_type', 'payment_date')
    search_fields = ('project__name', 'notes')

    def amount_fmt(self, obj):
        return f"{obj.amount:,.2f} ₸"
    amount_fmt.short_description = "Сумма"


@admin.register(RawMessage)
class RawMessageAdmin(ModelAdmin):
    list_display = ('sender_name', 'sender_phone', 'timestamp', 'content_snippet', 'processed_badge')
    list_filter = ('processed', 'timestamp')
    search_fields = ('content', 'sender_name', 'sender_phone', 'message_id')
    readonly_fields = ('timestamp', 'created_at', 'raw_payload')

    def content_snippet(self, obj):
        return (obj.content[:80] + '...') if len(obj.content) > 80 else obj.content
    content_snippet.short_description = "Текст сообщения"

    def processed_badge(self, obj):
        if obj.processed:
            return format_html(
                '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-emerald-500/10 text-emerald-500">{}</span>',
                '✓ Обработано'
            )
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-amber-500/10 text-amber-500">{}</span>',
            'Ожидает'
        )
    processed_badge.short_description = "Статус обработки"


@admin.register(BusinessEvent)
class BusinessEventAdmin(ModelAdmin):
    list_display = ('title', 'project', 'manager', 'timestamp', 'severity')
    list_filter = ('severity', 'event_type')
    search_fields = ('title', 'description', 'project__name')


@admin.register(WhatsAppConfig)
class WhatsAppConfigAdmin(ModelAdmin):
    list_display = ('name', 'group_jid', 'session_name', 'waha_api_url', 'status', 'is_active', 'updated_at')
    list_editable = ('group_jid', 'is_active')
    actions = ['test_waha_connection']

    @action(description="Проверить связь с WAHA")
    def test_waha_connection(self, request, queryset):
        for cfg in queryset:
            try:
                headers = {"X-Api-Key": cfg.waha_api_key} if cfg.waha_api_key else {}
                url = f"{cfg.waha_api_url.rstrip('/')}/api/sessions/{cfg.session_name or 'default'}"
                res = requests.get(url, headers=headers, timeout=5)
                if res.status_code == 200:
                    status = res.json().get('status', 'OK')
                    cfg.status = status
                    cfg.save(update_fields=['status'])
                    messages.success(request, f"WAHA ответ получен: статус {status}")
                else:
                    messages.warning(request, f"WAHA код ответа: {res.status_code} ({res.text})")
            except Exception as e:
                messages.error(request, f"Не удалось подключиться к WAHA ({cfg.waha_api_url}): {e}")


@admin.register(AISettings)
class AISettingsAdmin(ModelAdmin):
    list_display = ('name', 'chat_model_name', 'embedding_model_name', 'embedding_dimension', 'is_active', 'updated_at')
    list_editable = ('chat_model_name', 'embedding_model_name', 'is_active')
    fieldsets = (
        ("Общие настройки", {
            "fields": ("name", "is_active")
        }),
        ("Embeddings модель (Векторизация для Qdrant)", {
            "fields": ("embedding_provider_url", "embedding_model_name", "embedding_api_key", "embedding_dimension")
        }),
        ("Chat / Reasoning модель (Анализ сообщений и ассистент)", {
            "fields": ("chat_provider_url", "chat_model_name", "chat_api_key", "chat_temperature")
        }),
        ("Системные промпты", {
            "fields": ("system_prompt_worker", "system_prompt_assistant")
        }),
    )
    actions = ['test_models_connection']

    @action(description="Проверить подключение к моделям OpenRouter")
    def test_models_connection(self, request, queryset):
        for cfg in queryset:
            # 1. Test embedding
            try:
                emb_res = requests.post(
                    f"{cfg.embedding_provider_url.rstrip('/')}/embeddings",
                    json={"model": cfg.embedding_model_name, "input": "test"},
                    headers={"Authorization": f"Bearer {cfg.embedding_api_key}", "Content-Type": "application/json"},
                    timeout=10
                )
                if emb_res.status_code == 200:
                    dim = len(emb_res.json()["data"][0]["embedding"])
                    messages.success(request, f"Embeddings ({cfg.embedding_model_name}) работает! Размерность вектора: {dim}")
                else:
                    messages.error(request, f"Embeddings ошибка: {emb_res.status_code} {emb_res.text}")
            except Exception as e:
                messages.error(request, f"Embeddings исключение: {e}")

            # 2. Test chat
            try:
                chat_res = requests.post(
                    f"{cfg.chat_provider_url.rstrip('/')}/chat/completions",
                    json={"model": cfg.chat_model_name, "messages": [{"role": "user", "content": "ping"}]},
                    headers={"Authorization": f"Bearer {cfg.chat_api_key}", "Content-Type": "application/json"},
                    timeout=15
                )
                if chat_res.status_code == 200:
                    messages.success(request, f"Chat LLM ({cfg.chat_model_name}) отвечает штатно!")
                else:
                    messages.warning(request, f"Chat LLM ответ: {chat_res.status_code} ({chat_res.text[:120]})")
            except Exception as e:
                messages.error(request, f"Chat LLM исключение: {e}")


@admin.register(BitrixSettings)
class BitrixSettingsAdmin(ModelAdmin):
    list_display = (
        'name', 'webhook_url_masked', 'is_active', 'hourly_sync_enabled',
        'auto_import_deals', 'auto_create_tasks', 'last_hourly_sync_at', 'updated_at'
    )
    list_editable = ('is_active', 'hourly_sync_enabled', 'auto_import_deals', 'auto_create_tasks')
    actions = ['test_bitrix_connection', 'run_hourly_sync_now', 'run_deduplication_now']

    def webhook_url_masked(self, obj):
        if not obj.webhook_url:
            return "—"
        parts = obj.webhook_url.split('/')
        if len(parts) >= 6:
            return f"{parts[0]}//{parts[2]}/rest/.../{parts[-2][:4]}***"
        return obj.webhook_url
    webhook_url_masked.short_description = "Webhook"

    @action(description="Проверить связь с Битрикс24 (app.info)")
    def test_bitrix_connection(self, request, queryset):
        for cfg in queryset:
            try:
                url = f"{cfg.webhook_url.rstrip('/')}/app.info"
                res = requests.get(url, timeout=10)
                if res.status_code == 200:
                    data = res.json()
                    scopes = data.get("result", {}).get("SCOPE", [])
                    messages.success(request, f"Битрикс24 успешно ответил! Доступные права (SCOPE): {', '.join(scopes)}")
                else:
                    messages.error(request, f"Битрикс24 ошибка: HTTP {res.status_code}")
            except Exception as e:
                messages.error(request, f"Не удалось связаться с Битрикс24: {e}")

    @action(description="Запустить синхронизацию очереди прямо сейчас (Django Q2)")
    def run_hourly_sync_now(self, request, queryset):
        from .tasks import enqueue_hourly_bitrix_sync_task
        from django_q.tasks import async_task
        task_id = async_task(enqueue_hourly_bitrix_sync_task)
        messages.success(request, f"Синхронизация поставлена в очередь Redis (Task ID: {task_id}).")

    @action(description="Найти и устранить дубликаты сделок в Bitrix24 (Deduplicate)")
    def run_deduplication_now(self, request, queryset):
        from .bitrix_service import BitrixService
        try:
            report = BitrixService.clean_duplicate_deals(dry_run=False)
            deleted = report.get("deleted_count", 0)
            groups = report.get("duplicate_groups_count", 0)
            messages.success(request, f"Дедупликация завершена: найдено {groups} групп дублей, удалено {deleted} лишних сделок в Bitrix24.")
        except Exception as e:
            messages.error(request, f"Ошибка дедупликации: {e}")

