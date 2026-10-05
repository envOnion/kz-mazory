from collections import Counter
import json
from urllib.parse import quote, urlsplit

import requests

from django.contrib import admin
from django.db.models import F, Prefetch
from django.template.loader import render_to_string
from django.utils.html import format_html, format_html_join
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from .admin_forms import AISettingsForm, BitrixSettingsForm
from .user_admin import ProfileIdentityForm
from .bitrix_config import effective_webhook_url, masked_webhook_url
from .trace_context_ui import badge_text, render_context, context_view_data, retry_view, ERRORS
from django.utils.safestring import mark_safe
from django.contrib import messages
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action

from .admin_access import ScopedReadOnlyAdmin, IntegrationAdmin, SuperuserAdmin
from .providers import ProviderUnavailable, checked_url
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
    CandidateCrmMatch,
    FactCandidate,
    MessageProcessingTrace,
    DialogueThread,
    ThreadMessage,
)


CRM_MATCH_STATE_UI = {
    "not_requested": {
        "label": "Нет данных о CRM-проверке",
        "description": "CRM-сопоставление для этого факта ещё не запускалось или не фиксировалось в исторических данных.",
        "tone": "neutral",
    },
    "queued": {
        "label": "CRM-проверка в очереди",
        "description": "Фоновая read-only проверка запланирована или выполняется.",
        "tone": "info",
    },
    "matched": {
        "label": "Сопоставлено с CRM",
        "description": "Найдена одна детерминированная сделка либо выбор подтверждён человеком.",
        "tone": "good",
    },
    "ambiguous": {
        "label": "Требуется выбор сделки",
        "description": "CRM вернула варианты, но данных недостаточно для безопасной автоматической привязки.",
        "tone": "warn",
    },
    "not_found": {
        "label": "Совпадений в CRM не найдено",
        "description": "Read-only поиск успешно завершён и не вернул подходящих сделок.",
        "tone": "neutral",
    },
    "disabled": {
        "label": "CRM-проверка отключена",
        "description": "Поиск не выполнялся: read-only сопоставление с CRM выключено.",
        "tone": "neutral",
    },
    "error": {
        "label": "Ошибка CRM-проверки",
        "description": "Поиск не завершён; отсутствие сделки не подтверждено.",
        "tone": "error",
    },
}

CRM_MATCH_COUNT_LABELS = {
    "not_requested": "без данных",
    "queued": "в очереди",
    "matched": "сопоставлено",
    "ambiguous": "требует выбора",
    "not_found": "не найдено",
    "disabled": "отключено",
    "error": "ошибка",
}

CRM_MATCH_SUMMARY_ORDER = (
    "matched",
    "ambiguous",
    "not_found",
    "queued",
    "disabled",
    "error",
    "not_requested",
)

CRM_MATCH_REASON_LABELS = {
    "existing_project_bitrix_id": "У локального проекта уже сохранён этот Bitrix ID.",
    "company_exact": "Название компании совпало точно.",
    "title_exact": "TITLE сделки точно совпал с объектом.",
    "object_field_exact": "Настроенное поле объекта точно совпало с фактом.",
    "title_partial": "TITLE сделки частично совпал с объектом; требуется ручная проверка.",
    "object_field_partial": "Поле объекта частично совпало; требуется ручная проверка.",
}


def _fact_count_label(count):
    remainder = count % 100
    if 11 <= remainder <= 14:
        noun = "фактов"
    else:
        noun = {1: "факт", 2: "факта", 3: "факта", 4: "факта"}.get(
            count % 10, "фактов"
        )
    return f"{count} {noun}"


def _candidate_queryset():
    current_matches = (
        CandidateCrmMatch.objects.filter(
            crm_match_revision=F("candidate__crm_match_revision")
        )
        .select_related("project")
        .order_by("-score", "id")
    )
    return FactCandidate.objects.select_related("project").prefetch_related(
        Prefetch("crm_matches", queryset=current_matches, to_attr="current_crm_matches")
    ).order_by("id")


def _with_trace_candidates(queryset):
    return queryset.prefetch_related(
        Prefetch("candidates", queryset=_candidate_queryset(), to_attr="crm_candidates")
    )


def _trace_candidates(obj):
    prefetched = getattr(obj, "crm_candidates", None)
    if prefetched is not None:
        return list(prefetched)
    return list(_candidate_queryset().filter(trace=obj))


def _candidate_matches(candidate):
    prefetched = getattr(candidate, "current_crm_matches", None)
    if prefetched is not None:
        return list(prefetched)
    return list(
        candidate.crm_matches.filter(
            crm_match_revision=candidate.crm_match_revision
        )
        .select_related("project")
        .order_by("-score", "id")
    )


def _crm_summary(obj):
    candidates = _trace_candidates(obj)
    counts = Counter(candidate.crm_match_state for candidate in candidates)
    if not candidates:
        return {
            "candidate_count": 0,
            "counts": counts,
            "text": "Нет кандидатов для CRM-проверки",
            "tone": "neutral",
        }
    parts = [_fact_count_label(len(candidates))]
    parts.extend(
        f"{CRM_MATCH_COUNT_LABELS[state]}: {counts[state]}"
        for state in CRM_MATCH_SUMMARY_ORDER
        if counts[state]
    )
    tone = (
        "error"
        if counts["error"]
        else "warn"
        if counts["ambiguous"]
        else "info"
        if counts["queued"]
        else "good"
        if counts["matched"]
        and not any(
            counts[state]
            for state in ("not_requested", "not_found", "disabled")
        )
        else "neutral"
    )
    return {
        "candidate_count": len(candidates),
        "counts": counts,
        "text": " · ".join(parts),
        "tone": tone,
    }


def _crm_summary_badge(obj):
    summary = _crm_summary(obj)
    return format_html(
        '<span class="mazory-admin-badge mazory-admin-badge--{}">{}</span>',
        summary["tone"],
        summary["text"],
    )


def _candidate_projects(obj):
    projects = {}
    for candidate in _trace_candidates(obj):
        if candidate.project_id and candidate.project:
            projects[candidate.project_id] = candidate.project
    # Older traces predate FactCandidate and only keep the direct project link.
    # Preserve that historical relation without presenting it as a CRM match.
    if not projects and obj.project_id and obj.project:
        projects[obj.project_id] = obj.project
    return list(projects.values())


def _bitrix_display_config():
    matching_config = (
        BitrixSettings.objects.filter(crm_matching_enabled=True)
        .order_by("pk")
        .first()
    )
    return (
        matching_config
        or BitrixSettings.objects.order_by("pk").first()
        or BitrixSettings()
    )


def _bitrix_portal_base(config):
    try:
        webhook_url = effective_webhook_url(config)
        checked_url(webhook_url)
        parsed = urlsplit(webhook_url)
        if parsed.scheme != "https" or not parsed.hostname:
            return ""
        hostname = (
            f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        )
        port = parsed.port
        suffix = (
            f":{port}"
            if port and not (parsed.scheme == "https" and port == 443)
            and not (parsed.scheme == "http" and port == 80)
            else ""
        )
        return f"{parsed.scheme}://{hostname}{suffix}"
    except (ProviderUnavailable, TypeError, ValueError):
        return ""


def _crm_error_text(code):
    labels = {
        "crm_disabled": "Read-only сопоставление CRM выключено.",
        "crm_matching_disabled": "Read-only сопоставление CRM выключено.",
        "crm_not_configured": "Подключение Bitrix24 для read-only поиска не настроено.",
        "crm_rejected": "Bitrix24 отклонил read-only запрос.",
        "crm_invalid_response": "Bitrix24 вернул ответ в неожиданном формате.",
        "crm_invalid_pagination": "Bitrix24 вернул некорректную страницу результатов.",
        "crm_pagination_limit": "Поиск остановлен на безопасном лимите страниц.",
        "crm_object_field_invalid": "Код поля объекта CRM настроен некорректно.",
        "crm_match_query_empty": "В факте нет объекта или компании для CRM-поиска.",
        "crm_project_scope_conflict": "Локальный проект относится к другой команде.",
        "crm_match_stale": "Результат относится к устаревшей версии кандидата.",
        "crm_match_deadline": "CRM-проверка остановлена по общему лимиту времени.",
        "crm_match_request_limit": "CRM-проверка остановлена по лимиту read-only запросов.",
        "provider_timeout": "Bitrix24 не ответил вовремя.",
        "crm_connection_error": "Соединение с Bitrix24 прервано.",
        "crm_request_error": "Read-only запрос к Bitrix24 не был завершён.",
        "crm_unexpected_error": "CRM-проверка остановлена внутренней ошибкой.",
        "provider_not_allowed": "Адрес Bitrix24 не разрешён настройками безопасности.",
        "bitrix_webhook_invalid": "Адрес REST Webhook Bitrix24 имеет неверный формат.",
        "provider_server_error": "Bitrix24 временно недоступен.",
    }
    if str(code).startswith("crm_http_"):
        return "Bitrix24 вернул ошибку HTTP; безопасный повтор запланирован."
    return labels.get(
        code,
        "CRM-проверка не завершена. Подробности доступны в журнале фоновых задач.",
    )


def _crm_card(candidate, portal_base):
    state = CRM_MATCH_STATE_UI.get(
        candidate.crm_match_state, CRM_MATCH_STATE_UI["not_requested"]
    )
    proposed = candidate.proposed_changes or {}
    options = []
    matches = sorted(
        _candidate_matches(candidate),
        key=lambda match: (
            {"selected": 0, "suggested": 1, "dismissed": 2}.get(
                match.selection_state, 3
            ),
            -match.score,
            match.id,
        ),
    )
    for match in matches:
        deal_id = str(match.bitrix_deal_id)
        reasons = (
            [
                CRM_MATCH_REASON_LABELS.get(str(reason), str(reason))
                for reason in match.match_reasons[:8]
            ]
            if isinstance(match.match_reasons, list)
            else []
        )
        deal_url = (
            f"{portal_base}/crm/deal/details/{quote(deal_id, safe='')}/"
            if portal_base and deal_id
            else ""
        )
        options.append(
            {
                "id": match.id,
                "deal_id": deal_id,
                "deal_title": match.deal_title or "Без названия",
                "company_name": match.company_name or "—",
                "object_label": match.object_label or "—",
                "stage_id": match.stage_id or "—",
                "opportunity": (
                    f"{match.opportunity:,.2f} {match.currency or ''}".strip()
                    if match.opportunity is not None
                    else "—"
                ),
                "score": match.score,
                "reasons": reasons,
                "selection_state": match.selection_state,
                "selection_label": match.get_selection_state_display(),
                "deal_url": deal_url,
                "project": match.project,
                "project_url": (
                    reverse("admin:api_project_change", args=[match.project_id])
                    if match.project_id
                    else ""
                ),
            }
        )
    return {
        "id": candidate.id,
        "fact_type": candidate.get_fact_type_display(),
        "review_status": candidate.get_status_display(),
        "object_name": proposed.get("object_name") or "—",
        "company_name": proposed.get("company_name") or "—",
        "state": candidate.crm_match_state,
        "state_label": state["label"],
        "state_description": state["description"],
        "tone": state["tone"],
        "checked_at": candidate.crm_checked_at,
        "revision": candidate.crm_match_revision,
        "error_code": candidate.crm_match_error_code,
        "error_text": _crm_error_text(candidate.crm_match_error_code),
        "project": candidate.project,
        "project_url": (
            reverse("admin:api_project_change", args=[candidate.project_id])
            if candidate.project_id
            else ""
        ),
        "options": options,
    }


@admin.register(UserProfile)
class UserProfileAdmin(ScopedReadOnlyAdmin):
    form = ProfileIdentityForm
    fields = ("user", "full_name", "phone", "email", "department")

    def get_readonly_fields(self, request, obj=None):
        if not request.user.is_superuser:
            return self.fields
        return ("user",) if obj else ()

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

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

    def get_queryset(self, request):
        return _with_trace_candidates(
            super().get_queryset(request).select_related("project")
        )

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
        return _crm_summary_badge(obj)

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
    class Media:
        css = {"all": ("mazory/css/admin_lists.css",)}

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
    form = AISettingsForm
    list_display = (
        "name",
        "chat_api_format",
        "chat_model_name",
        "embedding_model_name",
        "embedding_dimension",
        "is_active",
        "updated_at",
    )
    list_filter = ("chat_api_format", "is_active")
    list_editable = ("chat_model_name", "embedding_model_name", "is_active")
    exclude = (
        "chat_api_key",
        "chat_api_key_encrypted",
        "embedding_api_key",
        "embedding_api_key_encrypted",
    )
    readonly_fields = (
        "chat_api_key_status",
        "embedding_api_key_status",
        "updated_at",
    )
    fieldsets = (
        ("Конфигурация", {"fields": ("name", "is_active")}),
        (
            "Chat / Reasoning",
            {
                "fields": (
                    "chat_api_format",
                    "chat_provider_url",
                    "chat_model_name",
                    "chat_api_key_status",
                    "new_chat_api_key",
                    "clear_chat_api_key",
                    "chat_temperature",
                )
            },
        ),
        (
            "Embeddings (OpenAI-compatible)",
            {
                "fields": (
                    "embedding_provider_url",
                    "embedding_model_name",
                    "embedding_api_key_status",
                    "new_embedding_api_key",
                    "clear_embedding_api_key",
                    "embedding_dimension",
                )
            },
        ),
        (
            "Расширенные настройки контекста (OpenAI-compatible)",
            {
                "classes": ("collapse",),
                "description": (
                    "Токенизатор — это словарь для подсчёта размера контекста, "
                    "а не API-ключ. Anthropic Messages использует собственный "
                    "count_tokens и игнорирует два поля токенизатора."
                ),
                "fields": (
                    "context_window_tokens",
                    "max_completion_tokens",
                    "context_safety_tokens",
                    "tokenizer_id",
                    "tokenizer_revision",
                ),
            },
        ),
        (
            "Управление обработкой и бюджетом",
            {
                "fields": (
                    "message_processing_paused",
                    "daily_request_limit",
                    "daily_budget_usd",
                )
            },
        ),
        (
            "Системные промпты",
            {
                "classes": ("collapse",),
                "fields": ("system_prompt_worker", "system_prompt_assistant"),
            },
        ),
        ("Состояние", {"fields": ("updated_at",)}),
    )

    @admin.display(description="Chat API key")
    def chat_api_key_status(self, obj):
        configured = bool(obj and obj.has_chat_api_key)
        return format_html(
            '<span class="mazory-admin-badge mazory-admin-badge--{}">{}</span>',
            "good" if configured else "neutral",
            "Настроен" if configured else "Не настроен",
        )

    @admin.display(description="Embeddings API key")
    def embedding_api_key_status(self, obj):
        configured = bool(obj and obj.has_embedding_api_key)
        return format_html(
            '<span class="mazory-admin-badge mazory-admin-badge--{}">{}</span>',
            "good" if configured else "neutral",
            "Настроен" if configured else "Не настроен",
        )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        from django.utils import timezone
        from .models import OutboxEvent

        if {"daily_request_limit", "daily_budget_usd"} & set(form.changed_data):
            OutboxEvent.objects.filter(
                state="pending", error_code="ai_daily_budget_exhausted"
            ).update(next_attempt_at=timezone.now())
        if (
            "message_processing_paused" in form.changed_data
            and not obj.message_processing_paused
        ):
            OutboxEvent.objects.filter(
                state="pending", event_type__in=["extract_message", "index_message"]
            ).exclude(error_code="ai_daily_budget_exhausted").update(
                next_attempt_at=timezone.now()
            )

    @method_decorator(
        sensitive_post_parameters("new_chat_api_key", "new_embedding_api_key")
    )
    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        return super().changeform_view(request, object_id, form_url, extra_context)


@admin.register(BitrixSettings)
class BitrixSettingsAdmin(IntegrationAdmin):
    form = BitrixSettingsForm
    search_fields = ("name",)

    def has_add_permission(self, request):
        return (
            not BitrixSettings.objects.exists()
            and super().has_add_permission(request)
        )

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
            "Read-only сопоставление",
            {
                "description": (
                    "Независимый поиск существующих компаний и сделок. "
                    "Этот режим не создаёт и не изменяет записи Bitrix24."
                ),
                "fields": (
                    "crm_matching_enabled",
                    "deal_object_field_code",
                    "deal_category_id",
                ),
            },
        ),
        (
            "Исходящая и входящая синхронизация",
            {
                "fields": (
                    "hourly_sync_enabled",
                    "auto_create_deals",
                    "auto_import_deals",
                    "auto_create_tasks",
                    "sync_timeline_comments",
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
            '<div class="mazory-list-stack">'
            '<span class="mazory-admin-badge mazory-admin-badge--{}">Запись: {}</span>'
            '<span class="mazory-admin-badge mazory-admin-badge--{}">Read-only поиск: {}</span>'
            "{}</div>",
            "good" if obj.is_active else "neutral",
            "включена" if obj.is_active else "выключена",
            "info" if obj.crm_matching_enabled else "neutral",
            "включён" if obj.crm_matching_enabled else "выключен",
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
        "thread_badge",
        "trace_context",
        "trace_result",
        "project_link",
    )
    list_display_links = ("trace_source",)
    list_filter = ("pipeline_action", "status", "created_at")
    search_fields = (
        "whatsapp_content",
        "whatsapp_sender_name",
        "whatsapp_sender_phone",
        "project__name",
        "candidates__project__name",
        "candidates__crm_matches__bitrix_deal_id",
        "candidates__crm_matches__deal_title",
        "candidates__crm_matches__company_name",
        "whatsapp_message_id",
    )
    readonly_fields = (
        "pipeline_overview_banner",
        "created_at_fmt",
        "pipeline_action_badge",
        "status_badge",
        "result_summary_fmt",
        "stage_1_whatsapp_card",
        "dialogue_thread_hierarchy_card",
        "stage_2_earlier_messages_card",
        "stage_3_bitrix_card",
        "stage_4_final_record_card",
    )
    fieldsets = (
        (
            "Сквозная цепочка (WhatsApp → Контекст → CRM-сопоставление → AI-факты)",
            {"fields": ("pipeline_overview_banner", "result_summary_fmt")},
        ),
        ("Этап 1: «Входные данные WhatsApp»", {"fields": ("stage_1_whatsapp_card",)}),
        (
            "Этап 2: «История сообщений и контекст треда»",
            {
                "fields": (
                    "dialogue_thread_hierarchy_card",
                    "stage_2_earlier_messages_card",
                )
            },
        ),
        (
            "Этап 3: Read-only сопоставление с Bitrix24 по каждому факту",
            {"fields": ("stage_3_bitrix_card",)},
        ),
        (
            "Этап 4: AI-факты и созданные сущности",
            {"fields": ("stage_4_final_record_card",)},
        ),
    )
    change_form_template = "admin/message_trace_change.html"
    actions = None

    def get_queryset(self, request):
        return _with_trace_candidates(
            super().get_queryset(request).select_related("project", "raw_message")
        )

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
        retry = retry_view(obj)
        if retry.get("state") in ("pending", "processing", "budget_wait"):
            return format_html(
                '<div class="mazory-list-stack"><span class="mazory-admin-badge mazory-admin-badge--warn">{}</span>'
                '<span class="mazory-list-meta">Попытки: {} из {}. {}</span></div>',
                "Ожидает повтора" if retry["state"] != "processing" else "Повторный анализ",
                retry["attempts"], retry["max_attempts"], ERRORS.get(obj.error_code, obj.error_code),
            )
        if obj.status == "error":
            return format_html(
                '<div class="mazory-list-stack">{}<span class="mazory-list-meta">{}</span></div>',
                self.status_badge(obj),
                ERRORS.get(obj.error_code, obj.error_code or obj.result_summary),
            )
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

    @admin.display(description="Тред диалога")
    def thread_badge(self, obj):
        if not obj.raw_message_id:
            return mark_safe(
                '<span class="mazory-admin-badge mazory-admin-badge--neutral">Без треда</span>'
            )
        tm = (
            ThreadMessage.objects.filter(raw_message=obj.raw_message)
            .select_related("thread__project")
            .first()
        )
        if not tm or not tm.thread:
            return mark_safe(
                '<span class="mazory-admin-badge mazory-admin-badge--neutral">Вне треда</span>'
            )
        thread = tm.thread
        state = thread.state or "open"
        tone = "warn" if state == "open" else "good" if state == "ready" else "neutral"
        project_name = ""
        if thread.project and thread.project.name:
            project_name = thread.project.name
        elif obj.project and obj.project.name:
            project_name = obj.project.name

        badge_text_val = f"Тред #{thread.id} ({state})"
        if project_name:
            clean_proj = (
                (project_name[:26] + "…")
                if len(project_name) > 26
                else project_name
            )
            badge_text_val += f" · {clean_proj}"

        return format_html(
            '<span class="mazory-admin-badge mazory-admin-badge--{}" title="{}">{}</span>',
            tone,
            thread.topic or "",
            badge_text_val,
        )

    def earlier_messages_badge(self, obj):
        return format_html(
            '<span class="mazory-admin-badge mazory-admin-badge--info">{}</span>',
            badge_text(obj),
        )

    earlier_messages_badge.short_description = "Сообщений в контексте"

    def bitrix_matched_badge(self, obj):
        return _crm_summary_badge(obj)

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
        candidates = _trace_candidates(obj)
        projects = _candidate_projects(obj)
        links = format_html_join(
            "",
            '<a href="{}" class="mazory-list-link mazory-project-name" title="{}">{} →</a>',
            (
                (
                    reverse("admin:api_project_change", args=[project.pk]),
                    project.name,
                    project.name,
                )
                for project in projects
            ),
        )
        if links:
            return format_html(
                '<span class="mazory-list-stack"><span class="mazory-list-meta">{} · {} связанных проектов</span>{}</span>',
                _fact_count_label(len(candidates)),
                len(projects),
                links,
            )
        return format_html(
            '<span class="mazory-list-stack"><span class="mazory-list-meta">{}</span>'
            '<span class="mazory-list-meta">{}</span></span>',
            _fact_count_label(len(candidates)) if candidates else "Фактов нет",
            "Каноническая сделка ещё не выбрана"
            if candidates
            else "Связь со сделкой не применима",
        )

    project_link.short_description = "Кандидаты / сделки"

    def status_badge(self, obj):
        color = {"success": "good", "warning": "warn", "error": "error"}.get(
            obj.status, "neutral"
        )
        return format_html(
            '<span class="mazory-admin-badge mazory-admin-badge--{}">AI: {}</span>',
            color,
            obj.get_status_display(),
        )

    status_badge.short_description = "AI-анализ"

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
        tm = (
            ThreadMessage.objects.filter(raw_message=obj.raw_message)
            .select_related("thread__project")
            .first()
            if obj.raw_message_id
            else None
        )
        if tm and tm.thread:
            s2_title = f"Тред #{tm.thread.id} ({tm.thread.state})"
            s2_sub = tm.thread.project.name if tm.thread.project else badge_text(obj)
        else:
            s2_title = badge_text(obj)
            s2_sub = "Сохранённый контекст"

        s3_sub = _crm_summary(obj)["text"]
        s4_sub = f"AI: {obj.get_status_display()}"
        projects = _candidate_projects(obj)
        project_summary = (
            projects[0].name
            if len(projects) == 1
            else f"Связано проектов: {len(projects)}"
            if projects
            else "Ожидает review"
        )
        html = format_html(
            '\n        <div class="mazory-trace-overview w-full my-3 p-5 rounded-2xl bg-gradient-to-r from-gray-900 via-indigo-950 to-gray-900 text-white shadow-lg border border-indigo-900/40">\n            <div class="text-xs font-mono uppercase tracking-wider text-indigo-400 mb-3 flex items-center justify-between">\n                <span>Data Lineage Audit Trail</span>\n                <span>ID Трассировки: #{}</span>\n            </div>\n            <div class="grid grid-cols-1 md:grid-cols-4 gap-4 relative">\n                <!-- Step 1 -->\n                <div class="p-3 rounded-xl bg-white/5 border border-emerald-500/30 flex flex-col justify-between">\n                    <div class="flex items-center gap-2 text-emerald-400 font-semibold text-xs uppercase">\n                        <span class="w-5 h-5 rounded-full bg-emerald-500/20 flex items-center justify-center text-xs">1</span>\n                        WhatsApp Вход\n                    </div>\n                    <div class="mt-2 text-sm font-bold truncate text-white">{}</div>\n                    <div class="text-xs text-gray-400 font-mono mt-0.5">{}</div>\n                </div>\n\n                <!-- Step 2 -->\n                <div class="p-3 rounded-xl bg-white/5 border border-purple-500/30 flex flex-col justify-between">\n                    <div class="flex items-center gap-2 text-purple-400 font-semibold text-xs uppercase">\n                        <span class="w-5 h-5 rounded-full bg-purple-500/20 flex items-center justify-center text-xs">2</span>\n                        Тред / Контекст\n                    </div>\n                    <div class="mt-2 text-sm font-bold truncate text-white">{}</div>\n                    <div class="text-xs text-purple-300 mt-0.5 truncate">{}</div>\n                </div>\n\n                <!-- Step 3 -->\n                <div class="p-3 rounded-xl bg-white/5 border border-sky-500/30 flex flex-col justify-between">\n                    <div class="flex items-center gap-2 text-sky-400 font-semibold text-xs uppercase">\n                        <span class="w-5 h-5 rounded-full bg-sky-500/20 flex items-center justify-center text-xs">3</span>\n                        Данные Bitrix24\n                    </div>\n                    <div class="mt-2 text-sm font-bold text-white truncate">{}</div>\n                    <div class="text-xs text-sky-300 mt-0.5 truncate">{}</div>\n                </div>\n\n                <!-- Step 4 -->\n                <div class="p-3 rounded-xl bg-white/5 border border-amber-500/30 flex flex-col justify-between">\n                    <div class="flex items-center gap-2 text-amber-400 font-semibold text-xs uppercase">\n                        <span class="w-5 h-5 rounded-full bg-amber-500/20 flex items-center justify-center text-xs">4</span>\n                        Итоговая запись\n                    </div>\n                    <div class="mt-2 text-sm font-bold text-white truncate">{}</div>\n                    <div class="text-xs text-amber-300 mt-0.5 truncate">{}</div>\n                </div>\n            </div>\n        </div>\n        ',
            obj.id,
            s1_title,
            obj.whatsapp_sender_phone or "Прямой вебхук",
            s2_title,
            s2_sub,
            s3_sub,
            "Независимый статус для каждого факта",
            s4_sub,
            project_summary,
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

    def dialogue_thread_hierarchy_card(self, obj):
        """Рендеринг иерархической карточки треда диалога (Этап 2)"""
        if not obj.raw_message_id:
            return mark_safe(
                '<div class="p-4 rounded-xl border border-gray-200 dark:border-gray-800 '
                'bg-gray-50/50 dark:bg-gray-900/40 text-xs text-gray-500 dark:text-gray-400 '
                'flex items-center gap-3">'
                '<span class="text-base">💬</span>'
                '<div>'
                '<div class="font-semibold text-gray-700 dark:text-gray-300">Сообщение вне треда диалога</div>'
                '<div class="text-[11px] mt-0.5 text-gray-500 dark:text-gray-400">'
                'Данное сообщение не связано с исходным сообщением WhatsApp или обрабатывается автономно.'
                '</div>'
                '</div>'
                '</div>'
            )

        tm = (
            ThreadMessage.objects.filter(raw_message=obj.raw_message)
            .select_related("thread__project__company", "thread__parent")
            .first()
        )
        if not tm or not tm.thread:
            return mark_safe(
                '<div class="p-4 rounded-xl border border-gray-200 dark:border-gray-800 '
                'bg-gray-50/50 dark:bg-gray-900/40 text-xs text-gray-500 dark:text-gray-400 '
                'flex items-center gap-3">'
                '<span class="text-base">💬</span>'
                '<div>'
                '<div class="font-semibold text-gray-700 dark:text-gray-300">Сообщение ещё не включено в тред диалога</div>'
                '<div class="text-[11px] mt-0.5 text-gray-500 dark:text-gray-400">'
                'Данное сообщение пока не сгруппировано в тематический тред или является автономным инфо-сообщением.'
                '</div>'
                '</div>'
                '</div>'
            )

        thread = tm.thread

        # 1. Компания Bitrix24
        company = None
        if thread.project and thread.project.company:
            company = thread.project.company
        elif obj.project and obj.project.company:
            company = obj.project.company

        if company:
            try:
                comp_url = reverse("admin:api_company_change", args=[company.id])
                company_link = format_html(
                    '<a href="{}" class="text-[11px] text-indigo-400 hover:underline">#{} ↗</a>',
                    comp_url,
                    company.bitrix_company_id or company.id,
                )
            except Exception:
                company_link = format_html(
                    '<span class="text-[11px] text-indigo-400 font-mono">#{}</span>',
                    company.bitrix_company_id or company.id,
                )
            company_name_html = format_html(
                '<span class="text-sm font-bold text-indigo-200">{}</span> {}',
                company.name,
                company_link,
            )
        else:
            company_name_html = mark_safe(
                '<span class="text-xs text-gray-400 italic">Компания не указана</span>'
            )

        # 2. Сделка / Объект
        project = thread.project or obj.project
        if project:
            try:
                proj_url = reverse("admin:api_project_change", args=[project.id])
                deal_label = (
                    f"Bitrix #{project.bitrix_id}"
                    if project.bitrix_id
                    else f"#{project.id}"
                )
                proj_link = format_html(
                    '<a href="{}" class="text-[11px] text-sky-400 hover:underline">{} ↗</a>',
                    proj_url,
                    deal_label,
                )
            except Exception:
                proj_link = format_html(
                    '<span class="text-[11px] text-sky-400 font-mono">#{}</span>',
                    project.bitrix_id or project.id,
                )
            project_name_html = format_html(
                '<span class="text-sm font-bold text-sky-200">{}</span> {}',
                project.name,
                proj_link,
            )
            amt_str = (
                f"{project.contract_amount:,.2f} ₸"
                if project.contract_amount
                else "—"
            )
            project_meta_html = format_html(
                '<span class="text-gray-400 text-[11px]">Сумма: <b class="text-emerald-400">{}</b> · Стадия: <b class="text-gray-300">{}</b></span>',
                amt_str,
                project.get_status_display(),
            )
        else:
            project_name_html = mark_safe(
                '<span class="text-xs text-gray-400 italic">Сделка / объект не привязаны</span>'
            )
            project_meta_html = mark_safe(
                '<span class="text-gray-500 text-[11px]">—</span>'
            )

        # 3. Тред и Поддиалог (если применимо)
        state_badge_map = {
            "open": ("badge-warn", "open (мысль в процессе)"),
            "ready": ("badge-good", "ready (мысль завершена)"),
            "superseded": ("badge-neutral", "superseded (заменена)"),
            "unknown": ("badge-neutral", "unknown"),
        }
        thread_badge_cls, thread_badge_label = state_badge_map.get(
            thread.state, ("badge-neutral", thread.state or "unknown")
        )

        msg_count = thread.message_links.count()
        count_label = f"{msg_count} сообщ. WA"

        if thread.parent:
            parent = thread.parent
            p_cls, p_label = state_badge_map.get(
                parent.state, ("badge-neutral", parent.state or "unknown")
            )
            thread_hierarchy_html = format_html(
                '<div class="space-y-2">'
                '<div class="flex items-center justify-between text-gray-300">'
                '<div class="flex items-center gap-2">'
                '<span>📂</span>'
                '<span class="font-medium text-white">Родительский тред #{}: {}</span>'
                '<span class="{} px-2 py-0.5 rounded text-[10px]">{}</span>'
                '</div>'
                '</div>'
                '<div class="ml-4 pl-3 border-l-2 border-indigo-400/40 space-y-1">'
                '<div class="flex items-center justify-between text-gray-300">'
                '<div class="flex items-center gap-2">'
                '<span>↳ 📂</span>'
                '<span class="font-bold text-indigo-200">Поддиалог #{}: {}</span>'
                '<span class="{} px-2 py-0.5 rounded text-[10px]">{}</span>'
                '</div>'
                '<span class="text-gray-400 font-mono text-[11px]">{}</span>'
                '</div>'
                '</div>'
                '</div>',
                parent.id,
                parent.topic or f"Тред #{parent.id}",
                p_cls,
                p_label,
                thread.id,
                thread.topic or f"Поддиалог #{thread.id}",
                thread_badge_cls,
                thread_badge_label,
                count_label,
            )
        else:
            child = thread.children.first()
            subdialogue_part = ""
            if child:
                c_cls, c_label = state_badge_map.get(
                    child.state, ("badge-neutral", child.state or "unknown")
                )
                subdialogue_part = format_html(
                    '<div class="ml-4 pl-3 border-l-2 border-indigo-400/20 text-gray-400 flex items-center justify-between pt-1 text-[11px]">'
                    '<span>↳ 📂 Поддиалог #{}: {}</span>'
                    '<span class="{} px-1.5 py-0.5 rounded text-[10px]">{}</span>'
                    '</div>',
                    child.id,
                    child.topic or f"Поддиалог #{child.id}",
                    c_cls,
                    c_label,
                )

            thread_hierarchy_html = format_html(
                '<div class="space-y-1">'
                '<div class="flex items-center justify-between text-gray-300">'
                '<div class="flex items-center gap-2">'
                '<span>📂</span>'
                '<span class="font-bold text-white">Тред #{}: {}</span>'
                '<span class="{} px-2 py-0.5 rounded text-[10px]">{}</span>'
                '</div>'
                '<span class="text-gray-400 font-mono text-[11px]">{}</span>'
                '</div>'
                '{}'
                '</div>',
                thread.id,
                thread.topic or f"Тред #{thread.id}",
                thread_badge_cls,
                thread_badge_label,
                count_label,
                mark_safe(subdialogue_part),
            )

        # 4. Message role, thought state & rationale
        role_map = {
            "discusses": "Обсуждает (discusses)",
            "answers": "Отвечает (answers)",
            "clarifies": "Уточняет (clarifies)",
            "cancels": "Отменяет / пересматривает (cancels)",
            "fulfills": "Выполняет обязательство (fulfills)",
        }
        thought_map = {
            "intermediate": "Промежуточная мысль (intermediate)",
            "final": "Финальная мысль (final)",
            "unknown": "Не определено (unknown)",
        }
        role_label = role_map.get(tm.relation, tm.relation or "discusses")
        thought_label = thought_map.get(tm.thought_state, tm.thought_state or "intermediate")
        rationale_text = tm.rationale or thread.summary or "Обоснование отсутствует."

        role_html = format_html(
            '<div class="p-3 rounded-lg bg-gray-900/90 border border-gray-800 space-y-1.5 text-xs">'
            '<div class="flex flex-wrap items-center gap-3">'
            '<div>'
            '<span class="text-gray-400 font-medium">Роль реплики в треде:</span>'
            '<span class="ml-1 px-2 py-0.5 rounded text-[10px] font-semibold bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">{}</span>'
            '</div>'
            '<div>'
            '<span class="text-gray-400 font-medium">Статус мысли:</span>'
            '<span class="ml-1 px-2 py-0.5 rounded text-[10px] font-semibold bg-purple-500/20 text-purple-300 border border-purple-500/30">{}</span>'
            '</div>'
            '</div>'
            '<div class="text-[11px] text-gray-300 pt-1">'
            '<span class="text-gray-400 font-semibold">Обоснование классификатора:</span>'
            '<span class="text-gray-200 ml-1">{}</span>'
            '</div>'
            '</div>',
            role_label,
            thought_label,
            rationale_text,
        )

        # 5. Extracted facts / commitments
        fact_cards = []
        for candidate in _trace_candidates(obj):
            changes = candidate.proposed_changes or {}
            cand_status = candidate.get_status_display()
            if candidate.fact_type == "project":
                name = changes.get("name") or (candidate.project.name if candidate.project else "Объект")
                amt = changes.get("contract_amount")
                if amt is None and candidate.project:
                    amt = candidate.project.contract_amount
                amt_fmt = f"{float(amt):,.2f} ₸" if amt is not None else "0.00 ₸"
                stage = changes.get("stage") or (candidate.project.get_status_display() if candidate.project else "")
                fact_cards.append({
                    "title": f"Сделка: {name}",
                    "desc": f"Сумма: {amt_fmt}" + (f" · Стадия: {stage}" if stage else ""),
                    "badge": f"Сопоставлено Bitrix ({cand_status})",
                    "border": "border-emerald-500/30",
                    "text_color": "text-emerald-400",
                    "badge_cls": "badge-good",
                })
            elif candidate.fact_type == "commitment":
                text = changes.get("commitment_text") or "Обязательство"
                dl = changes.get("deadline") or ""
                amt = changes.get("amount")
                amt_str = f" · Сумма: {float(amt):,.2f} ₸" if amt else ""
                fact_cards.append({
                    "title": f"Обязательство: {text[:60]}",
                    "desc": (f"Дедлайн: {dl}" if dl else "Без дедлайна") + amt_str,
                    "badge": f"SLA ({cand_status})",
                    "border": "border-amber-500/30",
                    "text_color": "text-amber-300",
                    "badge_cls": "badge-warn",
                })
            elif candidate.fact_type == "payment":
                amt = changes.get("amount") or 0
                pt = changes.get("payment_type") or "Оплата"
                dt = changes.get("payment_date") or ""
                fact_cards.append({
                    "title": f"Платеж: {float(amt):,.2f} ₸",
                    "desc": f"{pt}" + (f" от {dt}" if dt else ""),
                    "badge": f"Финансы ({cand_status})",
                    "border": "border-teal-500/30",
                    "text_color": "text-teal-400",
                    "badge_cls": "badge-good",
                })

        if not fact_cards:
            if obj.commitment:
                c = obj.commitment
                fact_cards.append({
                    "title": f"Обязательство: {c.commitment_text[:60]}",
                    "desc": f"Дедлайн: {c.deadline or '—'} · Срочность: {c.get_severity_display()}",
                    "badge": f"Создано SLA #{c.id}",
                    "border": "border-amber-500/30",
                    "text_color": "text-amber-300",
                    "badge_cls": "badge-warn",
                })
            if obj.financial_record:
                f = obj.financial_record
                fact_cards.append({
                    "title": f"Платеж: {float(f.amount):,.2f} ₸",
                    "desc": f"{f.get_payment_type_display()} · {f.payment_date}",
                    "badge": f"Оплата #{f.id}",
                    "border": "border-teal-500/30",
                    "text_color": "text-teal-400",
                    "badge_cls": "badge-good",
                })
            if obj.ai_extracted_facts:
                facts = obj.ai_extracted_facts
                ca = facts.get("contract_amount")
                if ca and float(ca) > 0:
                    fact_cards.append({
                        "title": f"Сделка: {facts.get('object_name') or 'Объект'}",
                        "desc": f"Сумма: {float(ca):,.2f} ₸ · Стадия: {facts.get('stage') or '—'}",
                        "badge": f"AI-факт ({int(obj.ai_confidence * 100)}%)",
                        "border": "border-emerald-500/30",
                        "text_color": "text-emerald-400",
                        "badge_cls": "badge-good",
                    })

        if fact_cards:
            rendered_cards = "".join(
                format_html(
                    '<div class="p-2.5 rounded-lg bg-gray-900 {} border flex items-center justify-between gap-2">'
                    '<div class="min-w-0">'
                    '<div class="font-bold {} text-xs truncate">{}</div>'
                    '<div class="text-[11px] text-gray-400 mt-0.5 truncate">{}</div>'
                    '</div>'
                    '<span class="{} text-[10px] px-2 py-0.5 rounded shrink-0">{}</span>'
                    '</div>',
                    c["border"],
                    c["text_color"],
                    c["title"],
                    c["desc"],
                    c["badge_cls"],
                    c["badge"],
                )
                for c in fact_cards
            )
            facts_html = format_html(
                '<div class="grid grid-cols-1 sm:grid-cols-2 gap-2 pt-1">{}</div>',
                mark_safe(rendered_cards),
            )
        else:
            facts_html = mark_safe(
                '<div class="text-[11px] text-gray-400 italic py-1">Факты и обязательства в данной реплике не зафиксированы.</div>'
            )

        card_html = format_html(
            '<div class="unfold-card p-5 space-y-4 border border-indigo-900/40 bg-gradient-to-b from-gray-900 to-[#0c1222] text-white">'
            '<div class="flex items-center justify-between pb-3 border-b border-gray-800">'
            '<div>'
            '<h3 class="text-sm font-bold text-white uppercase tracking-wider flex items-center gap-2">'
            '<span>🌳 Иерархия диалога и контекст треда</span>'
            '</h3>'
            '<p class="text-xs text-gray-400 mt-0.5">'
            'Положение сообщения в директории проектов и связных тематических ветках'
            '</p>'
            '</div>'
            '<div class="flex items-center gap-2">'
            '<span class="{} px-2.5 py-0.5 rounded-full text-xs font-semibold">{}</span>'
            '</div>'
            '</div>'
            '<div class="space-y-3 font-sans text-xs">'
            '<div class="p-3.5 rounded-lg bg-gray-950/70 border border-gray-800 space-y-3">'
            '<!-- 1. Компания Bitrix -->'
            '<div class="flex items-center justify-between font-semibold text-white">'
            '<div class="flex items-center gap-2">'
            '<span class="text-indigo-400 font-bold">📁 Проект (Компания Bitrix):</span>'
            '{}'
            '</div>'
            '</div>'
            '<!-- 2. Сделка / Объект -->'
            '<div class="ml-4 pl-3 border-l-2 border-indigo-500/40 space-y-2">'
            '<div class="flex items-center justify-between text-gray-300">'
            '<div class="flex items-center gap-2">'
            '<span class="text-sky-400 font-bold">🏢 Объект / Сделка:</span>'
            '{}'
            '</div>'
            '{}'
            '</div>'
            '<!-- 3. Тред и Поддиалог -->'
            '<div class="ml-4 pl-3 border-l-2 border-indigo-500/40 space-y-2.5">'
            '{}'
            '{}'
            '<!-- 4. Извлеченные факты -->'
            '<div class="pt-1">'
            '<div class="text-[11px] font-semibold text-gray-400 uppercase tracking-wider mb-1.5">'
            'Извлеченные сущности и факты из этого контекста:'
            '</div>'
            '{}'
            '</div>'
            '</div>'
            '</div>'
            '</div>'
            '</div>'
            '</div>',
            thread_badge_cls,
            thread_badge_label,
            company_name_html,
            project_name_html,
            project_meta_html,
            thread_hierarchy_html,
            role_html,
            facts_html,
        )
        return mark_safe(card_html)

    dialogue_thread_hierarchy_card.short_description = (
        "2. Иерархия треда диалога"
    )

    def stage_2_earlier_messages_card(self, obj):
        return mark_safe(render_context(obj))

    stage_2_earlier_messages_card.short_description = "2. История сообщений"

    def stage_3_bitrix_card(self, obj):
        """Render one truthful CRM state per extracted fact."""
        config = _bitrix_display_config()
        portal_base = _bitrix_portal_base(config)
        return render_to_string(
            "admin/trace_crm_matches.html",
            {
                "cards": [
                    _crm_card(candidate, portal_base)
                    for candidate in _trace_candidates(obj)
                ],
                "summary": _crm_summary(obj),
                "config_name": config.name,
                "matching_enabled": config.crm_matching_enabled,
                "outbound_enabled": config.is_active,
                "webhook_configured": bool(effective_webhook_url(config)),
            },
        )

    stage_3_bitrix_card.short_description = (
        "3. Read-only сопоставление с Bitrix24 по каждому факту"
    )

    def stage_4_final_record_card(self, obj):
        """Рендеринг Этапа 4: «Итоговая запись»"""
        facts = obj.ai_extracted_facts or {}
        facts_json = json.dumps(facts, indent=2, ensure_ascii=False)
        if obj.status == "error" or "facts" in facts or (
            obj.status == "warning"
            and obj.context_metadata.get("source") == "chat_history"
        ):
            return render_to_string(
                "admin/trace_result.html",
                {
                    "trace": obj,
                    "retry": retry_view(obj),
                    "error_label": ERRORS.get(obj.error_code, obj.error_code),
                    "diagnostics": json.dumps(
                        obj.context_metadata.get("response_diagnostics", {}),
                        ensure_ascii=False, indent=2,
                    ),
                    "candidates": _trace_candidates(obj),
                    "facts_json": facts_json,
                },
            )
        conf_pct = f"{obj.ai_confidence * 100:.0f}%"
        projects = _candidate_projects(obj)
        if projects:
            project_html = format_html_join(
                "",
                '<div class="p-3 rounded-lg mazory-trace-card border border-emerald-500/30 space-y-2">'
                '<a href="{}" class="text-sm font-bold text-indigo-600 hover:underline">'
                'Проект #{}: {} ↗</a>'
                '<div class="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">'
                '<div><span class="mazory-trace-meta">Сумма:</span> '
                '<b class="mazory-trace-text">{} ₸</b></div>'
                '<div><span class="mazory-trace-meta">Маржа:</span> '
                '<b class="mazory-trace-text">{}%</b></div>'
                '<div><span class="mazory-trace-meta">Статус:</span> '
                '<b class="mazory-trace-text">{}</b></div>'
                '<div><span class="mazory-trace-meta">Проверка:</span> '
                '<b class="mazory-trace-text">{}</b></div>'
                '</div></div>',
                (
                    (
                        reverse("admin:api_project_change", args=[project.pk]),
                        project.pk,
                        project.name,
                        f"{project.contract_amount:,.2f}",
                        project.actual_margin_percent,
                        project.get_status_display(),
                        "Подтверждена" if project.is_verified else "Ожидает review",
                    )
                    for project in projects
                ),
            )
        else:
            project_html = format_html(
                '<em class="mazory-trace-meta">{}</em>',
                "Канонические проекты кандидатов ещё не выбраны",
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
    NotificationDelivery,
)

for model in (Team, TeamMembership, ChatAccess, ClientProjectAccess):
    admin.site.register(model, SuperuserAdmin)
for model in (
    AuditEvent,
    ProjectRevision,
    FactCandidate,
    NotificationDelivery,
):
    admin.site.register(model, ScopedReadOnlyAdmin)

from .models import McpToken


@admin.register(McpToken)
class McpTokenAdmin(ModelAdmin):
    list_display = ("name", "user", "is_active", "created_at", "last_used_at", "expires_at")
    list_filter = ("is_active", "created_at")
    search_fields = ("name", "user__username", "user__first_name", "user__last_name")
    readonly_fields = ("token_hash", "created_at", "last_used_at")


from . import job_admin  # noqa: F401, E402 — register job views after source admins.

