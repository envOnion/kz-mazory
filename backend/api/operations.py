import csv
import io
import time
import logging
from asgiref.sync import async_to_sync
from django.db import connection
from django.conf import settings
from django.utils import timezone
from .models import AsyncOperation, UserProfile
from . import access
from .datamart import datamart
from .providers import ProviderUnavailable
from .facts import json_value

logger = logging.getLogger(__name__)


def _execute_operation(pk):
    op = AsyncOperation.objects.select_related("requested_by").get(pk=pk)
    if op.status in ("cancelled", "expired", "succeeded", "failed"):
        return
    if op.expires_at <= timezone.now() or op.access_fingerprint != access.fingerprint(
        op.requested_by
    ):
        AsyncOperation.objects.filter(pk=pk).update(
            status="expired", result={}, error_code="access_or_lifetime_changed"
        )
        return
    if not AsyncOperation.objects.filter(
        pk=pk, status__in=["queued", "running"], expires_at__gt=timezone.now()
    ).update(status="running"):
        return
    try:
        user = op.requested_by
        values = op.request
        prompt = values.get("prompt", "")
        query = " ".join(prompt.lower().replace("↗", "").split())
        period = values.get("period", "this_month")
        profile = UserProfile.objects.filter(user=user).first()
        mode = profile.ai_response_mode if profile else "detailed"
        suggest = profile.ai_auto_suggest_next_actions if profile else True
        quotes = []
        presentation = None
        dynamic = None
        parent_artifact = None
        if op.operation_type == "export":
            mart = datamart.get_sales_kpi_mart(user, period, values)
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(["id", "project_id", "payment_date", "amount", "currency"])
            from .datamart import scoped_projects, period_bounds
            from .models import FinancialRecord

            qs, currency = scoped_projects(user, values)
            start, end, today = period_bounds(period, mart["timezone"])
            from .datamart import confirmed_payments

            rows = (
                confirmed_payments(user, values)
                .filter(
                    payment_date__gte=start,
                    payment_date__lt=min(
                        end, today + __import__("datetime").timedelta(days=1)
                    ),
                )
                .order_by("payment_date", "id")
            )
            if rows.count() > 10000:
                raise ProviderUnavailable("export_limit_use_filters")
            for row in rows.values(
                "id", "project_id", "payment_date", "amount", "currency"
            ).iterator():
                writer.writerow(
                    [
                        row["id"],
                        row["project_id"],
                        row["payment_date"],
                        row["amount"],
                        row["currency"],
                    ]
                )
            result = {
                "filename": "payments.csv",
                "content": buffer.getvalue(),
                "coverage": mart["coverage"],
            }
        else:
            if query == "покажи график поступлений" and not values.get("dialogue"):
                widget = {
                    "type": "chart",
                    "data": datamart.get_sales_chart_dataset(user, period, values),
                }
                text = (
                    widget["data"].get("empty_reason")
                    or "Подтверждённые поступления по дате платежа."
                )
            elif query == "какие обещания просрочены?" and not values.get("dialogue"):
                widget = {
                    "type": "commitments_list",
                    "data": datamart.get_commitments_sla_mart(user, period),
                }
                text = f"На контроле: {widget['data']['total_count']}. Просрочено: {widget['data']['overdue_count']}."
            elif query == "покажи воронку проектов" and not values.get("dialogue"):
                widget = {
                    "type": "project_table",
                    "data": datamart.get_pipeline_mart(user, values),
                }
                text = "Проекты и сведения CRM. Источник стадии и суммы указан; неизвестные значения отмечены отдельно."
            elif query == "покажи kpi команды" and not values.get("dialogue"):
                data = datamart.get_sales_kpi_mart(user, period, values)
                widget = {"type": "kpi_grid", "data": data}
                text = data["insight"]["headline"] + " " + data["coverage"]["message"]
            else:
                from .analytics.data import OperationContext
                from .analytics.host import run

                deadline = time.monotonic() + min(
                    150,
                    max(1, settings.AI_WORKER_TIMEOUT - 30),
                    max(0, (op.expires_at - timezone.now()).total_seconds()),
                )
                context = OperationContext(
                    op.id,
                    user.id,
                    op.access_fingerprint,
                    op.expires_at,
                    deadline,
                    {
                        key: values[key]
                        for key in [
                            "period",
                            "currency",
                            "team_id",
                            "manager_id",
                            "project_id",
                        ]
                        if key in values
                    },
                )
                from .ai_service import analytics_deadline

                deadline_token = analytics_deadline.set(deadline)
                try:
                    history = values.get("history", [])
                    if values.get("dialogue"):
                        from .analytics.dialogue import prepare, recalculate
                        turn, parent_artifact, history = prepare(context, op)
                        if parent_artifact and "period" not in values.get("input", {}):
                            parent_query=parent_artifact.query_plan["blocks"][0]["query"]
                            if parent_query.get("date_range"): context.intent["date_range"]=parent_query["date_range"]
                        patch = turn.resolved_intent.get("patch", {})
                        if patch and parent_artifact:
                            try:
                                dynamic = recalculate(context, parent_artifact.query_plan, patch)
                            except ProviderUnavailable as exc:
                                context.trace.append({'tool':'recalculate','patch':patch,'error':str(exc),
                                                      'validation_errors':exc.diagnostics.get('validation_errors',[])})
                                if str(exc) not in ["plan_requires_months", "crm_history_unavailable"]:
                                    raise
                                message = "План утверждён по месяцам. Перейти к месяцам для сравнения плана и факта?" if str(exc) == "plan_requires_months" else "CRM содержит текущие стадии сделок. Для динамики по датам нужна история изменений; сейчас можно сравнить текущие стадии или ответственных."
                                dynamic = {"text": message, "presentation": None, "quotes": [], "answer_document": {"version": "1.0", "kind": "clarification", "markdown": message, "facts": {}, "artifact_ids": [], "suggested_actions": []}}
                    if dynamic is None:
                        dynamic = async_to_sync(run)(context, prompt, mode, suggest, history)
                    if values.get("dialogue"):
                        turn.resolved_intent = {**turn.resolved_intent, **context.intent}
                        turn.save(update_fields=["resolved_intent"])
                    text, widget = dynamic["text"], None
                    presentation = dynamic["presentation"]
                    quotes = dynamic.get("quotes", [])
                finally:
                    analytics_deadline.reset(deadline_token)
                    context.registry.clear()
                    context.sources.clear()
            if mode == "concise" and not values.get("dialogue"):
                text = text.split("\n\n")[0][:600]
            if mode == "finance" and widget and widget["type"] == "kpi_grid":
                data = widget["data"]
                text = f"Поступления: {data['fact']} {data['currency']}. План: {data['target'] or 'не задан'}. {data['coverage']['message']}"
            result = {
                "resolved_intent": dynamic.get("resolved_intent", {}) if dynamic else {},
                "analytics_trace": dynamic.get("analytics_trace", []) if dynamic else [],
                "prompt": prompt,
                "text": text,
                "widget": widget,
                "presentation": presentation,
                "quotes": quotes,
                "insights": ["Откройте источник показателя для проверки операций."]
                if suggest
                else [],
            }
        # Recheck access after the provider returns; revoked grants invalidate in-flight work.
        user.refresh_from_db()
        op.refresh_from_db()
        if op.status != "running":
            return
        if (
            op.expires_at <= timezone.now()
            or op.access_fingerprint != access.fingerprint(user)
        ):
            AsyncOperation.objects.filter(pk=pk).update(
                status="expired", result={}, error_code="access_changed"
            )
        else:
            from django.db import transaction
            with transaction.atomic():
                locked = AsyncOperation.objects.select_for_update().get(pk=pk)
                if locked.status != "running":
                    return
                if values.get("dialogue"):
                    from .analytics.dialogue import persist
                    if dynamic and dynamic.get("answer_document"):
                        result["answer_document"] = dynamic["answer_document"]
                    persist(op, result, parent_artifact)
                locked.status, locked.result, locked.error_code = "succeeded", json_value(result), ""
                locked.save(update_fields=["status", "result", "error_code"])
    except Exception as exc:
        code = (
            str(exc)[:64]
            if isinstance(exc, ProviderUnavailable)
            else "operation_failed"
        )
        logger.warning(
            "operation_failed operation_id=%s code=%s",
            pk,
            code,
            exc_info=not isinstance(exc, ProviderUnavailable),
        )
        AsyncOperation.objects.filter(pk=pk, status="running").update(
            status="expired" if code == "access_or_lifetime_changed" else "failed",
            result={"resolved_intent": context.intent, "analytics_trace": context.trace} if "context" in locals() else {},
            error_code=code,
        )


def execute_operation(pk):
    # The worker holds the lock for the complete operation, including tool turns.
    # Separate operation namespace prevents collisions with other advisory users.
    if connection.vendor != "postgresql":
        try:
            return _execute_operation(pk)
        finally:
            from .analytics.dialogue import settle
            settle(pk)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [73104, pk])
        acquired = cursor.fetchone()[0]
    if not acquired:
        return
    try:
        _execute_operation(pk)
    finally:
        from .analytics.dialogue import settle
        settle(pk)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [73104, pk])
