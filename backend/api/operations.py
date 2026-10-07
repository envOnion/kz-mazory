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
            rows = confirmed_payments(user, values).filter(
                payment_date__gte=start,
                payment_date__lt=min(end, today + __import__("datetime").timedelta(days=1)),
            ).order_by("payment_date", "id")
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
            if query == "покажи график поступлений":
                widget = {
                    "type": "chart",
                    "data": datamart.get_sales_chart_dataset(user, period, values),
                }
                text = widget["data"].get("empty_reason") or "Подтверждённые поступления по дате платежа."
            elif query == "какие обещания просрочены?":
                widget = {
                    "type": "commitments_list",
                    "data": datamart.get_commitments_sla_mart(user, period),
                }
                text = f"На контроле: {widget['data']['total_count']}. Просрочено: {widget['data']['overdue_count']}."
            elif query == "покажи воронку проектов":
                widget = {
                    "type": "project_table",
                    "data": datamart.get_pipeline_mart(user, values),
                }
                text = "Проекты и сведения CRM. Источник стадии и суммы указан; неизвестные значения отмечены отдельно."
            elif query == "покажи kpi команды":
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
                    {key: values[key] for key in ["period", "currency", "team_id", "manager_id", "project_id"] if key in values},
                )
                from .ai_service import analytics_deadline

                deadline_token = analytics_deadline.set(deadline)
                try:
                    dynamic = async_to_sync(run)(context, prompt, mode, suggest, values.get("history", []))
                    text, widget = dynamic["text"], None
                    presentation = dynamic["presentation"]
                    quotes = dynamic.get("quotes", [])
                finally:
                    analytics_deadline.reset(deadline_token)
                    context.registry.clear()
                    context.sources.clear()
            if mode == "concise":
                text = text.split("\n\n")[0][:600]
            if mode == "finance" and widget and widget["type"] == "kpi_grid":
                data = widget["data"]
                text = f"Поступления: {data['fact']} {data['currency']}. План: {data['target'] or 'не задан'}. {data['coverage']['message']}"
            result = {
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
            AsyncOperation.objects.filter(pk=pk, status="running").update(
                status="succeeded", result=json_value(result), error_code=""
            )
    except Exception as exc:
        code = (
            str(exc)[:64]
            if isinstance(exc, ProviderUnavailable)
            else "operation_failed"
        )
        logger.warning("operation_failed operation_id=%s code=%s", pk, code)
        AsyncOperation.objects.filter(pk=pk, status="running").update(
            status="expired" if code == "access_or_lifetime_changed" else "failed",
            result={},
            error_code=code,
        )


def execute_operation(pk):
    # The worker holds the lock for the complete operation, including tool turns.
    # Separate operation namespace prevents collisions with other advisory users.
    if connection.vendor != "postgresql":
        return _execute_operation(pk)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [73104, pk])
        acquired = cursor.fetchone()[0]
    if not acquired:
        return
    try:
        _execute_operation(pk)
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [73104, pk])
