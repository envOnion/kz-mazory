import csv
import io
from django.db import transaction
from django.utils import timezone
from .models import AsyncOperation, UserProfile
from . import access
from .datamart import datamart
from .ai_service import AIService
from .qdrant_service import qdrant_service
from .providers import ProviderUnavailable
from .facts import json_value


def execute_operation(pk):
    op = AsyncOperation.objects.select_related("requested_by").get(pk=pk)
    if op.status in ("cancelled", "expired", "succeeded"):
        return
    if op.expires_at <= timezone.now() or op.access_fingerprint != access.fingerprint(
        op.requested_by
    ):
        AsyncOperation.objects.filter(pk=pk).update(
            status="expired", result={}, error_code="access_or_lifetime_changed"
        )
        return
    AsyncOperation.objects.filter(pk=pk).update(status="running")
    try:
        user = op.requested_by
        values = op.request
        prompt = values.get("prompt", "")
        query = prompt.lower()
        period = values.get("period", "this_month")
        profile = UserProfile.objects.filter(user=user).first()
        mode = profile.ai_response_mode if profile else "detailed"
        suggest = profile.ai_auto_suggest_next_actions if profile else True
        quotes = []
        if op.operation_type == "export":
            mart = datamart.get_sales_kpi_mart(user, period, values)
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(["id", "project_id", "payment_date", "amount", "currency"])
            from .datamart import scoped_projects, period_bounds
            from .models import FinancialRecord

            qs, currency = scoped_projects(user, values)
            start, end, today = period_bounds(period, mart["timezone"])
            rows = FinancialRecord.objects.filter(
                project__in=qs,
                is_verified=True,
                status="received",
                currency=currency,
                payment_date__gte=start,
                payment_date__lt=min(
                    end, today + __import__("datetime").timedelta(days=1)
                ),
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
            if any(word in query for word in ("график", "диаграм", "chart", "сравни")):
                widget = {
                    "type": "chart",
                    "data": datamart.get_sales_chart_dataset(user, period, values),
                }
                text = "План и подтверждённые поступления за одинаковый период. План не задан там, где серия отсутствует."
            elif any(
                word in query
                for word in ("обещ", "дедлайн", "напомин", "задач", "срок", "просроч")
            ):
                widget = {
                    "type": "commitments_list",
                    "data": datamart.get_commitments_sla_mart(user, period),
                }
                text = f"На контроле: {widget['data']['total_count']}. Просрочено: {widget['data']['overdue_count']}."
            elif any(
                word in query
                for word in ("сделк", "объект", "проект", "воронк", "марж")
            ):
                widget = {
                    "type": "project_table",
                    "data": datamart.get_pipeline_mart(user, values),
                }
                text = "Подтверждённые проекты. Маржа доступна только при подтверждённой стоимости."
            elif any(
                word in query
                for word in ("kpi", "кпи", "план", "команд", "менеджер", "поступлен")
            ):
                data = datamart.get_sales_kpi_mart(user, period, values)
                widget = {"type": "kpi_grid", "data": data}
                text = data["insight"]["headline"] + " " + data["coverage"]["message"]
            else:
                config_ids = list(access.configs_for(user).values_list("id", flat=True))
                quotes = qdrant_service.search(prompt, config_ids=config_ids)
                data = datamart.get_sales_kpi_mart(user, period, values)
                text = AIService.chat_assistant(
                    prompt, {"kpi": json_value(data), "evidence": quotes}, mode, suggest
                )
                widget = None
            if mode == "concise":
                text = text.split("\n\n")[0][:600]
            if mode == "finance" and widget and widget["type"] == "kpi_grid":
                data = widget["data"]
                text = f"Поступления: {data['fact']} {data['currency']}. План: {data['target'] or 'не задан'}. {data['coverage']['message']}"
            result = {
                "prompt": prompt,
                "text": text,
                "widget": widget,
                "quotes": quotes,
                "insights": ["Откройте источник показателя для проверки операций."]
                if suggest
                else [],
            }
        # Recheck access after the provider returns; revoked grants invalidate in-flight work.
        if op.access_fingerprint != access.fingerprint(user):
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
        AsyncOperation.objects.filter(pk=pk, status="running").update(
            status="failed", result={}, error_code=code
        )
