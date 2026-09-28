import json
import requests
from django.contrib import admin
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.contrib import messages
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action

from api.models import (
    UserProfile, Company, Project, RawMessage, Commitment,
    FinancialRecord, BusinessEvent, WhatsAppConfig, AISettings, BitrixSettings,
    BitrixDealChangeLog, MessageProcessingTrace
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


class BitrixDealChangeLogInLine(TabularInline):
    model = BitrixDealChangeLog
    extra = 0
    can_delete = False
    readonly_fields = ('created_at_fmt', 'action_badge', 'status_badge', 'changed_fields_summary', 'duration_fmt', 'bitrix_deal_link')
    fields = ('created_at_fmt', 'action_badge', 'status_badge', 'changed_fields_summary', 'duration_fmt', 'bitrix_deal_link')

    def has_add_permission(self, request, obj=None):
        return False

    def created_at_fmt(self, obj):
        return obj.created_at.strftime('%d.%m.%Y %H:%M:%S') if obj.created_at else '—'
    created_at_fmt.short_description = "Дата и время"

    def action_badge(self, obj):
        color = "sky" if obj.action == 'create' else "indigo"
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-500">{}</span>',
            color, color, obj.get_action_display()
        )
    action_badge.short_description = "Действие"

    def status_badge(self, obj):
        color = "emerald" if obj.status == 'success' else "rose"
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-500">{}</span>',
            color, color, obj.get_status_display()
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
            base_url = cfg.webhook_url.split('/rest/')[0] if '/rest/' in cfg.webhook_url else 'https://aquakip.bitrix24.kz'
            url = f"{base_url}/crm/deal/details/{obj.bitrix_deal_id}/"
            return format_html('<a href="{}" target="_blank" class="text-indigo-600 font-semibold underline">#{}</a>', url, obj.bitrix_deal_id)
        return "—"
    bitrix_deal_link.short_description = "Bitrix24"


class MessageProcessingTraceInLine(TabularInline):
    model = MessageProcessingTrace
    extra = 0
    can_delete = False
    readonly_fields = (
        'created_at_fmt', 'whatsapp_sender_fmt', 'whatsapp_content_snippet',
        'earlier_messages_badge', 'bitrix_matched_badge', 'pipeline_action_badge', 'trace_link'
    )
    fields = (
        'created_at_fmt', 'whatsapp_sender_fmt', 'whatsapp_content_snippet',
        'earlier_messages_badge', 'bitrix_matched_badge', 'pipeline_action_badge', 'trace_link'
    )

    def has_add_permission(self, request, obj=None):
        return False

    def created_at_fmt(self, obj):
        return obj.created_at.strftime('%d.%m.%Y %H:%M:%S') if obj.created_at else '—'
    created_at_fmt.short_description = "Дата и время"

    def whatsapp_sender_fmt(self, obj):
        phone_part = f" ({obj.whatsapp_sender_phone})" if obj.whatsapp_sender_phone else ""
        return f"{obj.whatsapp_sender_name}{phone_part}"
    whatsapp_sender_fmt.short_description = "Отправитель"

    def whatsapp_content_snippet(self, obj):
        text = obj.whatsapp_content or ""
        return (text[:60] + "...") if len(text) > 60 else text
    whatsapp_content_snippet.short_description = "Сообщение WhatsApp"

    def earlier_messages_badge(self, obj):
        count = obj.earlier_messages_count or len(obj.earlier_messages_context or [])
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-purple-500/10 text-purple-500">{} в RAG</span>',
            count
        )
    earlier_messages_badge.short_description = "История"

    def bitrix_matched_badge(self, obj):
        if obj.bitrix_matched_deal_id:
            return format_html(
                '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-blue-500/10 text-blue-500">Сделка #{}</span>',
                obj.bitrix_matched_deal_id
            )
        return mark_safe('<span class="text-xs text-gray-400">В CRM нет</span>')
    bitrix_matched_badge.short_description = "Bitrix24"

    def pipeline_action_badge(self, obj):
        colors = {
            'created_deal': 'emerald',
            'updated_deal': 'sky',
            'matched_bitrix_imported': 'indigo',
            'commitment_created': 'amber',
            'financial_record_created': 'emerald',
            'non_commercial': 'gray',
            'error': 'rose',
        }
        color = colors.get(obj.pipeline_action, 'gray')
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-500">{}</span>',
            color, color, obj.get_pipeline_action_display()
        )
    pipeline_action_badge.short_description = "Действие"

    def trace_link(self, obj):
        url = f"/admin/api/messageprocessingtrace/{obj.id}/change/"
        return format_html('<a href="{}" class="text-indigo-600 font-semibold underline">Открыть трассировку &rarr;</a>', url)
    trace_link.short_description = "Трассировка"


@admin.register(Project)
class ProjectAdmin(ModelAdmin):
    list_display = (
        'name', 'verified_badge', 'source', 'company', 'manager', 'contract_amount_fmt',
        'paid_amount_fmt', 'due_amount_fmt', 'margin_badge',
        'status', 'needs_bitrix_sync', 'priority', 'bitrix_link'
    )
    list_filter = ('is_verified', 'source', 'needs_bitrix_sync', 'status', 'priority', 'project_type', 'manager')
    search_fields = ('name', 'normalized_name', 'contract_number', 'company__name', 'decision_maker', 'bitrix_id')
    readonly_fields = ('normalized_name', 'actual_margin_percent', 'due_amount', 'created_at', 'updated_at')
    inlines = [MessageProcessingTraceInLine, BitrixDealChangeLogInLine]
    actions = ['mark_as_verified', 'mark_as_unverified']

    def verified_badge(self, obj):
        if obj.is_verified:
            return mark_safe(
                '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-emerald-500/10 text-emerald-500">✓ Проверено</span>'
            )
        return mark_safe(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-amber-500/10 text-amber-500">⏳ Требует проверки</span>'
        )
    verified_badge.short_description = "Проверено"

    @action(description="Отметить как проверенные (включить в аналитику и синхронизацию)")
    def mark_as_verified(self, request, queryset):
        from django_q.tasks import async_task
        count = 0
        for proj in queryset:
            proj.is_verified = True
            proj.save(update_fields=['is_verified'])
            if not proj.bitrix_id:
                async_task('api.tasks.create_bitrix_deal_task', proj.id)
            elif proj.needs_bitrix_sync:
                async_task('api.tasks.sync_single_deal_to_bitrix_task', proj.id)
            count += 1
        self.message_user(request, f"Успешно проверено и отправлено в обработку сделок: {count}", messages.SUCCESS)

    @action(description="Снять отметку проверки (исключить из аналитики)")
    def mark_as_unverified(self, request, queryset):
        updated = queryset.update(is_verified=False)
        self.message_user(request, f"Снята отметка проверки для {updated} сделок", messages.WARNING)

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
        margin_str = f"{margin:.1f}%"
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-500">{}</span>',
            color, color, margin_str
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
    list_display = ('commitment_text_snippet', 'verified_badge', 'project', 'manager', 'deadline', 'bitrix_task_id', 'status_badge', 'severity')
    list_filter = ('is_verified', 'status', 'severity', 'manager')
    search_fields = ('commitment_text', 'counterparty_person', 'project__name', 'bitrix_task_id')
    actions = ['mark_as_verified', 'mark_as_unverified']

    def verified_badge(self, obj):
        if obj.is_verified:
            return mark_safe(
                '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-emerald-500/10 text-emerald-500">✓ Проверено</span>'
            )
        return mark_safe(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-amber-500/10 text-amber-500">⏳ Требует проверки</span>'
        )
    verified_badge.short_description = "Проверено"

    @action(description="Отметить как проверенные (включить в SLA)")
    def mark_as_verified(self, request, queryset):
        updated = queryset.update(is_verified=True)
        self.message_user(request, f"Успешно подтверждено обязательств: {updated}", messages.SUCCESS)

    @action(description="Снять отметку проверки (исключить из SLA)")
    def mark_as_unverified(self, request, queryset):
        updated = queryset.update(is_verified=False)
        self.message_user(request, f"Снята отметка проверки для {updated} обязательств", messages.WARNING)

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
    list_display = ('project', 'verified_badge', 'amount_fmt', 'payment_date', 'payment_type', 'status', 'created_at')
    list_filter = ('is_verified', 'status', 'payment_type', 'payment_date')
    search_fields = ('project__name', 'notes')
    actions = ['mark_as_verified', 'mark_as_unverified']

    def verified_badge(self, obj):
        if obj.is_verified:
            return mark_safe(
                '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-emerald-500/10 text-emerald-500">✓ Проверено</span>'
            )
        return mark_safe(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-amber-500/10 text-amber-500">⏳ Требует проверки</span>'
        )
    verified_badge.short_description = "Проверено"

    @action(description="Отметить как проверенные (включить в план-факт оплат)")
    def mark_as_verified(self, request, queryset):
        updated = queryset.update(is_verified=True)
        self.message_user(request, f"Успешно подтверждено финансовых записей: {updated}", messages.SUCCESS)

    @action(description="Снять отметку проверки (исключить из плана-факта)")
    def mark_as_unverified(self, request, queryset):
        updated = queryset.update(is_verified=False)
        self.message_user(request, f"Снята отметка проверки для {updated} записей оплат", messages.WARNING)

    def amount_fmt(self, obj):
        return f"{obj.amount:,.2f} ₸"
    amount_fmt.short_description = "Сумма"


@admin.register(RawMessage)
class RawMessageAdmin(ModelAdmin):
    list_display = ('sender_name', 'sender_phone', 'timestamp', 'content_snippet', 'processed_badge', 'pipeline_trace_link')
    list_filter = ('processed', 'timestamp')
    search_fields = ('content', 'sender_name', 'sender_phone', 'message_id')
    readonly_fields = ('timestamp', 'created_at', 'raw_payload', 'pipeline_trace_link')
    actions = ['reprocess_message_pipeline']

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

    def pipeline_trace_link(self, obj):
        trace = getattr(obj, 'traces', None)
        trace_obj = trace.first() if trace else None
        if trace_obj:
            url = f"/admin/api/messageprocessingtrace/{trace_obj.id}/change/"
            return format_html('<a href="{}" class="px-2 py-0.5 text-xs font-semibold rounded-full bg-indigo-500/10 text-indigo-600 underline">Цепочка (#{}) &rarr;</a>', url, trace_obj.id)
        return mark_safe('<span class="text-xs text-gray-400">Нет трассировки</span>')
    pipeline_trace_link.short_description = "Трассировка пайплайна"

    @action(description="Сформировать/перезапустить трассировку пайплайна")
    def reprocess_message_pipeline(self, request, queryset):
        from api.tasks import process_incoming_message_task
        count = 0
        for msg in queryset:
            payload = msg.raw_payload or {
                "message_id": msg.message_id,
                "content": msg.content,
                "sender_name": msg.sender_name,
                "sender_phone": msg.sender_phone,
                "chat_id": msg.chat_id,
            }
            process_incoming_message_task(payload)
            count += 1
        self.message_user(request, f"Успешно сформировано трассировок: {count}", messages.SUCCESS)


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


@admin.register(BitrixDealChangeLog)
class BitrixDealChangeLogAdmin(ModelAdmin):
    list_display = (
        'created_at_fmt', 'bitrix_deal_link', 'project_link',
        'action_badge', 'status_badge', 'changed_fields_summary',
        'duration_fmt', 'triggered_by'
    )
    list_filter = ('status', 'action', 'created_at')
    search_fields = ('bitrix_deal_id', 'project__name', 'error_message', 'triggered_by')
    readonly_fields = (
        'project', 'bitrix_deal_id', 'action', 'status', 'created_at',
        'duration_ms', 'triggered_by', 'error_message', 'formatted_payload', 'formatted_response', 'changed_fields'
    )
    fields = (
        'created_at', 'status', 'action', 'bitrix_deal_id', 'project',
        'duration_ms', 'triggered_by', 'error_message',
        'formatted_payload', 'formatted_response'
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def created_at_fmt(self, obj):
        return obj.created_at.strftime('%d.%m.%Y %H:%M:%S') if obj.created_at else '—'
    created_at_fmt.short_description = "Время отправки"

    def bitrix_deal_link(self, obj):
        if obj.bitrix_deal_id:
            cfg = BitrixSettings.get_active()
            base_url = cfg.webhook_url.split('/rest/')[0] if '/rest/' in cfg.webhook_url else 'https://aquakip.bitrix24.kz'
            url = f"{base_url}/crm/deal/details/{obj.bitrix_deal_id}/"
            return format_html('<a href="{}" target="_blank" class="text-indigo-600 font-semibold underline">#{}</a>', url, obj.bitrix_deal_id)
        return "—"
    bitrix_deal_link.short_description = "Сделка Bitrix24"

    def project_link(self, obj):
        if obj.project:
            url = f"/admin/api/project/{obj.project.id}/change/"
            return format_html('<a href="{}" class="text-indigo-600 font-semibold underline">{}</a>', url, obj.project.name)
        return "—"
    project_link.short_description = "Объект Mazory"

    def action_badge(self, obj):
        color = "sky" if obj.action == 'create' else "indigo"
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-500">{}</span>',
            color, color, obj.get_action_display()
        )
    action_badge.short_description = "Действие"

    def status_badge(self, obj):
        color = "emerald" if obj.status == 'success' else "rose"
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-500">{}</span>',
            color, color, obj.get_status_display()
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
        return format_html('<pre class="bg-gray-900 text-gray-100 p-4 rounded-lg overflow-x-auto text-xs font-mono"><code>{}</code></pre>', formatted)
    formatted_payload.short_description = "Отправленные данные (Payload)"

    def formatted_response(self, obj):
        formatted = json.dumps(obj.response_data or {}, indent=2, ensure_ascii=False)
        return format_html('<pre class="bg-gray-900 text-gray-100 p-4 rounded-lg overflow-x-auto text-xs font-mono"><code>{}</code></pre>', formatted)
    formatted_response.short_description = "Ответ Bitrix24 REST API"


@admin.register(MessageProcessingTrace)
class MessageProcessingTraceAdmin(ModelAdmin):
    class Media:
        css = {
            'all': ('mazory/css/admin_trace.css',)
        }

    list_display = (
        'created_at_fmt', 'whatsapp_sender_fmt', 'whatsapp_content_snippet',
        'earlier_messages_badge', 'bitrix_matched_badge', 'pipeline_action_badge',
        'project_link', 'status_badge'
    )
    list_filter = ('pipeline_action', 'status', 'created_at')
    search_fields = (
        'whatsapp_content', 'whatsapp_sender_name', 'whatsapp_sender_phone',
        'bitrix_matched_deal_id', 'project__name', 'whatsapp_message_id'
    )
    readonly_fields = (
        'pipeline_overview_banner', 'created_at_fmt', 'pipeline_action_badge',
        'status_badge', 'result_summary_fmt', 'stage_1_whatsapp_card',
        'stage_2_earlier_messages_card', 'stage_3_bitrix_card', 'stage_4_final_record_card'
    )
    fieldsets = (
        ("Сквозная цепочка пайплайна (WhatsApp → Сообщения ранее → Bitrix24 → Итог)", {
            "fields": ("pipeline_overview_banner", "result_summary_fmt")
        }),
        ("Этап 1: «Входные данные WhatsApp»", {
            "fields": ("stage_1_whatsapp_card",)
        }),
        ("Этап 2: «Зависимые данные из сообщений ранее» (Qdrant RAG / Чат)", {
            "fields": ("stage_2_earlier_messages_card",)
        }),
        ("Этап 3: «Зависимые данные из Bitrix24» (CRM Поиск & Синхронизация)", {
            "fields": ("stage_3_bitrix_card",)
        }),
        ("Этап 4: «Итоговая запись» (AI Факты & Созданные сущности)", {
            "fields": ("stage_4_final_record_card",)
        }),
    )
    actions = ['reprocess_traces']

    def has_add_permission(self, request):
        return False

    def created_at_fmt(self, obj):
        return obj.created_at.strftime('%d.%m.%Y %H:%M:%S') if obj.created_at else '—'
    created_at_fmt.short_description = "Время обработки"

    def whatsapp_sender_fmt(self, obj):
        phone_part = f"<br><span class='text-xs text-gray-400 font-mono'>{obj.whatsapp_sender_phone}</span>" if obj.whatsapp_sender_phone else ""
        return format_html("<b>{}</b>{}", obj.whatsapp_sender_name, mark_safe(phone_part))
    whatsapp_sender_fmt.short_description = "Отправитель"

    def whatsapp_content_snippet(self, obj):
        text = obj.whatsapp_content or ""
        snippet = (text[:75] + "...") if len(text) > 75 else text
        return format_html('<span title="{}">{}</span>', text, snippet)
    whatsapp_content_snippet.short_description = "Текст сообщения"

    def earlier_messages_badge(self, obj):
        count = obj.earlier_messages_count or len(obj.earlier_messages_context or [])
        if count > 0:
            return format_html(
                '<span class="px-2.5 py-1 text-xs font-semibold rounded-full bg-purple-500/10 text-purple-600 border border-purple-500/20">🔍 {} сондай</span>',
                count
            )
        return mark_safe('<span class="text-xs text-gray-400">0 контекста</span>')
    earlier_messages_badge.short_description = "Сообщения ранее"

    def bitrix_matched_badge(self, obj):
        if obj.bitrix_matched_deal_id:
            cfg = BitrixSettings.get_active()
            base_url = cfg.webhook_url.split('/rest/')[0] if '/rest/' in cfg.webhook_url else 'https://aquakip.bitrix24.kz'
            url = f"{base_url}/crm/deal/details/{obj.bitrix_matched_deal_id}/"
            return format_html(
                '<a href="{}" target="_blank" class="px-2.5 py-1 text-xs font-semibold rounded-full bg-blue-500/10 text-blue-600 border border-blue-500/20 hover:underline">CRM #{} ↗</a>',
                url, obj.bitrix_matched_deal_id
            )
        return mark_safe('<span class="px-2 py-0.5 text-xs text-gray-500 bg-gray-100 dark:bg-gray-800 rounded">В CRM нет</span>')
    bitrix_matched_badge.short_description = "Bitrix24 CRM"

    def pipeline_action_badge(self, obj):
        colors = {
            'created_deal': ('emerald', '✓ Создана сделка'),
            'updated_deal': ('sky', '⟳ Обновлена сделка'),
            'matched_bitrix_imported': ('indigo', '📥 Импорт из CRM'),
            'commitment_created': ('amber', '⏱ Обязательство'),
            'financial_record_created': ('teal', '₸ Оплата'),
            'non_commercial': ('gray', 'ℹ Инфо-сообщение'),
            'error': ('rose', '⚠ Ошибка'),
        }
        color, label = colors.get(obj.pipeline_action, ('gray', obj.get_pipeline_action_display()))
        return format_html(
            '<span class="px-2.5 py-1 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-600 border border-{}-500/20">{}</span>',
            color, color, color, label
        )
    pipeline_action_badge.short_description = "Итоговое действие"

    def project_link(self, obj):
        if obj.project:
            url = f"/admin/api/project/{obj.project.id}/change/"
            verified_mark = "✓ " if obj.project.is_verified else "⏳ "
            amt_num = float(obj.project.contract_amount or 0)
            amt_str = f" ({amt_num:,.0f} ₸)"
            return format_html(
                '<a href="{}" class="font-medium text-indigo-600 hover:underline">{}<span class="text-xs text-gray-500">{}</span></a>',
                url, verified_mark + obj.project.name, amt_str
            )
        return mark_safe('<span class="text-xs text-gray-400">—</span>')
    project_link.short_description = "Итоговая сделка"

    def status_badge(self, obj):
        color = "emerald" if obj.status == 'success' else ("amber" if obj.status == 'warning' else "rose")
        return format_html(
            '<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-{}-500/10 text-{}-500">{}</span>',
            color, color, obj.get_status_display()
        )
    status_badge.short_description = "Статус"

    def result_summary_fmt(self, obj):
        lines = (obj.result_summary or "").split("\n")
        items_html = "".join([f"<li class='py-1 flex items-start gap-2'><span class='text-indigo-500 font-bold'>•</span><span class='mazory-trace-text'>{line}</span></li>" for line in lines if line.strip()])
        return format_html(
            '<div class="p-4 mazory-trace-card rounded-xl border font-sans text-sm mazory-trace-text shadow-sm"><ul class="space-y-1">{}</ul></div>',
            mark_safe(items_html) if items_html else mark_safe("<em>Нет текстового резюме</em>")
        )
    result_summary_fmt.short_description = "Резюме цепочки принятия решений"

    def pipeline_overview_banner(self, obj):
        """Интерактивный 4-шаговый визуальный прогресс пайплайна"""
        s1_title = f"{obj.whatsapp_sender_name}"
        s2_count = obj.earlier_messages_count or len(obj.earlier_messages_context or [])
        s2_sub = f"{s2_count} сообщений в RAG" if s2_count else "Новый контекст"
        s3_sub = f"CRM #{obj.bitrix_matched_deal_id}" if obj.bitrix_matched_deal_id else "В CRM отсутствует"
        s4_sub = obj.get_pipeline_action_display()

        html = f"""
        <div class="w-full my-3 p-5 rounded-2xl bg-gradient-to-r from-gray-900 via-indigo-950 to-gray-900 text-white shadow-lg border border-indigo-900/40">
            <div class="text-xs font-mono uppercase tracking-wider text-indigo-400 mb-3 flex items-center justify-between">
                <span>Data Lineage Audit Trail</span>
                <span>ID Трассировки: #{obj.id}</span>
            </div>
            <div class="grid grid-cols-1 md:grid-cols-4 gap-4 relative">
                <!-- Step 1 -->
                <div class="p-3 rounded-xl bg-white/5 border border-emerald-500/30 flex flex-col justify-between">
                    <div class="flex items-center gap-2 text-emerald-400 font-semibold text-xs uppercase">
                        <span class="w-5 h-5 rounded-full bg-emerald-500/20 flex items-center justify-center text-xs">1</span>
                        WhatsApp Вход
                    </div>
                    <div class="mt-2 text-sm font-bold truncate text-white">{s1_title}</div>
                    <div class="text-xs text-gray-400 font-mono mt-0.5">{obj.whatsapp_sender_phone or 'Прямой вебхук'}</div>
                </div>

                <!-- Step 2 -->
                <div class="p-3 rounded-xl bg-white/5 border border-purple-500/30 flex flex-col justify-between">
                    <div class="flex items-center gap-2 text-purple-400 font-semibold text-xs uppercase">
                        <span class="w-5 h-5 rounded-full bg-purple-500/20 flex items-center justify-center text-xs">2</span>
                        Контекст ранее
                    </div>
                    <div class="mt-2 text-sm font-bold text-white">{s2_sub}</div>
                    <div class="text-xs text-purple-300 mt-0.5">Векторный поиск Qdrant</div>
                </div>

                <!-- Step 3 -->
                <div class="p-3 rounded-xl bg-white/5 border border-sky-500/30 flex flex-col justify-between">
                    <div class="flex items-center gap-2 text-sky-400 font-semibold text-xs uppercase">
                        <span class="w-5 h-5 rounded-full bg-sky-500/20 flex items-center justify-center text-xs">3</span>
                        Данные Bitrix24
                    </div>
                    <div class="mt-2 text-sm font-bold text-white truncate">{s3_sub}</div>
                    <div class="text-xs text-sky-300 mt-0.5 truncate">{obj.bitrix_deal_title or 'Поиск по объекту'}</div>
                </div>

                <!-- Step 4 -->
                <div class="p-3 rounded-xl bg-white/5 border border-amber-500/30 flex flex-col justify-between">
                    <div class="flex items-center gap-2 text-amber-400 font-semibold text-xs uppercase">
                        <span class="w-5 h-5 rounded-full bg-amber-500/20 flex items-center justify-center text-xs">4</span>
                        Итоговая запись
                    </div>
                    <div class="mt-2 text-sm font-bold text-white truncate">{s4_sub}</div>
                    <div class="text-xs text-amber-300 mt-0.5 truncate">{obj.project.name if obj.project else 'Связанная сущность'}</div>
                </div>
            </div>
        </div>
        """
        return mark_safe(html)
    pipeline_overview_banner.short_description = "Сквозной процесс обработки"

    def stage_1_whatsapp_card(self, obj):
        """Рендеринг Этапа 1: «Входные данные WhatsApp»"""
        raw_json = json.dumps(obj.whatsapp_raw_payload or {}, indent=2, ensure_ascii=False)
        ts_str = obj.whatsapp_timestamp.strftime('%d.%m.%Y %H:%M:%S') if obj.whatsapp_timestamp else '—'
        html = f"""
        <style>
        .mazory-trace-content {{ background-color: #ffffff !important; border: 1px solid #e2e8f0 !important; color: #0f172a !important; }}
        html.dark .mazory-trace-content, body.dark .mazory-trace-content, .dark .mazory-trace-content {{ background-color: #0f172a !important; border-color: #334155 !important; color: #f8fafc !important; }}
        .mazory-trace-card {{ background-color: #ffffff !important; border: 1px solid #e2e8f0 !important; color: #0f172a !important; }}
        html.dark .mazory-trace-card, body.dark .mazory-trace-card, .dark .mazory-trace-card {{ background-color: #1e293b !important; border-color: #334155 !important; color: #f8fafc !important; }}
        .mazory-trace-text {{ color: #0f172a !important; }}
        html.dark .mazory-trace-text, body.dark .mazory-trace-text, .dark .mazory-trace-text {{ color: #f8fafc !important; }}
        .mazory-trace-meta {{ color: #475569 !important; }}
        html.dark .mazory-trace-meta, body.dark .mazory-trace-meta, .dark .mazory-trace-meta {{ color: #94a3b8 !important; }}
        .mazory-trace-label {{ color: #64748b !important; }}
        html.dark .mazory-trace-label, body.dark .mazory-trace-label, .dark .mazory-trace-label {{ color: #94a3b8 !important; }}
        </style>
        <div class="p-5 rounded-xl border border-emerald-500/30 bg-emerald-50/10 dark:bg-emerald-950/10 space-y-4">
            <div class="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-emerald-500/20">
                <div class="flex items-center gap-3">
                    <span class="p-2 rounded-lg bg-emerald-500/20 text-emerald-600 font-bold text-sm">WhatsApp</span>
                    <div>
                        <div class="text-sm font-bold mazory-trace-text">{obj.whatsapp_sender_name}</div>
                        <div class="text-xs mazory-trace-meta font-mono">{obj.whatsapp_sender_phone or 'Номер скрыт'}</div>
                    </div>
                </div>
                <div class="text-right text-xs mazory-trace-meta">
                    <div><b>Время получения:</b> {ts_str}</div>
                    <div><b>Чат / Группа:</b> <span class="font-mono text-indigo-500 font-semibold">{obj.whatsapp_chat_id or 'sales-group'}</span></div>
                    <div><b>Message ID:</b> <span class="font-mono mazory-trace-label">{obj.whatsapp_message_id}</span></div>
                </div>
            </div>

            <div>
                <div class="text-xs font-semibold mazory-trace-meta uppercase tracking-wider mb-1">Исходный текст сообщения WhatsApp:</div>
                <div class="p-4 rounded-lg text-sm font-sans whitespace-pre-wrap leading-relaxed shadow-sm mazory-trace-content">
{obj.whatsapp_content or '—'}
                </div>
            </div>

            <details class="text-xs mazory-trace-meta cursor-pointer pt-2">
                <summary class="font-semibold text-emerald-600 hover:underline">Показать сырой payload сообщения (WAHA Webhook JSON)</summary>
                <pre class="mt-2 p-3 bg-gray-950 text-gray-200 rounded-lg overflow-x-auto font-mono text-xs max-h-60"><code>{raw_json}</code></pre>
            </details>
        </div>
        """
        return mark_safe(html)
    stage_1_whatsapp_card.short_description = "1. Входные данные WhatsApp"

    def stage_2_earlier_messages_card(self, obj):
        """Рендеринг Этапа 2: «Зависимые данные из сообщений ранее»"""
        messages = obj.earlier_messages_context or []
        if not messages:
            return format_html(
                '<div class="p-4 rounded-xl border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-900/50 text-sm mazory-trace-meta">'
                'ℹ Зависимые сообщения из истории не найдены. Это первичное сообщение по данному объекту / теме.'
                '</div>'
            )

        cards_html = []
        for idx, m in enumerate(messages, 1):
            score = float(m.get('score', 0.0))
            score_percent = f"{score * 100:.1f}%"
            score_color = "emerald" if score >= 0.75 else ("purple" if score >= 0.5 else "gray")
            sender = m.get('sender_name') or m.get('author') or 'Коллега'
            ts = m.get('timestamp') or '—'
            content = m.get('content') or m.get('text') or ''

            card = f"""
            <div class="p-3 rounded-lg mazory-trace-card border border-purple-500/20 shadow-sm space-y-1.5">
                <div class="flex items-center justify-between text-xs">
                    <span class="font-semibold mazory-trace-text flex items-center gap-1.5">
                        <span class="w-4 h-4 rounded-full bg-purple-500/20 text-purple-600 flex items-center justify-center text-[10px] font-bold">{idx}</span>
                        {sender}
                    </span>
                    <div class="flex items-center gap-2">
                        <span class="mazory-trace-meta font-mono">{ts}</span>
                        <span class="px-2 py-0.5 rounded text-[11px] font-bold bg-{score_color}-500/10 text-{score_color}-600">
                            Сходство: {score_percent}
                        </span>
                    </div>
                </div>
                <div class="text-xs mazory-trace-text font-sans whitespace-pre-wrap pl-5 border-l-2 border-purple-500/40">
                    {content}
                </div>
            </div>
            """
            cards_html.append(card)

        html = f"""
        <div class="p-5 rounded-xl border border-purple-500/30 bg-purple-50/10 dark:bg-purple-950/10 space-y-3">
            <div class="flex items-center justify-between pb-2 border-b border-purple-500/20">
                <div class="text-xs font-semibold text-purple-600 uppercase tracking-wider">
                    Семантический поиск контекста в Qdrant RAG (найдено: {len(messages)})
                </div>
                <div class="text-xs mazory-trace-meta">
                    Модель: <span class="font-mono text-purple-600">liquid/lfm-2.5-embedding-350m (1024 dim)</span>
                </div>
            </div>
            <div class="space-y-2">
                {"".join(cards_html)}
            </div>
        </div>
        """
        return mark_safe(html)
    stage_2_earlier_messages_card.short_description = "2. Зависимые данные из сообщений ранее"

    def stage_3_bitrix_card(self, obj):
        """Рендеринг Этапа 3: «Зависимые данные из Bitrix24»"""
        matched_id = obj.bitrix_matched_deal_id
        cfg = BitrixSettings.get_active()
        base_url = cfg.webhook_url.split('/rest/')[0] if '/rest/' in cfg.webhook_url else 'https://aquakip.bitrix24.kz'
        deal_url = f"{base_url}/crm/deal/details/{matched_id}/" if matched_id else "#"

        opp_str = f"{obj.bitrix_deal_opportunity:,.2f} ₸" if obj.bitrix_deal_opportunity else "—"
        raw_deal_json = json.dumps(obj.bitrix_raw_deal or {}, indent=2, ensure_ascii=False)

        if matched_id:
            deal_block = f"""
            <div class="p-4 rounded-xl mazory-trace-card border border-sky-500/30 shadow-sm space-y-3">
                <div class="flex items-center justify-between">
                    <div class="flex items-center gap-2">
                        <span class="px-2.5 py-1 text-xs font-bold rounded-lg bg-sky-500/20 text-sky-600">
                            Сделка в Bitrix24 найдена
                        </span>
                        <a href="{deal_url}" target="_blank" class="text-sm font-bold text-sky-600 hover:underline">
                            #{matched_id} — {obj.bitrix_deal_title or 'Сделка Bitrix24'} ↗
                        </a>
                    </div>
                    <span class="text-xs font-mono mazory-trace-meta">Стадия CRM: {obj.bitrix_deal_stage or 'PREPARATION'}</span>
                </div>

                <div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
                    <div class="p-2.5 rounded mazory-trace-content">
                        <span class="mazory-trace-meta block text-[11px]">Сумма в CRM:</span>
                        <span class="font-bold mazory-trace-text">{opp_str}</span>
                    </div>
                    <div class="p-2.5 rounded mazory-trace-content">
                        <span class="mazory-trace-meta block text-[11px]">Компания в CRM:</span>
                        <span class="font-bold mazory-trace-text">{obj.bitrix_company_data.get('company_name') or obj.bitrix_company_data.get('TITLE') or obj.bitrix_company_data.get('name') or 'ТОО / Не привязана'}</span>
                    </div>
                    <div class="p-2.5 rounded mazory-trace-content">
                        <span class="mazory-trace-meta block text-[11px]">Поисковый запрос:</span>
                        <span class="font-bold text-indigo-500 font-mono truncate block">{obj.bitrix_search_query or '—'}</span>
                    </div>
                    <div class="p-2.5 rounded mazory-trace-content">
                        <span class="mazory-trace-meta block text-[11px]">Защита от дублей:</span>
                        <span class="font-bold text-emerald-600">Связана существующая</span>
                    </div>
                </div>

                <details class="text-xs mazory-trace-meta cursor-pointer pt-1">
                    <summary class="font-semibold text-sky-600 hover:underline">Показать сырой ответ crm.deal.list/get (JSON)</summary>
                    <pre class="mt-2 p-3 bg-gray-950 text-gray-200 rounded-lg overflow-x-auto font-mono text-xs max-h-56"><code>{raw_deal_json}</code></pre>
                </details>
            </div>
            """
        else:
            deal_block = f"""
            <div class="p-4 rounded-xl mazory-trace-card border border-amber-500/30 shadow-sm space-y-2">
                <div class="flex items-center gap-2 text-xs font-semibold text-amber-600">
                    <span>⚠ В Bitrix24 CRM сделка по объекту «{obj.bitrix_search_query or '—'}» не найдена</span>
                </div>
                <div class="text-xs mazory-trace-meta">
                    Система проверила наличие сделки через <code>BitrixService.find_deal_by_name</code> по полям TITLE и кастомным свойствам объекта.
                    Так как совпадений нет, сделка создана локально как новая и подготовлена к первичной регистрации.
                </div>
            </div>
            """

        summary_text = obj.bitrix_known_deals_summary or "—"
        html = f"""
        <div class="p-5 rounded-xl border border-sky-500/30 bg-sky-50/10 dark:bg-sky-950/10 space-y-3">
            <div class="flex items-center justify-between pb-2 border-b border-sky-500/20">
                <div class="text-xs font-semibold text-sky-600 uppercase tracking-wider">
                    Состояние Bitrix24 CRM на момент обработки
                </div>
                <div class="text-xs mazory-trace-meta">
                    Webhook: <span class="font-mono text-sky-600">{cfg.name}</span>
                </div>
            </div>

            {deal_block}

            <details class="text-xs mazory-trace-meta cursor-pointer pt-1">
                <summary class="font-semibold text-sky-600 hover:underline">Сводка известных сделок компании, переданная в контекст AI (промпт)</summary>
                <div class="mt-2 p-3 rounded-lg mazory-trace-content text-xs font-mono max-h-48 overflow-y-auto whitespace-pre-wrap">
{summary_text}
                </div>
            </details>
        </div>
        """
        return mark_safe(html)
    stage_3_bitrix_card.short_description = "3. Зависимые данные из Bitrix24"

    def stage_4_final_record_card(self, obj):
        """Рендеринг Этапа 4: «Итоговая запись»"""
        facts = obj.ai_extracted_facts or {}
        facts_json = json.dumps(facts, indent=2, ensure_ascii=False)
        conf_pct = f"{obj.ai_confidence * 100:.0f}%"

        # Созданный/обновленный проект
        project_html = '<em class="mazory-trace-meta">Сделка не создавалась/не привязана</em>'
        if obj.project:
            p = obj.project
            p_url = f"/admin/api/project/{p.id}/change/"
            verified_badge = '<span class="text-emerald-500 font-bold">✓ Проверено</span>' if p.is_verified else '<span class="text-amber-500 font-bold">⏳ Ожидает проверки</span>'
            project_html = f"""
            <div class="p-3 rounded-lg mazory-trace-card border border-emerald-500/30 space-y-2">
                <div class="flex items-center justify-between">
                    <a href="{p_url}" class="text-sm font-bold text-indigo-600 hover:underline">
                        Проект #{p.id}: {p.name} ↗
                    </a>
                    {verified_badge}
                </div>
                <div class="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
                    <div><span class="mazory-trace-meta">Сумма:</span> <b class="mazory-trace-text">{p.contract_amount:,.2f} ₸</b></div>
                    <div><span class="mazory-trace-meta">Маржа:</span> <b class="mazory-trace-text">{p.actual_margin_percent}%</b></div>
                    <div><span class="mazory-trace-meta">Статус:</span> <b class="mazory-trace-text">{p.get_status_display()}</b></div>
                    <div><span class="mazory-trace-meta">Синхр. Bitrix:</span> <b class="mazory-trace-text">{'Да' if p.needs_bitrix_sync else 'Актуально'}</b></div>
                </div>
            </div>
            """

        # Созданное обязательство
        commitment_html = ""
        if obj.commitment:
            c = obj.commitment
            c_url = f"/admin/api/commitment/{c.id}/change/"
            commitment_html = f"""
            <div class="p-3 rounded-lg mazory-trace-card border border-amber-500/30 space-y-1 text-xs">
                <div class="flex items-center justify-between">
                    <span class="font-bold text-amber-600">Создано обязательство (SLA):</span>
                    <a href="{c_url}" class="text-indigo-600 hover:underline font-semibold">#{c.id} ↗</a>
                </div>
                <div class="mazory-trace-text font-medium">«{c.commitment_text}»</div>
                <div class="mazory-trace-meta flex gap-4">
                    <span>Дедлайн: <b class="mazory-trace-text">{c.deadline or '—'}</b></span>
                    <span>Статус: <b class="mazory-trace-text">{c.get_status_display()}</b></span>
                    <span>Срочность: <b class="mazory-trace-text">{c.get_severity_display()}</b></span>
                </div>
            </div>
            """

        # Созданная финансовая запись
        fin_html = ""
        if obj.financial_record:
            f = obj.financial_record
            f_url = f"/admin/api/financialrecord/{f.id}/change/"
            fin_html = f"""
            <div class="p-3 rounded-lg mazory-trace-card border border-teal-500/30 space-y-1 text-xs">
                <div class="flex items-center justify-between">
                    <span class="font-bold text-teal-600">Зафиксирована финансовая запись:</span>
                    <a href="{f_url}" class="text-indigo-600 hover:underline font-semibold">#{f.id} ↗</a>
                </div>
                <div class="mazory-trace-text font-medium">Сумма: <b class="mazory-trace-text">{f.amount:,.2f} ₸</b> ({f.get_payment_type_display()})</div>
                <div class="mazory-trace-meta">Дата: <b class="mazory-trace-text">{f.payment_date}</b> | Статус: <b class="mazory-trace-text">{f.get_status_display()}</b></div>
            </div>
            """

        html = f"""
        <div class="p-5 rounded-xl border border-emerald-500/30 bg-emerald-50/10 dark:bg-emerald-950/10 space-y-4">
            <div class="flex items-center justify-between pb-2 border-b border-emerald-500/20">
                <div class="text-xs font-semibold text-emerald-600 uppercase tracking-wider">
                    Результат обработки нейросетью и созданные записи
                </div>
                <div class="text-xs">
                    Уверенность AI: <span class="px-2 py-0.5 rounded font-bold bg-emerald-500/20 text-emerald-600">{conf_pct}</span>
                </div>
            </div>

            <!-- AI Facts table -->
            <div>
                <div class="text-xs font-semibold mazory-trace-meta uppercase tracking-wider mb-2">Извлеченные структурированные факты AI:</div>
                <div class="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
                    <div class="p-2.5 rounded mazory-trace-content">
                        <span class="mazory-trace-meta block text-[11px]">Объект:</span>
                        <span class="font-bold mazory-trace-text">{facts.get('object_name') or '—'}</span>
                    </div>
                    <div class="p-2.5 rounded mazory-trace-content">
                        <span class="mazory-trace-meta block text-[11px]">Сумма договора:</span>
                        <span class="font-bold text-emerald-600">{float(facts.get('contract_amount') or 0):,.2f} ₸</span>
                    </div>
                    <div class="p-2.5 rounded mazory-trace-content">
                        <span class="mazory-trace-meta block text-[11px]">Оборудование:</span>
                        <span class="font-bold mazory-trace-text">{facts.get('direction') or facts.get('equipment_type') or '—'}</span>
                    </div>
                    <div class="p-2.5 rounded mazory-trace-content">
                        <span class="mazory-trace-meta block text-[11px]">Стадия:</span>
                        <span class="font-bold mazory-trace-text">{facts.get('stage') or '—'}</span>
                    </div>
                    <div class="p-2.5 rounded mazory-trace-content">
                        <span class="mazory-trace-meta block text-[11px]">Следующий шаг:</span>
                        <span class="font-bold mazory-trace-text">{facts.get('next_action') or '—'}</span>
                    </div>
                    <div class="p-2.5 rounded mazory-trace-content">
                        <span class="mazory-trace-meta block text-[11px]">Дедлайн:</span>
                        <span class="font-bold mazory-trace-text">{facts.get('next_action_at') or '—'}</span>
                    </div>
                    <div class="p-2.5 rounded mazory-trace-content">
                        <span class="mazory-trace-meta block text-[11px]">Компания:</span>
                        <span class="font-bold mazory-trace-text">{facts.get('company_name') or '—'}</span>
                    </div>
                    <div class="p-2.5 rounded mazory-trace-content">
                        <span class="mazory-trace-meta block text-[11px]">Приоритет:</span>
                        <span class="font-bold mazory-trace-text">{facts.get('priority') or 'standard'}</span>
                    </div>
                </div>
            </div>

            <!-- Entities created -->
            <div class="space-y-2 pt-1">
                <div class="text-xs font-semibold mazory-trace-meta uppercase tracking-wider">Связанные сущности в БД Mazory:</div>
                {project_html}
                {commitment_html}
                {fin_html}
            </div>

            <details class="text-xs mazory-trace-meta cursor-pointer pt-1">
                <summary class="font-semibold text-emerald-600 hover:underline">Показать полный JSON ответ нейросети (facts)</summary>
                <pre class="mt-2 p-3 bg-gray-950 text-gray-200 rounded-lg overflow-x-auto font-mono text-xs max-h-56"><code>{facts_json}</code></pre>
            </details>
        </div>
        """
        return mark_safe(html)
    stage_4_final_record_card.short_description = "4. Итоговая запись"

    @action(description="Перезапустить обработку пайплайна для выбранных сообщений")
    def reprocess_traces(self, request, queryset):
        from api.tasks import process_incoming_message_task
        count = 0
        for trace in queryset:
            payload = trace.whatsapp_raw_payload or {
                "message_id": trace.whatsapp_message_id,
                "content": trace.whatsapp_content,
                "sender_name": trace.whatsapp_sender_name,
                "sender_phone": trace.whatsapp_sender_phone,
                "chat_id": trace.whatsapp_chat_id,
            }
            process_incoming_message_task(payload)
            count += 1
        self.message_user(request, f"Успешно перезапущено и обновлено трассировок: {count}", messages.SUCCESS)



