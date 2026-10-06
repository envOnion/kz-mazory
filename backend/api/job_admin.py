from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, OuterRef, Prefetch, Q, Subquery
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import escape, format_html, format_html_join, mark_safe
from django.views.decorators.http import require_POST

from .admin_access import IntegrationAdmin, ScopedReadOnlyAdmin
from .history_jobs import ERROR_LABELS, IMPORT_STATES, control_run, progress, start_job
from .models import (
    DialogueThread,
    FactCandidate,
    MessageProcessingTrace,
    OutboxEvent,
    ThreadMessage,
    ThreadRevision,
    WhatsAppHistoryJob,
    WhatsAppHistoryRun,
    HistoryAnalysisItem,
)
from .providers import ProviderUnavailable
from .trace_context_ui import ERRORS


@admin.register(WhatsAppHistoryJob)
class WhatsAppHistoryJobAdmin(IntegrationAdmin):
    change_form_template = "admin/history_job_change.html"
    list_display = (
        "config",
        "enabled",
        "frequency",
        "analyze_after_import",
        "latest_run",
        "next_run_at",
    )
    list_filter = ("enabled", "only_new", "analyze_after_import")
    search_fields = ("config__name", "config__group_jid")
    readonly_fields = (
        "latest_run",
        "next_run_at",
        "updated_at",
        "new_messages_started_at",
        "new_messages_since",
        "last_checked_at",
    )
    fieldsets = (
        (
            "Источник и расписание",
            {
                "fields": (
                    "config",
                    "enabled",
                    "only_new",
                    "new_message_poll_seconds",
                    "interval_minutes",
                    "next_run_at",
                    "new_messages_started_at",
                    "new_messages_since",
                    "last_checked_at",
                )
            },
        ),
        (
            "Сбор истории",
            {
                "fields": (
                    "page_size",
                    "initial_wait_seconds",
                    "poll_seconds",
                    "stable_scans_required",
                )
            },
        ),
        (
            "Анализ",
            {
                "fields": ("analyze_after_import",),
                "description": "Модель, лимиты анализа и бюджет задаются в настройках AI. Размер страницы не ограничивает общий объём истории.",
            },
        ),
        ("Последний запуск", {"fields": ("latest_run", "updated_at")}),
    )

    @admin.display(description="Периодичность")
    def frequency(self, obj):
        if obj.only_new:
            return f"Только новые · каждые {obj.new_message_poll_seconds} сек."
        return (
            f"Каждые {obj.interval_minutes} мин."
            if obj.interval_minutes
            else "Только вручную"
        )

    @admin.display(description="Запуск")
    def latest_run(self, obj):
        run = obj.runs.first() if obj.pk else None
        return (
            format_html(
                '<a href="{}">#{} · {}</a>',
                reverse("admin:api_whatsapphistoryrun_change", args=[run.pk]),
                run.pk,
                run.get_state_display(),
            )
            if run
            else "Ещё не запускалось"
        )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("config")

    def get_urls(self):
        return [
            path(
                "<int:object_id>/start/",
                self.admin_site.admin_view(require_POST(self.start)),
                name="api_whatsapphistoryjob_start",
            )
        ] + super().get_urls()

    def start(self, request, object_id):
        job = get_object_or_404(self.get_queryset(request), pk=object_id)
        if not self.has_change_permission(request, job):
            raise PermissionDenied()
        try:
            run = start_job(job.id, request.user)
        except (ValidationError, ProviderUnavailable) as exc:
            message = (
                exc.messages[0]
                if isinstance(exc, ValidationError)
                else ERROR_LABELS.get(str(exc), str(exc))
            )
            messages.error(request, message)
            return redirect("admin:api_whatsapphistoryjob_change", job.id)
        messages.success(
            request, "Импорт поставлен в очередь. Здесь появится его прогресс."
        )
        return redirect("admin:api_whatsapphistoryrun_change", run.id)

    def save_model(self, request, obj, form, change):
        if {
            "interval_minutes",
            "enabled",
            "only_new",
            "new_message_poll_seconds",
        } & set(form.changed_data):
            obj.next_run_at = None
        super().save_model(request, obj, form, change)
        from .new_messages import sync_monitor_settings

        sync_monitor_settings(obj, form.changed_data, request.user)


def _pluralize_ru(n: int, one: str, few: str, many: str) -> str:
    n_abs = abs(int(n))
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return f"{n} {one}"
    elif 2 <= n_abs % 10 <= 4 and (n_abs % 100 < 10 or n_abs % 100 >= 20):
        return f"{n} {few}"
    return f"{n} {many}"


def _format_tenge(val) -> str:
    if val is None or val == "":
        return ""
    try:
        from decimal import Decimal

        cleaned = (
            str(val)
            .replace(" ", "")
            .replace("\xa0", "")
            .replace(",", ".")
            .replace("₸", "")
            .strip()
        )
        num = Decimal(cleaned)
        if num == num.to_integral():
            formatted = f"{int(num):,}".replace(",", " ")
        else:
            formatted = f"{num:,.2f}".replace(",", " ")
        return f"{formatted} ₸"
    except Exception:
        s = str(val).strip()
        return f"{s} ₸" if "₸" not in s else s


def _get_thread_facts(thread):
    revisions = sorted(thread.revisions.all(), key=lambda r: r.version, reverse=True)
    facts = []
    candidates = []
    if revisions:
        latest_rev = revisions[0]
        candidates = [
            c for c in latest_rev.candidates.all() if c.status != "superseded"
        ]
        if not candidates:
            for rev in revisions:
                candidates.extend(
                    [c for c in rev.candidates.all() if c.status != "superseded"]
                )
    if candidates:
        for c in candidates:
            facts.append(
                {
                    "type": c.fact_type,
                    "status": c.status,
                    "crm_match_state": getattr(c, "crm_match_state", "not_requested"),
                    "proposed_changes": (
                        c.proposed_changes
                        if isinstance(c.proposed_changes, dict)
                        else {}
                    ),
                }
            )
    elif revisions and revisions[0].extraction:
        for item in revisions[0].extraction:
            if isinstance(item, dict):
                facts.append(
                    {
                        "type": item.get("fact_type", "commitment"),
                        "status": "pending",
                        "crm_match_state": "not_requested",
                        "proposed_changes": item,
                    }
                )
    return facts


def _render_fact_card(fact, thread_project=None):
    ftype = fact.get("type", "commitment")
    status = fact.get("status", "pending")
    crm_state = fact.get("crm_match_state", "not_requested")
    data = fact.get("proposed_changes", {})

    if ftype == "project":
        title_val = (
            data.get("object_name")
            or data.get("deal_title")
            or data.get("name")
            or (thread_project.name if thread_project else "Сделка")
        )
        title = f"Сделка: {title_val}"
        amt_raw = (
            data.get("contract_amount")
            or data.get("amount")
            or data.get("total_amount")
        )
        amt_str = (
            _format_tenge(amt_raw)
            if amt_raw
            else (
                _format_tenge(thread_project.contract_amount)
                if thread_project and thread_project.contract_amount
                else ""
            )
        )
        stage = (
            data.get("stage_id")
            or data.get("stage")
            or data.get("status")
            or (thread_project.get_status_display() if thread_project else "")
        )
        details_parts = []
        if amt_str:
            details_parts.append(f"Сумма: {amt_str}")
        if stage:
            details_parts.append(f"Стадия: {stage}")
        details = " · ".join(details_parts) if details_parts else "Данные сделки"

        if crm_state == "matched":
            badge_text = "Сопоставлено Bitrix"
            badge_cls = "badge-good px-2 py-0.5 rounded text-[10px] font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30"
        elif status == "approved":
            badge_text = "Подтверждено"
            badge_cls = "badge-good px-2 py-0.5 rounded text-[10px] font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30"
        else:
            badge_text = "Ожидает review"
            badge_cls = "badge-warn px-2 py-0.5 rounded text-[10px] font-semibold bg-amber-500/15 text-amber-400 border border-amber-500/30"

        return (
            f'<div class="p-2 rounded bg-gray-900 border border-emerald-500/30 flex items-center justify-between">'
            f'<div>'
            f'<div class="font-bold text-emerald-400">{escape(title)}</div>'
            f'<div class="text-[11px] text-gray-400">{escape(details)}</div>'
            f'</div>'
            f'<span class="{badge_cls}">{escape(badge_text)}</span>'
            f'</div>'
        )

    elif ftype == "payment":
        amt_raw = data.get("payment_amount") or data.get("amount") or data.get("sum")
        amt_str = _format_tenge(amt_raw) if amt_raw else ""
        ptype = data.get("payment_type") or data.get("title") or "Аванс"
        title = f"Платеж: {ptype} {amt_str}".strip() if amt_str else f"Платеж: {ptype}"
        contract = data.get("contract_number") or data.get("contract")
        details = (
            f"Договор №{contract}"
            if contract
            else (f"Сумма: {amt_str}" if amt_str else "Платеж зафиксирован")
        )

        if status == "approved":
            badge_text = "Подтвержден"
            badge_cls = "badge-good px-2 py-0.5 rounded text-[10px] font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30"
        else:
            badge_text = "Ожидает review"
            badge_cls = "badge-warn px-2 py-0.5 rounded text-[10px] font-semibold bg-amber-500/15 text-amber-400 border border-amber-500/30"

        return (
            f'<div class="p-2 rounded bg-gray-900 border border-teal-500/30 flex items-center justify-between">'
            f'<div>'
            f'<div class="font-bold text-teal-400">{escape(title)}</div>'
            f'<div class="text-[11px] text-gray-400">{escape(details)}</div>'
            f'</div>'
            f'<span class="{badge_cls}">{escape(badge_text)}</span>'
            f'</div>'
        )

    else:  # commitment
        comm_title = (
            data.get("commitment_text")
            or data.get("text")
            or data.get("action")
            or data.get("title")
            or "Обязательство"
        )
        if len(comm_title) > 90:
            comm_title = comm_title[:87] + "..."
        title = f"Обязательство: {comm_title}"

        assignee = (
            data.get("assignee")
            or data.get("executor")
            or data.get("responsible")
            or data.get("manager")
        )
        deadline = data.get("deadline") or data.get("due_date") or data.get("date")
        amt_raw = data.get("amount")
        amt_str = _format_tenge(amt_raw) if amt_raw else ""

        details_parts = []
        if assignee:
            details_parts.append(f"Исполнитель: {assignee}")
        if deadline:
            details_parts.append(f"Срок: {deadline}")
        if amt_str:
            details_parts.append(f"Сумма: {amt_str}")
        details = (
            " · ".join(details_parts)
            if details_parts
            else "Обязательство зафиксировано"
        )

        if status == "approved":
            badge_text = "Подтверждено"
            badge_cls = "badge-good px-2 py-0.5 rounded text-[10px] font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30"
        else:
            badge_text = "Ожидает review"
            badge_cls = "badge-warn px-2 py-0.5 rounded text-[10px] font-semibold bg-amber-500/15 text-amber-400 border border-amber-500/30"

        return (
            f'<div class="p-2 rounded bg-gray-900 border border-amber-500/30 flex items-center justify-between">'
            f'<div>'
            f'<div class="font-bold text-amber-300">{escape(title)}</div>'
            f'<div class="text-[11px] text-gray-400">{escape(details)}</div>'
            f'</div>'
            f'<span class="{badge_cls}">{escape(badge_text)}</span>'
            f'</div>'
        )


def _render_state_badge(state: str) -> str:
    if state == "open":
        return '<span class="badge-warn px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-500/15 text-amber-400 border border-amber-500/30">open (мысль в процессе)</span>'
    elif state == "ready":
        return '<span class="badge-good px-1.5 py-0.5 rounded text-[10px] font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">ready (закончен)</span>'
    return f'<span class="badge-neutral px-1.5 py-0.5 rounded text-[10px] font-semibold bg-slate-500/15 text-slate-400 border border-slate-500/30">{escape(state)}</span>'


def _render_thread_block(thread, children, facts):
    msg_count = getattr(thread, "message_count", 0) or thread.message_links.count()
    msg_text = _pluralize_ru(msg_count, "сообщение", "сообщения", "сообщений") + " WA"
    state_badge = _render_state_badge(thread.state)

    facts_html = ""
    if facts:
        fact_cards = "".join(_render_fact_card(f, thread.project) for f in facts)
        facts_html = f'<div class="ml-4 grid grid-cols-1 sm:grid-cols-2 gap-2 mt-2">{fact_cards}</div>'

    children_html = ""
    if children:
        child_rows = []
        for child in children:
            c_msg_count = (
                getattr(child, "message_count", 0) or child.message_links.count()
            )
            c_msg_text = (
                _pluralize_ru(c_msg_count, "сообщение", "сообщения", "сообщений")
                + " WA"
            )
            c_state_badge = _render_state_badge(child.state)
            c_facts = _get_thread_facts(child)
            c_facts_html = ""
            if c_facts:
                c_cards = "".join(
                    _render_fact_card(f, child.project or thread.project)
                    for f in c_facts
                )
                c_facts_html = f'<div class="ml-4 grid grid-cols-1 sm:grid-cols-2 gap-2 mt-1.5">{c_cards}</div>'

            child_rows.append(
                f'<div class="ml-4 pl-3 border-l-2 border-indigo-400/20 text-gray-300 space-y-1.5 py-1">'
                f'<div class="flex items-center justify-between">'
                f'<div class="flex items-center gap-2">'
                f'<span class="text-indigo-400">↳ 📂</span>'
                f'<span class="font-medium text-white">Поддиалог #{child.id}: {escape(child.topic)}</span>'
                f'{c_state_badge}'
                f'</div>'
                f'<span class="text-gray-500 font-mono text-xs">{c_msg_text}</span>'
                f'</div>'
                f'{c_facts_html}'
                f'</div>'
            )
        children_html = "".join(child_rows)

    return (
        f'<div class="ml-4 pl-3 border-l-2 border-indigo-500/40 space-y-2 py-1.5">'
        f'<div class="flex items-center justify-between text-gray-300">'
        f'<div class="flex items-center gap-2">'
        f'<span>📂</span>'
        f'<span class="font-medium text-white">Тред #{thread.id}: {escape(thread.topic)}</span>'
        f'{state_badge}'
        f'</div>'
        f'<span class="text-gray-500 font-mono text-xs">{msg_text}</span>'
        f'</div>'
        f'{facts_html}'
        f'{children_html}'
        f'</div>'
    )


@admin.register(WhatsAppHistoryRun)
class WhatsAppHistoryRunAdmin(IntegrationAdmin):
    change_form_template = "admin/history_run_change.html"
    list_display = (
        "id",
        "job",
        "state",
        "thematic_summary",
        "fetched_count",
        "imported_count",
        "existing_count",
        "analysis_progress",
        "created_at",
        "finished_at",
    )
    list_filter = ("state", "job")
    search_fields = ("job__config__name", "error_code", "status_message")
    fields = (
        "job",
        "state",
        "status_message",
        "error_code",
        "thematic_tree_card",
        "fetched_count",
        "imported_count",
        "existing_count",
        "no_text_count",
        "scheduled_count",
        "analysis_progress",
        "analysis_errors",
        "analysis_coverage_links",
        "results_links",
        "source_snapshot",
        "settings_snapshot",
        "scan_number",
        "stable_scans",
        "cutoff_at",
        "last_check",
        "next_check",
        "requested_by",
        "created_at",
        "updated_at",
        "finished_at",
    )
    readonly_fields = fields
    list_per_page = 25

    @admin.display(description="Последняя успешная проверка")
    def last_check(self, obj):
        return obj.job.last_checked_at or "—"

    @admin.display(description="Следующая проверка")
    def next_check(self, obj):
        return obj.job.next_run_at or "—"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return (
            super().get_queryset(request).select_related("job__config", "requested_by")
        )

    @admin.display(description="Обработка сообщений")
    def analysis_progress(self, obj):
        if not obj.pk:
            return "—"
        value = progress(obj)
        from .history_analysis import summary
        return summary(value)

    @admin.display(description="Покрытие и причины отсутствия данных")
    def analysis_coverage_links(self, obj):
        return format_html('<a href="{}?run__id__exact={}&state__exact=succeeded">Результаты сообщений</a> · <a href="{}?run__id__exact={}&needs_attention=1">Нет данных / ошибки и причины</a>', reverse("admin:api_historyanalysisitem_changelist"), obj.id, reverse("admin:api_historyanalysisitem_changelist"), obj.id)

    @admin.display(description="Причины ошибок анализа")
    def analysis_errors(self, obj):
        from collections import Counter

        if obj.settings_snapshot.get("analysis_policy"):
            return format_html_join("<br>", "{}: {}", obj.analysis_items.exclude(reason_code="").exclude(disposition__in=["facts", "no_facts", "no_text"]).values_list("reason_code", "reason_description")[:30]) or "Ошибок и недостатка данных нет."

        if obj.settings_snapshot.get("analysis_mode") == "reprocess_all":
            codes = OutboxEvent.objects.filter(
                event_type="extract_message", payload__history_run_id=obj.id,
                state__in=["failed", "unknown", "done", "cancelled"],
            ).exclude(
                deduplication_key__in=MessageProcessingTrace.objects.filter(
                    operation_key__in=OutboxEvent.objects.filter(
                        event_type="extract_message", payload__history_run_id=obj.id,
                    ).values("deduplication_key"), status="success",
                ).values("operation_key"),
            ).values_list("error_code", flat=True)
        else:
            latest = MessageProcessingTrace.objects.filter(
                raw_message_id=OuterRef("pk")
            ).order_by("-attempt_no", "-id")
            codes = (
                obj.messages.filter(processing_state="failed")
                .annotate(latest_error=Subquery(latest.values("error_code")[:1]))
                .values_list("latest_error", flat=True)
            )
        counts = Counter(code or "unknown" for code in codes)
        rows = format_html_join(
            "",
            "<li>{} — {} <code>{}</code></li>",
            (
                (count, "Задание заменено тематическим разбором" if code == "replaced_by_thread_backfill" else ERRORS.get(code, "Причина не записана"), code)
                for code, count in counts.most_common()
            ),
        )
        return format_html(
            '<div data-testid="history-analysis-errors"><p>{}</p><ul>{}</ul></div>',
            "Ошибок анализа нет."
            if not counts
            else "Ошибки последних попыток:",
            rows,
        )

    @admin.display(description="Результаты")
    def results_links(self, obj):
        return format_html(
            '<a href="{}?history_run={}">Сообщения</a> · <a href="{}?history_run={}">Трассы анализа</a> · <a href="{}?history_run={}">Шаги в очереди</a>',
            reverse("admin:api_rawmessage_changelist"),
            obj.id,
            reverse("admin:api_messageprocessingtrace_changelist"),
            obj.id,
            reverse("admin:api_outboxevent_changelist"),
            obj.id,
        )

    @admin.display(description="Тематический разбор")
    def thematic_summary(self, obj):
        if not obj or not obj.pk:
            return "—"
        thread_ids = (
            ThreadMessage.objects.filter(raw_message__in=obj.messages.all())
            .values_list("thread_id", flat=True)
            .distinct()
        )
        if not thread_ids:
            return "0 тредов"

        threads = list(DialogueThread.objects.filter(id__in=thread_ids))
        total_threads = len(threads)
        if total_threads == 0:
            return "0 тредов"

        open_count = sum(1 for t in threads if t.state == "open")
        ready_count = sum(1 for t in threads if t.state == "ready")
        other_count = total_threads - open_count - ready_count

        state_parts = []
        if open_count:
            state_parts.append(f"{open_count} open")
        if ready_count:
            state_parts.append(f"{ready_count} ready")
        if other_count:
            state_parts.append(f"{other_count} other")
        state_str = f" ({' / '.join(state_parts)})" if state_parts else ""

        commitments_count = (
            FactCandidate.objects.filter(
                thread_revision__thread_id__in=thread_ids,
                fact_type="commitment",
            )
            .exclude(status="superseded")
            .count()
        )
        if commitments_count == 0:
            revisions = ThreadRevision.objects.filter(thread_id__in=thread_ids)
            for rev in revisions:
                if rev.extraction and isinstance(rev.extraction, list):
                    commitments_count += sum(
                        1
                        for item in rev.extraction
                        if isinstance(item, dict)
                        and item.get("fact_type") == "commitment"
                    )

        threads_label = _pluralize_ru(total_threads, "тред", "треда", "тредов")
        commitments_label = _pluralize_ru(
            commitments_count, "обязательство", "обязательства", "обязательств"
        )
        return f"{threads_label}{state_str} · {commitments_label}"

    @admin.display(description="Иерархический разбор диалогов")
    def thematic_tree_card(self, obj):
        if not obj or not obj.pk:
            return format_html(
                '<div class="text-sm text-gray-500">Нет данных импорта</div>'
            )
        thread_ids = list(
            ThreadMessage.objects.filter(raw_message__in=obj.messages.all())
            .values_list("thread_id", flat=True)
            .distinct()
        )
        if not thread_ids:
            return mark_safe(
                '<style>'
                '.unfold-card { background-color: #111827; border: 1px solid #1f2937; border-radius: 0.75rem; }'
                '</style>'
                '<div class="unfold-card p-5 border border-indigo-900/40 bg-gradient-to-b from-gray-900 to-[#0c1222] rounded-xl text-gray-400 text-xs">'
                '<div class="flex items-center justify-between pb-2 border-b border-gray-800">'
                '<h3 class="text-sm font-bold text-white uppercase tracking-wider flex items-center gap-2">'
                '<span>🌳 Иерархический разбор импорта по Проектам и Тредсам диалогов</span>'
                '</h3>'
                '<span class="text-xs text-gray-500 font-mono">0 диалогов · Диалоги не распознаны</span>'
                '</div>'
                '<p class="mt-2 text-gray-400">В данном импорте тематические диалоги пока не распознаны или сообщения ещё не обработаны AI.</p>'
                '</div>'
            )

        child_qs = (
            DialogueThread.objects.annotate(
                message_count=Count("message_links", distinct=True)
            ).prefetch_related("revisions__candidates")
        )

        threads_qs = (
            DialogueThread.objects.filter(id__in=thread_ids)
            .select_related("project__company")
            .prefetch_related(
                Prefetch("children", queryset=child_qs),
                "revisions__candidates",
            )
            .annotate(message_count=Count("message_links", distinct=True))
        )
        threads = list(threads_qs)
        thread_ids_set = set(thread_ids)

        companies_map = {}
        total_facts_count = 0
        all_threads_count = 0

        for t in threads:
            is_root = (t.parent_id is None) or (t.parent_id not in thread_ids_set)
            if not is_root:
                continue

            company = t.project.company if (t.project and t.project.company) else None
            company_key = company.id if company else None
            if company_key not in companies_map:
                companies_map[company_key] = {
                    "company": company,
                    "name": company.name if company else "Без контрагента",
                    "deals": {},
                }

            deal = t.project
            deal_key = deal.id if deal else None
            deals_map = companies_map[company_key]["deals"]
            if deal_key not in deals_map:
                deals_map[deal_key] = {
                    "project": deal,
                    "name": deal.name if deal else "Общие вопросы",
                    "threads": [],
                }

            children = list(t.children.all())
            facts = _get_thread_facts(t)
            deals_map[deal_key]["threads"].append((t, children, facts))

            all_threads_count += 1 + len(children)
            total_facts_count += len(facts)
            for child in children:
                total_facts_count += len(_get_thread_facts(child))

        total_companies_count = len(companies_map)
        companies_summary_str = _pluralize_ru(
            total_companies_count, "компания", "компании", "компаний"
        )
        threads_summary_str = _pluralize_ru(
            all_threads_count, "ветка", "ветки", "веток"
        )
        facts_summary_str = _pluralize_ru(
            total_facts_count, "факт", "факта", "фактов"
        )
        header_stats_str = (
            f"{companies_summary_str} · {threads_summary_str} · {facts_summary_str}"
        )

        companies_html_list = []
        for company_data in companies_map.values():
            company = company_data["company"]
            co_name = company_data["name"]

            co_threads_count = 0
            co_facts_count = 0
            deals_html_list = []

            for deal_data in company_data["deals"].values():
                deal = deal_data["project"]
                deal_threads = deal_data["threads"]
                deal_threads_count = sum(1 + len(ch) for _, ch, _ in deal_threads)
                co_threads_count += deal_threads_count

                threads_html_list = []
                for t, children, facts in deal_threads:
                    co_facts_count += len(facts)
                    for ch in children:
                        co_facts_count += len(_get_thread_facts(ch))
                    threads_html_list.append(_render_thread_block(t, children, facts))

                deal_threads_html = "".join(threads_html_list)

                if deal:
                    try:
                        deal_url = reverse("admin:api_project_change", args=[deal.id])
                        deal_id_str = (
                            f"#{deal.bitrix_id} ↗"
                            if deal.bitrix_id
                            else f"#{deal.id} ↗"
                        )
                        deal_link = f'<a href="{deal_url}" class="text-[11px] text-sky-400 hover:text-sky-300 underline font-mono">{deal_id_str}</a>'
                    except Exception:
                        deal_link = ""
                    deal_amt_str = (
                        f'<span class="text-emerald-400 font-mono text-[11px]">({_format_tenge(deal.contract_amount)})</span>'
                        if getattr(deal, "contract_amount", None)
                        else ""
                    )
                    deals_html_list.append(
                        f'<div class="pt-2 border-t border-gray-800/80 space-y-2">'
                        f'<div class="flex items-center justify-between text-xs">'
                        f'<div class="flex items-center gap-2">'
                        f'<span class="text-sky-400 font-bold">🏗️ Сделка:</span>'
                        f'<span class="font-semibold text-sky-200">{escape(deal.name)}</span>'
                        f'{deal_link}'
                        f'{deal_amt_str}'
                        f'</div>'
                        f'<span class="text-gray-400 font-mono text-[11px]">{_pluralize_ru(deal_threads_count, "тред", "треда", "тредов")}</span>'
                        f'</div>'
                        f'{deal_threads_html}'
                        f'</div>'
                    )
                else:
                    deals_html_list.append(
                        f'<div class="pt-2 border-t border-gray-800/80 space-y-2">'
                        f'<div class="flex items-center justify-between text-xs">'
                        f'<div class="flex items-center gap-2">'
                        f'<span class="text-slate-400 font-bold">💬 Общие вопросы:</span>'
                        f'<span class="font-semibold text-gray-300">Без привязки к сделке</span>'
                        f'</div>'
                        f'<span class="text-gray-400 font-mono text-[11px]">{_pluralize_ru(deal_threads_count, "тред", "треда", "тредов")}</span>'
                        f'</div>'
                        f'{deal_threads_html}'
                        f'</div>'
                    )

            if company:
                try:
                    co_url = reverse("admin:api_company_change", args=[company.id])
                    co_id_str = (
                        f"#{company.bitrix_company_id} ↗"
                        if company.bitrix_company_id
                        else f"#{company.id} ↗"
                    )
                    co_link = f'<a href="{co_url}" class="text-[11px] text-indigo-400 hover:text-indigo-300 underline font-mono">{co_id_str}</a>'
                except Exception:
                    co_link = ""
                co_label = (
                    "Проект (Компания Bitrix)"
                    if getattr(company, "bitrix_company_id", None)
                    else "Компания"
                )
            else:
                co_link = ""
                co_label = "Контрагент"

            co_dialogues_str = _pluralize_ru(
                co_threads_count, "диалог", "диалога", "диалогов"
            )
            co_facts_str = _pluralize_ru(co_facts_count, "факт", "факта", "фактов")
            co_badge_str = f"{co_dialogues_str} · {co_facts_str}"

            deals_content = "".join(deals_html_list)
            companies_html_list.append(
                f'<div class="p-3 rounded-lg bg-gray-950/70 border border-gray-800 space-y-3">'
                f'<div class="flex items-center justify-between font-semibold text-white">'
                f'<div class="flex items-center gap-2">'
                f'<span class="text-indigo-400 font-bold">📁 {co_label}:</span>'
                f'<span class="text-sm font-bold text-indigo-200">{escape(co_name)}</span>'
                f'{co_link}'
                f'</div>'
                f'<span class="badge-good px-2 py-0.5 rounded-full text-[10px] font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">'
                f'{co_badge_str}'
                f'</span>'
                f'</div>'
                f'{deals_content}'
                f'</div>'
            )

        companies_html = "".join(companies_html_list)

        css_styles = (
            "<style>"
            ".unfold-card { background-color: #111827; border: 1px solid #1f2937; border-radius: 0.75rem; }"
            ".badge-good { background-color: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }"
            ".badge-warn { background-color: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }"
            ".badge-info { background-color: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3); }"
            ".badge-neutral { background-color: rgba(148, 163, 184, 0.15); color: #94a3b8; border: 1px solid rgba(148, 163, 184, 0.3); }"
            "</style>"
        )

        return mark_safe(
            f"{css_styles}"
            f'<div class="unfold-card p-5 space-y-4 border-indigo-900/40 bg-gradient-to-b from-gray-900 to-[#0c1222] rounded-xl text-gray-200">'
            f'<div class="flex items-center justify-between pb-3 border-b border-gray-800">'
            f"<div>"
            f'<h3 class="text-sm font-bold text-white uppercase tracking-wider flex items-center gap-2">'
            f"<span>🌳 Иерархический разбор импорта по Проектам и Тредсам диалогов</span>"
            f"</h3>"
            f'<p class="text-xs text-gray-400 mt-0.5">'
            f"Сообщения сгруппированы по компаниям Bitrix24 и связным тематическим веткам"
            f"</p>"
            f"</div>"
            f'<span class="text-xs text-indigo-400 font-mono">{header_stats_str}</span>'
            f"</div>"
            f'<div class="space-y-3 font-sans text-xs">'
            f"{companies_html}"
            f"</div>"
            f"</div>"
        )

    def get_urls(self):
        return [
            path(
                "<int:object_id>/progress/",
                self.admin_site.admin_view(self.run_progress),
                name="api_whatsapphistoryrun_progress",
            ),
            path(
                "<int:object_id>/<str:action>/control/",
                self.admin_site.admin_view(require_POST(self.control)),
                name="api_whatsapphistoryrun_control",
            ),
        ] + super().get_urls()

    def require_view(self, request, object_id):
        run = get_object_or_404(self.get_queryset(request), pk=object_id)
        if not self.has_view_permission(request, run):
            raise PermissionDenied()
        return run

    def run_progress(self, request, object_id):
        run = self.require_view(request, object_id)
        if request.method != "GET":
            from django.http import HttpResponseNotAllowed

            return HttpResponseNotAllowed(["GET"])
        counts = progress(run)
        fields = {
            name: getattr(run, name)
            for name in (
                "status_message",
                "error_code",
                "fetched_count",
                "imported_count",
                "existing_count",
                "no_text_count",
                "scheduled_count",
                "scan_number",
                "stable_scans",
            )
        }
        fields.update(
            state=run.get_state_display(),
            source_snapshot=run.source_snapshot,
            analysis_progress=self.analysis_progress(run),
        )
        for name, value in (
            ("last_check", run.job.last_checked_at),
            ("next_check", run.job.next_run_at),
        ):
            fields[name] = (
                date_format(timezone.localtime(value), "DATETIME_FORMAT")
                if value
                else "—"
            )
        for name in ("cutoff_at", "updated_at", "finished_at"):
            value = getattr(run, name)
            fields[name] = (
                date_format(timezone.localtime(value), "DATETIME_FORMAT")
                if value
                else "—"
            )
        return JsonResponse(
            {
                "state": run.state,
                "label": run.get_state_display(),
                "message": run.status_message,
                "error": run.error_code,
                "fetched": run.fetched_count,
                "imported": run.imported_count,
                "existing": run.existing_count,
                "analysis": counts,
                "fields": fields,
                "updated_at": run.updated_at.isoformat(),
            }
        )

    def control(self, request, object_id, action):
        run = self.require_view(request, object_id)
        if not self.admin_site.get_model_admin(
            WhatsAppHistoryJob
        ).has_change_permission(request, run.job):
            raise PermissionDenied()
        try:
            if action == "reanalyze":
                from .history_jobs import reanalyze_saved
                new_run = reanalyze_saved(run.id, request.user)
                return redirect("admin:api_whatsapphistoryrun_change", new_run.id)
            control_run(run.id, action, request.user)
        except (ValidationError, ProviderUnavailable) as exc:
            messages.error(
                request,
                exc.messages[0]
                if isinstance(exc, ValidationError)
                else ERROR_LABELS.get(str(exc), str(exc)),
            )
        return redirect("admin:api_whatsapphistoryrun_change", run.id)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        obj = self.get_object(request, object_id)
        can_control = bool(
            obj
            and self.admin_site.get_model_admin(
                WhatsAppHistoryJob
            ).has_change_permission(request, obj.job)
        )
        return super().change_view(
            request,
            object_id,
            form_url,
            {
                **(extra_context or {}),
                "history_field_names": self.fields,
                "thematic_tree_card": self.thematic_tree_card(obj) if obj else "",
                "can_control_import": can_control,
                "can_pause_import": can_control and obj.state in IMPORT_STATES,
                "can_resume_import": can_control and obj.state == "paused",
                "can_cancel_import": can_control
                and obj.state in IMPORT_STATES | {"paused", "analyzing"},
            },
        )


class AnalysisAttentionFilter(admin.SimpleListFilter):
    title = "Отсутствие данных и ошибки"
    parameter_name = "needs_attention"

    def lookups(self, request, model_admin):
        return [("1", "Недостаточно данных / ошибки"), ("0", "Готовые результаты")]

    def queryset(self, request, queryset):
        condition = Q(disposition="insufficient_data") | Q(state__in=["failed", "cancelled"])
        if self.value() == "1":
            return queryset.filter(condition)
        if self.value() == "0":
            return queryset.filter(state="succeeded").exclude(condition)
        return queryset


@admin.register(HistoryAnalysisItem)
class HistoryAnalysisItemAdmin(IntegrationAdmin):
    list_display = ("id", "run", "raw_message", "state", "disposition", "reason_code", "reason_description", "trace", "updated_at")
    list_filter = ("run", "state", "disposition", AnalysisAttentionFilter)
    list_per_page = 50
    readonly_fields = ("run", "raw_message", "outbox_event", "trace", "state", "disposition", "reason_code", "reason_description", "updated_at")
    fields = readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        from django.db.models import Case, When, Value, IntegerField
        return super().get_queryset(request).select_related("run", "raw_message", "trace").annotate(
            result_priority=Case(When(disposition="facts", then=Value(0)), When(disposition="no_facts", then=Value(1)), default=Value(2), output_field=IntegerField())
        ).order_by("result_priority", "raw_message__timestamp", "id")


class HistoryRunFilter(admin.SimpleListFilter):
    title = "Запуск импорта"
    parameter_name = "history_run"

    def lookups(self, request, model_admin):
        if not request.user.is_superuser:
            return []
        return [
            (r.id, f"#{r.id} · {r.get_state_display()}")
            for r in WhatsAppHistoryRun.objects.order_by("-id")[:30]
        ]

    def queryset(self, request, queryset):
        if not self.value():
            return queryset
        if not self.value().isdigit():
            return queryset.none()
        run_id = int(self.value())
        name = queryset.model.__name__
        if name == "OutboxEvent":
            run = WhatsAppHistoryRun.objects.filter(pk=run_id).first()
            raw_ids = list(run.messages.values_list("id", flat=True)) if run else []
            return queryset.filter(
                Q(event_type="history_import", payload__history_run_id=run_id)
                | Q(
                    event_type__in=["extract_message", "index_message"],
                    payload__raw_id__in=raw_ids,
                )
            )
        path = (
            "raw_message__history_import_runs__id"
            if name == "MessageProcessingTrace"
            else "history_import_runs__id"
        )
        return queryset.filter(**{path: run_id}).distinct()


@admin.register(OutboxEvent)
class OutboxEventAdmin(ScopedReadOnlyAdmin):
    list_display = (
        "id",
        "task_type",
        "task_state",
        "attempt_count",
        "next_attempt_at",
        "error_code",
        "source_link",
        "created_at",
    )
    list_filter = ("event_type", "state", "error_code", HistoryRunFilter)
    search_fields = ("deduplication_key", "error_code")
    list_per_page = 50
    ordering = ("-id",)

    @admin.display(description="Задание", ordering="event_type")
    def task_type(self, obj):
        return {
            "history_import": "Импорт истории WhatsApp",
            "extract_message": "Анализ сообщения",
            "index_message": "Индексация сообщения",
            "waha_control": "Управление WhatsApp",
            "otp": "Код входа",
            "notification": "Уведомление",
            "delivery_ack": "Подтверждение доставки",
            "crm_sync": "Синхронизация CRM",
            "crm_import": "Импорт CRM",
            "operation": "Запрос ассистенту",
            "attachment": "Обработка вложения",
        }.get(obj.event_type, obj.event_type)

    @admin.display(description="Состояние", ordering="state")
    def task_state(self, obj):
        return {
            "pending": "Ожидает запуска",
            "enqueued": "Передано в очередь",
            "processing": "Выполняется",
            "done": "Выполнено",
            "failed": "Ошибка",
            "unknown": "Результат неизвестен",
            "cancelled": "Отменено",
        }.get(obj.state, obj.state)

    @admin.display(description="Источник")
    def source_link(self, obj):
        if (
            obj.event_type == "history_import"
            and type(obj.payload.get("history_run_id")) is int
        ):
            return format_html(
                '<a href="{}">Импорт #{}</a>',
                reverse(
                    "admin:api_whatsapphistoryrun_change",
                    args=[obj.payload["history_run_id"]],
                ),
                obj.payload["history_run_id"],
            )
        if type(obj.payload.get("raw_id")) is int:
            return format_html(
                '<a href="{}">Сообщение #{}</a>',
                reverse("admin:api_rawmessage_change", args=[obj.payload["raw_id"]]),
                obj.payload["raw_id"],
            )
        return "—"


# Existing source/trace admins keep their established permission scoping.
from .models import RawMessage

for model in (RawMessage, MessageProcessingTrace):
    registered = admin.site.get_model_admin(model)
    registered.list_filter = (*registered.list_filter, HistoryRunFilter)
