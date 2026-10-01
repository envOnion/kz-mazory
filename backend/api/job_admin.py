from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import OuterRef, Q, Subquery
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import format_html, format_html_join
from django.views.decorators.http import require_POST

from .admin_access import IntegrationAdmin, ScopedReadOnlyAdmin
from .history_jobs import ERROR_LABELS, IMPORT_STATES, control_run, progress, start_job
from .models import (
    AISettings,
    MessageProcessingTrace,
    OutboxEvent,
    WhatsAppHistoryJob,
    WhatsAppHistoryRun,
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
                "description": "Модель, окно контекста, пауза и бюджет задаются в настройках AI. Размер страницы не ограничивает общий объём истории.",
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


@admin.register(WhatsAppHistoryRun)
class WhatsAppHistoryRunAdmin(IntegrationAdmin):
    change_form_template = "admin/history_run_change.html"
    list_display = (
        "id",
        "job",
        "state",
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
        "fetched_count",
        "imported_count",
        "existing_count",
        "no_text_count",
        "scheduled_count",
        "analysis_progress",
        "analysis_errors",
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
        return f"Обработано {value['processed']} / {value['total']}; ошибок: {value['errors']}"

    @admin.display(description="Причины ошибок анализа")
    def analysis_errors(self, obj):
        from collections import Counter

        if obj.settings_snapshot.get("analysis_mode") == "reprocess_all":
            codes = OutboxEvent.objects.filter(
                event_type="extract_message", payload__history_run_id=obj.id,
                state__in=["failed", "unknown", "done"],
            ).exclude(
                payload__trace_id__in=MessageProcessingTrace.objects.filter(
                    operation_key__startswith=f"history:{obj.id}:extract:", status="success",
                ).values("id"),
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
                (count, ERRORS.get(code, "Причина не записана"), code)
                for code, count in counts.most_common()
            ),
        )
        return format_html(
            '<div data-testid="history-analysis-errors"><p>{}</p><ul>{}</ul></div>',
            "Новые запросы AI приостановлены в настройках AI."
            if AISettings.get_active().message_processing_paused
            else "Ошибок анализа нет."
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
            analysis_progress=f"Обработано {counts['processed']} / {counts['total']}; ошибок: {counts['errors']}",
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
                "can_control_import": can_control,
                "can_pause_import": can_control and obj.state in IMPORT_STATES,
                "can_resume_import": can_control and obj.state == "paused",
                "can_cancel_import": can_control
                and obj.state in IMPORT_STATES | {"paused"},
            },
        )


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
