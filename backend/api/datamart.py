"""One scoped Decimal data mart for dashboards, chat, profiles and exports."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from django.db.models import Sum, Q, Count, Min, Max
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from . import access
from .models import (
    FinancialRecord,
    SalesTarget,
    Project,
    Team,
    StageTransition,
    PaymentScheduleItem,
    UserProfile,
)

ZERO = Decimal("0.00")


def money(value):
    return str((value or ZERO).quantize(Decimal(".01")))


def formatted(value, currency="KZT"):
    return f"{value:,.2f}".replace(",", " ") + (
        " ₸" if currency == "KZT" else f" {currency}"
    )


def month_after(day):
    return date(
        day.year + (day.month == 12), 1 if day.month == 12 else day.month + 1, 1
    )


def period_bounds(period, zone="Asia/Almaty", today=None):
    today = today or timezone.now().astimezone(ZoneInfo(zone)).date()
    this = today.replace(day=1)
    if period == "this_month":
        start, end = this, month_after(this)
    elif period == "last_month":
        start, end = (this - timedelta(days=1)).replace(day=1), this
    elif period == "quarter":
        start = date(today.year, 1 + 3 * ((today.month - 1) // 3), 1)
        end = month_after(month_after(month_after(start)))
    elif period == "year":
        start, end = date(today.year, 1, 1), date(today.year + 1, 1, 1)
    else:
        raise ValidationError("Неизвестный период.")
    return start, end, today


def scoped_projects(user, filters=None, *, using="default"):
    filters = filters or {}
    qs = (
        access.projects_for(user)
        .using(using)
        .filter(
            Q(is_verified=True)
            | Q(identity_confirmed=True)
            | Q(
                financial_records__is_verified=True,
                financial_records__status="received",
                financial_records__direction="income",
                financial_records__amount_precision="exact",
            )
        )
        .exclude(archived=True)
        .distinct()
    )
    for field in ("team_id", "manager_id", "project_id"):
        if filters.get(field):
            value = filters[field]
            if not str(value).isdigit():
                raise ValidationError("Некорректный фильтр.")
            qs = qs.filter(**{("id" if field == "project_id" else field): int(value)})
    currency = filters.get("currency", "KZT")
    if currency not in ("KZT", "USD", "EUR", "RUB"):
        raise ValidationError("Неподдерживаемая валюта.")
    return qs.filter(currency=currency), currency


def confirmed_payments(user, filters=None, *, using="default"):
    """Payment attribution belongs to the ledger, not the current project owner."""
    filters = filters or {}
    projects, currency = scoped_projects(
        user, {k: v for k, v in filters.items() if k != "manager_id"}, using=using
    )
    qs = FinancialRecord.objects.using(using).filter(
        project__in=projects,
        is_verified=True,
        status="received",
        direction="income",
        amount_precision="exact",
        currency=currency,
    )
    if filters.get("manager_id"):
        qs = qs.filter(credited_profile_id=filters["manager_id"])
    return qs


def project_stage(p):
    if "stage" in p.whatsapp_fields:
        return p.status, p.get_status_display(), "Учёт"
    crm = getattr(p, "crm_snapshot", None)
    if crm and crm.external_stage_id:
        return (
            "crm:" + crm.external_stage_id,
            crm.external_stage_name or crm.external_stage_id,
            "CRM",
        )
    return "unknown", "Стадия не определена", ""


def project_row(p, *, paid_total=None, payments_known=None):
    margin = (
        (p.contract_amount - p.cost_amount) / p.contract_amount * 100
        if p.cost_confirmed and p.contract_amount
        else None
    )
    if paid_total is None or payments_known is None:
        totals = p.financial_records.filter(
            is_verified=True,
            status="received",
            direction="income",
            amount_precision="exact",
            currency=p.currency,
        ).aggregate(s=Sum("amount"), n=Count("id"))
        paid_total, payments_known = totals["s"] or ZERO, bool(totals["n"])
    paid = paid_total
    due = p.contract_amount - paid
    contract_known = p.contract_known or p.contract_amount > 0
    balance_known = contract_known and payments_known
    stage, stage_label, stage_source = project_stage(p)
    crm = getattr(p, "crm_snapshot", None)
    return {
        "id": p.id,
        "name": p.name,
        "company": p.company.name if p.company else "",
        "manager": p.manager.full_name
        if p.manager
        else (crm.external_manager_name if crm else "") or "Не назначен",
        "manager_id": p.manager_id,
        "team_id": p.team_id,
        "version": p.version,
        "currency": p.currency,
        "contract_known": contract_known,
        "payments_known": payments_known,
        "balance_known": balance_known,
        "contract_amount": money(p.contract_amount) if contract_known else None,
        "contract_formatted": formatted(p.contract_amount, p.currency)
        if contract_known
        else "Нет данных",
        "paid_amount": money(paid) if payments_known else None,
        "paid_formatted": formatted(paid, p.currency)
        if payments_known
        else "Нет данных",
        "due_amount": money(due) if balance_known else None,
        "due_formatted": formatted(due, p.currency)
        if balance_known
        else "Не определён",
        "overpayment": money(max(ZERO, -due)) if balance_known else None,
        "cost_amount": money(p.cost_amount) if p.cost_confirmed else None,
        "margin_percent": round(float(margin), 2) if margin is not None else None,
        "margin_alert": margin is not None and margin < 15,
        "status": stage_label,
        "status_code": stage,
        "stage_source": stage_source,
        "crm_amount": money(crm.opportunity)
        if crm and crm.opportunity is not None
        else None,
        "crm_formatted": formatted(crm.opportunity, crm.currency)
        if crm and crm.opportunity is not None
        else None,
        "crm_synced_at": crm.synced_at.isoformat() if crm else None,
        "priority": p.priority,
        "equipment": p.equipment_type,
        "is_verified": p.is_verified,
        "current_action": p.current_action,
        "next_action": p.next_action,
    }


class DataMartService:
    def get_sales_kpi_mart(self, user, period="this_month", filters=None):
        qs, currency = scoped_projects(user, filters)
        profile = UserProfile.objects.filter(user=user).first()
        zone = profile.timezone if profile else "Asia/Almaty"
        start, end, today = period_bounds(period, zone)
        all_payments = confirmed_payments(user, filters)
        payments = all_payments.filter(
            payment_date__gte=start,
            payment_date__lt=min(end, today + timedelta(days=1)),
        )
        fact = payments.aggregate(s=Sum("amount"))["s"] or ZERO
        teams = Team.objects.filter(pk__in=qs.values("team_id"))
        if not teams.exists():
            teams = Team.objects.filter(pk__in=access.team_ids(user))
        complete = (
            teams.exists()
            and not teams.filter(
                Q(history_complete_from__isnull=True)
                | Q(history_complete_from__gt=start)
            ).exists()
        )
        month_count = (end.year - start.year) * 12 + end.month - start.month
        profiles = (
            access.profiles_for(user)
            .filter(
                user__memberships__role="manager",
                user__memberships__status="active",
                user__is_active=True,
            )
            .distinct()
        )
        if (filters or {}).get("manager_id"):
            profiles = profiles.filter(pk=int(filters["manager_id"]))
        if (filters or {}).get("team_id"):
            profiles = profiles.filter(
                user__memberships__team_id=int(filters["team_id"]),
                user__memberships__status="active",
            ).distinct()
        targets = SalesTarget.objects.filter(
            team__in=teams,
            profile__in=profiles,
            currency=currency,
            month__gte=start,
            month__lt=end,
            is_active=True,
        )
        managers = []
        for manager in profiles.order_by("id"):
            own_fact = (
                payments.filter(credited_profile=manager).aggregate(s=Sum("amount"))[
                    "s"
                ]
                or ZERO
            )
            own_targets = targets.filter(profile=manager)
            expected_teams = (
                teams.filter(
                    memberships__user=manager.user, memberships__status="active"
                )
                .distinct()
                .count()
            )
            target = (
                own_targets.aggregate(s=Sum("amount"))["s"]
                if expected_teams
                and own_targets.count() == month_count * expected_teams
                and not (filters or {}).get("project_id")
                else None
            )
            percentage = (
                round(float(own_fact / target * 100), 2)
                if complete and target and target > 0
                else None
            )
            projects = qs.filter(manager=manager)
            managers.append(
                {
                    "id": manager.id,
                    "name": manager.full_name or manager.user.username,
                    "role": manager.role,
                    "avatar": manager.avatar_url,
                    "salesAmount": formatted(own_fact, currency)
                    if complete or payments.filter(credited_profile=manager).exists()
                    else "Нет зарегистрированных оплат",
                    "source_count": payments.filter(credited_profile=manager).count(),
                    "fact": money(own_fact),
                    "targetAmount": money(target) if target is not None else None,
                    "targetFormatted": formatted(target, currency)
                    if target is not None
                    else "План не задан",
                    "kpiPercent": percentage,
                    "kpiBarColor": "green"
                    if percentage is not None and percentage >= 100
                    else "yellow",
                    "statusColor": "green"
                    if percentage is not None and percentage >= 100
                    else "yellow",
                    "dealsCount": projects.count(),
                    "trend": "Полная история" if complete else "Неполная история",
                    "trendPositive": complete,
                    "projects": [
                        project_row(p)
                        for p in projects.select_related("manager", "company")[:50]
                    ],
                }
            )
        managers.sort(key=lambda row: Decimal(row["fact"]), reverse=True)
        total_target = (
            sum(
                (
                    Decimal(m["targetAmount"])
                    for m in managers
                    if m["targetAmount"] is not None
                ),
                ZERO,
            )
            if managers and all(m["targetAmount"] is not None for m in managers)
            else None
        )
        receivables = self.receivables(user, filters, today)
        # Compare to equally elapsed days in previous period, not synthetic multipliers.
        previous_end = start
        if period in ("this_month", "last_month"):
            previous_start = (start - timedelta(days=1)).replace(day=1)
        elif period == "quarter":
            previous_start = (
                date(start.year - 1, 10, 1)
                if start.month == 1
                else date(start.year, start.month - 3, 1)
            )
        else:
            previous_start = date(start.year - 1, 1, 1)
        days = (min(end, today + timedelta(days=1)) - start).days
        compare_end = min(previous_end, previous_start + timedelta(days=days))
        previous = (
            all_payments.filter(
                payment_date__gte=previous_start, payment_date__lt=compare_end
            ).aggregate(s=Sum("amount"))["s"]
            or ZERO
        )
        previous_complete = (
            teams.exists()
            and not teams.filter(
                Q(history_complete_from__isnull=True)
                | Q(history_complete_from__gt=previous_start)
            ).exists()
        )
        growth = (
            round(float((fact - previous) / previous * 100), 2)
            if complete and previous_complete and previous
            else None
        )
        title = f"{start:%d.%m.%Y} — {end - timedelta(days=1):%d.%m.%Y}"
        trend = (
            f"{growth:+.2f}%"
            if growth is not None
            else "Недостаточно данных для сравнения"
        )
        chart = {
            "title": "План и факт поступлений",
            "chart_type": "bar",
            "labels": [m["name"] for m in managers],
            "unit": currency,
            "datasets": [
                {
                    "label": "Подтверждённые поступления",
                    "data": [
                        m["fact"] if complete or m["source_count"] else None
                        for m in managers
                    ],
                    "backgroundColor": "#34d399",
                },
                {
                    "label": "План",
                    "data": [m["targetAmount"] for m in managers],
                    "backgroundColor": "#818cf8",
                },
            ],
        }
        source_count = payments.count()
        history = all_payments.aggregate(
            first=Min("payment_date"), last=Max("payment_date"), count=Count("id")
        )
        reason = (
            ""
            if source_count
            else "За выбранный период поступлений нет. Полнота истории подтверждена."
            if complete
            else (
                "За выбранный период поступления не зарегистрированы. Выберите период с имеющимися операциями."
                if history["count"]
                else "Поступления ещё не зарегистрированы. Сведения и причины отсутствия данных доступны в рабочем кабинете и решениях системы."
            )
        )
        unassigned = payments.exclude(
            credited_profile_id__in=[m["id"] for m in managers]
        ).aggregate(s=Sum("amount"), n=Count("id"))
        if unassigned["n"]:
            chart["labels"].append("Без персонального KPI")
            chart["datasets"][0]["data"].append(money(unassigned["s"]))
            chart["datasets"][1]["data"].append(None)
        chart["empty_reason"] = (
            reason if not source_count and total_target is None else ""
        )
        known_contracts = qs.filter(
            Q(contract_known=True) | Q(contract_amount__gt=0)
        ).aggregate(n=Count("id"), amount=Sum("contract_amount"))
        availability = {
            "period_payments": source_count,
            "all_payments": history["count"],
            "first_payment_date": history["first"].isoformat()
            if history["first"]
            else None,
            "last_payment_date": history["last"].isoformat()
            if history["last"]
            else None,
            "message": reason,
            "manager_count": len(managers),
            "unattributed_payments": unassigned["n"],
            "target_known": total_target is not None,
            "projects": qs.count(),
            "contracts_known": known_contracts["n"],
        }
        return {
            "data_availability": availability,
            "categoryBadge": "ПОДТВЕРЖДЁННЫЕ ДАННЫЕ",
            "queryTitle": "KPI отдела продаж",
            "querySubtitle": f"{title} · {currency}",
            "period": period,
            "periodCode": period,
            "periodLabel": title,
            "updatedAtText": f"Обновлено {timezone.localtime():%d.%m.%Y %H:%M}",
            "currency": currency,
            "period_start": start.isoformat(),
            "period_end_exclusive": end.isoformat(),
            "timezone": zone,
            "coverage": {
                "status": "complete" if complete else "partial",
                "message": "Полная история"
                if complete
                else "Недостаточно данных: полнота истории периода не подтверждена.",
            },
            "fact": money(fact),
            "target": money(total_target) if total_target is not None else None,
            "comparison": {
                "start": previous_start.isoformat(),
                "end_exclusive": compare_end.isoformat(),
                "fact": money(previous),
                "absolute_change": money(fact - previous)
                if previous_complete
                else None,
                "percent": growth,
                "method": "equal_elapsed_days",
            },
            "summaryMetrics": [
                {
                    "id": "receipts",
                    "title": "Подтверждённые поступления",
                    "value": formatted(fact, currency)
                    if source_count or complete
                    else "Нет данных о поступлениях",
                    "trend": trend,
                    "trendPositive": growth is not None and growth >= 0,
                    "icon": "bar-chart",
                },
                {
                    "id": "target",
                    "title": "План периода",
                    "value": formatted(total_target, currency)
                    if total_target is not None
                    else "План не задан",
                    "trend": "Утверждённые планы по месяцам",
                    "trendPositive": True,
                    "icon": "target",
                },
                {
                    "id": "overdue",
                    "title": "Просроченная дебиторка",
                    "value": formatted(Decimal(receivables["overdue"]), currency)
                    if receivables["rows"]
                    else "Нет графика платежей",
                    "trend": "По графику платежей",
                    "trendPositive": False,
                    "icon": "users",
                },
            ],
            "project_summary": {
                "count": qs.count(),
                "contracts_known": known_contracts["n"],
                "contract_amount": money(known_contracts["amount"])
                if known_contracts["n"]
                else None,
                "contract_formatted": formatted(known_contracts["amount"], currency)
                if known_contracts["n"]
                else "Договоры не зарегистрированы",
            },
            "managers": managers,
            "chartData": chart,
            "receivables": receivables,
            "timeline": self.timeline(
                payments,
                start,
                end,
                today,
                currency,
                complete=complete,
                empty_reason=reason if not complete else "",
            ),
            "source_rows": list(
                payments.order_by("payment_date", "id").values(
                    "id",
                    "project_id",
                    "payment_date",
                    "amount",
                    "currency",
                    "candidate_id",
                    "credited_profile_id",
                )[:50]
            ),
            "source_count": payments.count(),
            "source_path": "/finance/payments/?"
            + urlencode({"period": period, **(filters or {})}),
            "definition": "Сумма подтвержденных received-операций по дате платежа, включая корректировки. Бухгалтерская выручка не рассчитывается.",
            "forecast": self.forecast(qs, teams, currency, today, start, end),
            "insight": {
                "badge": "Расчёт по фактам",
                "source": "Реестр подтверждённых платежей",
                "headline": f"За период поступило {formatted(fact, currency)}.",
                "details": "Учитываются доступные проекты и подтверждённые операции. "
                + ("История полная." if complete else "Покрытие истории неполное."),
                "actions": [],
            },
        }

    def forecast(self, projects, teams, currency, today, start, end):
        # Versioned, explainable baseline; forecast is never part of actual receipts.
        current = today.replace(day=1)
        history = current
        for _ in range(3):
            history = (history - timedelta(days=1)).replace(day=1)
        if (
            not teams.exists()
            or teams.filter(
                Q(history_complete_from__isnull=True)
                | Q(history_complete_from__gt=history)
            ).exists()
        ):
            return {
                "available": False,
                "reason": "Нужна подтверждённая полная история трёх закрытых месяцев.",
            }
        if start != current or end != month_after(current):
            return {
                "available": False,
                "reason": "Прогноз рассчитывается только для текущего месяца.",
            }
        historical = (
            FinancialRecord.objects.filter(
                project__in=projects,
                is_verified=True,
                status="received",
                direction="income",
                amount_precision="exact",
                currency=currency,
                payment_date__gte=history,
                payment_date__lt=current,
            ).aggregate(s=Sum("amount"))["s"]
            or ZERO
        )
        actual = (
            FinancialRecord.objects.filter(
                project__in=projects,
                is_verified=True,
                status="received",
                direction="income",
                amount_precision="exact",
                currency=currency,
                payment_date__gte=current,
                payment_date__lte=today,
            ).aggregate(s=Sum("amount"))["s"]
            or ZERO
        )
        remaining = (end - today - timedelta(days=1)).days
        forecast = actual + historical / Decimal((current - history).days) * max(
            remaining, 0
        )
        return {
            "available": True,
            "amount": money(forecast),
            "currency": currency,
            "as_of": today.isoformat(),
            "method": "daily_mean_previous_3_complete_months_v1",
            "history_start": history.isoformat(),
            "reason": "Факт на сегодня + средние дневные поступления трёх закрытых месяцев × оставшиеся дни. Сезонность не учитывается.",
        }

    def timeline(
        self, payments, start, end, today, currency, *, complete=False, empty_reason=""
    ):
        rows = {
            str(day["payment_date"]): money(day["s"])
            for day in payments.values("payment_date").annotate(s=Sum("amount"))
        }
        days = [
            start + timedelta(days=n)
            for n in range(max(0, (min(end, today + timedelta(days=1)) - start).days))
        ]
        return {
            "title": "Поступления по дням",
            "empty_reason": empty_reason,
            "unit": currency,
            "labels": [d.isoformat() for d in days],
            "datasets": [
                {
                    "label": "Факт",
                    "data": [
                        rows.get(d.isoformat(), "0.00" if complete else None)
                        for d in days
                    ],
                    "backgroundColor": "#34d399",
                }
            ],
        }

    def receivables(self, user, filters=None, today=None):
        qs, currency = scoped_projects(user, filters)
        today = today or timezone.localdate()
        buckets = {
            key: ZERO for key in ("not_due", "1_30", "31_60", "61_90", "over_90")
        }
        rows = []
        for item in (
            PaymentScheduleItem.objects.filter(
                project__in=qs,
                is_verified=True,
                currency=currency,
                direction="income",
                amount_precision="exact",
            )
            .annotate(paid=Sum("allocations__amount"))
            .order_by("due_date", "id")
        ):
            remaining = max(ZERO, item.amount - (item.paid or ZERO))
            age = (today - item.due_date).days
            bucket = (
                "not_due"
                if age <= 0
                else "1_30"
                if age <= 30
                else "31_60"
                if age <= 60
                else "61_90"
                if age <= 90
                else "over_90"
            )
            buckets[bucket] += remaining
            rows.append(
                {
                    "id": item.id,
                    "project_id": item.project_id,
                    "amount": money(item.amount),
                    "remaining": money(remaining),
                    "due_date": item.due_date.isoformat(),
                    "bucket": bucket,
                }
            )
        return {
            "buckets": {k: money(v) for k, v in buckets.items()},
            "overdue": money(
                sum((v for k, v in buckets.items() if k != "not_due"), ZERO)
            ),
            "rows": rows,
            "unknown_schedule_projects": qs.exclude(
                payment_schedule__is_verified=True
            ).count(),
            "currency": currency,
        }

    def get_pipeline_mart(self, user, filters=None):
        from .project_workspace import workspace_projects, workspace_rows

        filters = filters or {}
        qs, currency = scoped_projects(user, filters)
        all_projects = list(qs.select_related("company", "manager", "crm_snapshot"))
        listing, _ = workspace_projects(
            user, {**filters, "group": "all", "completeness": "all"}
        )
        projects = workspace_rows(
            user, listing.select_related("company", "manager", "crm_snapshot")[:50]
        )
        groups = {}
        for project in all_projects:
            code, label, source = project_stage(project)
            stage = groups.setdefault(
                code,
                {
                    "code": code,
                    "label": label,
                    "source": source,
                    "count": 0,
                    "contract_count": 0,
                    "volume": ZERO,
                    "crm_count": 0,
                    "crm_volume": ZERO,
                },
            )
            stage["count"] += 1
            if project.contract_known or project.contract_amount > 0:
                stage["volume"] += project.contract_amount
                stage["contract_count"] += 1
            crm = getattr(project, "crm_snapshot", None)
            if crm and crm.opportunity is not None and crm.currency == currency:
                stage["crm_volume"] += crm.opportunity
                stage["crm_count"] += 1
        stages = []
        for stage in groups.values():
            stage["volume_formatted"] = (
                formatted(stage["volume"], currency)
                if stage["contract_count"]
                else "Договоры неизвестны"
            )
            stage["volume"] = (
                money(stage["volume"]) if stage["contract_count"] else None
            )
            stage["crm_volume_formatted"] = (
                formatted(stage["crm_volume"], currency) if stage["crm_count"] else None
            )
            stage["crm_volume"] = (
                money(stage["crm_volume"]) if stage["crm_count"] else None
            )
            stages.append(stage)
        order = [code for code, _ in Project.STATUS_CHOICES]
        stages.sort(
            key=lambda row: (
                row["code"] == "unknown",
                order.index(row["code"]) if row["code"] in order else len(order),
                row["label"],
            )
        )
        known = qs.filter(cost_confirmed=True, contract_amount__gt=0).aggregate(
            contract=Sum("contract_amount"), cost=Sum("cost_amount")
        )
        weighted = (
            round(
                float((known["contract"] - known["cost"]) / known["contract"] * 100), 2
            )
            if known["contract"]
            else None
        )
        margins = [
            ((p.contract_amount - p.cost_amount) / p.contract_amount * 100)
            if p.cost_confirmed and p.contract_amount
            else None
            for p in all_projects
        ]
        coverage = {
            "projects": len(all_projects),
            "contracts_known": sum(
                p.contract_known or p.contract_amount > 0 for p in all_projects
            ),
            "crm_snapshots": sum(
                getattr(p, "crm_snapshot", None) is not None for p in all_projects
            ),
            "known_stages": sum(project_stage(p)[0] != "unknown" for p in all_projects),
            "payments_known": confirmed_payments(user, filters)
            .values("project_id")
            .distinct()
            .count(),
        }
        return {
            "projects": projects,
            "total_count": len(all_projects),
            "stages": stages,
            "next_page": 2 if len(all_projects) > 50 else None,
            "source_path": "/projects/?"
            + urlencode(
                {
                    k: v
                    for k, v in filters.items()
                    if k in ["team_id", "manager_id", "project_id", "currency"]
                }
            ),
            "coverage": coverage,
            "weighted_margin": weighted,
            "margin_distribution": {
                "low_under_15": sum(m is not None and m < 15 for m in margins),
                "norm_15_to_20": sum(m is not None and 15 <= m <= 20 for m in margins),
                "high_over_20": sum(m is not None and m > 20 for m in margins),
                "unknown": sum(m is None for m in margins),
            },
            **self.stage_analytics(
                qs.filter(
                    id__in=[p.id for p in all_projects if "stage" in p.whatsapp_fields]
                )
            ),
        }

    def stage_analytics(self, projects):
        rows = list(
            StageTransition.objects.filter(project__in=projects).order_by(
                "project_id", "effective_at", "id"
            )
        )
        histories = {}
        for row in rows:
            histories.setdefault(row.project_id, []).append(row)
        cohort = [
            events
            for events in histories.values()
            if events[0].from_stage == "" and events[0].to_stage == "lead"
        ]
        reached = sum(
            any(event.to_stage == "completed" for event in events) for events in cohort
        )
        durations = {}
        now = timezone.now()
        for events in histories.values():
            for index, event in enumerate(events):
                finish = (
                    events[index + 1].effective_at if index + 1 < len(events) else now
                )
                if event.to_stage in ("completed", "lost"):
                    continue
                durations.setdefault(event.to_stage, []).append(
                    max(0, (finish - event.effective_at).total_seconds() / 86400)
                )
        return {
            "conversion": {
                "value": round(reached / len(cohort) * 100, 2) if cohort else None,
                "cohort_size": len(cohort),
                "completed": reached,
                "reason": "Наблюдаемые проекты с подтверждённым входом в lead; доля когда-либо достигших completed. Незавершённые входят в знаменатель.",
            },
            "stage_duration_days": [
                {
                    "stage": stage,
                    "mean_days": round(sum(values) / len(values), 2),
                    "observations": len(values),
                }
                for stage, values in durations.items()
            ],
        }

    def get_commitments_sla_mart(self, user, period="this_month"):
        from .notifications import effective_deadline
        from .message_time import KAZAKHSTAN_OFFSET

        start, end, today = period_bounds(period)
        items = []
        ontime = original_ontime = denominator = 0
        for c in (
            access.commitments_for(user)
            .filter(is_verified=True)
            .select_related("project", "manager")
            .order_by("deadline_at", "id")[:500]
        ):
            deadline = effective_deadline(c)
            overdue = (
                c.status in ("pending", "overdue")
                and deadline is not None
                and deadline < timezone.now()
            )
            if (
                deadline
                and start <= deadline.date() < end
                and c.status != "cancelled"
                and deadline <= timezone.now()
            ):
                denominator += 1
                ontime += bool(c.fulfilled_at and c.fulfilled_at <= deadline)
                original_ontime += bool(
                    c.fulfilled_at
                    and c.fulfilled_at <= (c.original_deadline_at or deadline)
                )
            items.append(
                {
                    "id": c.id,
                    "text": c.commitment_text,
                    "project_id": c.project_id,
                    "project_name": c.project.name
                    if c.project
                    else "Общая задача команды",
                    "manager_name": c.manager.full_name
                    if c.manager
                    else c.responsible_name or "Не назначен",
                    "deadline": deadline.isoformat() if deadline else None,
                    "deadline_formatted": deadline.astimezone(
                        KAZAKHSTAN_OFFSET
                    ).strftime("%d.%m.%Y %H:%M")
                    if deadline
                    else "Срок не определён",
                    "status_code": c.status,
                    "status": "Просрочено" if overdue else c.get_status_display(),
                    "status_color": "red"
                    if overdue
                    else "green"
                    if c.status == "fulfilled"
                    else "yellow",
                    "version": c.version,
                    "severity": c.severity,
                    "postponed_reason": c.postponed_reason,
                }
            )
        return {
            "commitments": items,
            "total_count": len(items),
            "fulfilled_count": sum(c["status_code"] == "fulfilled" for c in items),
            "overdue_count": sum(c["status_color"] == "red" for c in items),
            "pending_count": sum(c["status_code"] == "pending" for c in items),
            "slippage_rate_percent": round((1 - ontime / denominator) * 100, 2)
            if denominator
            else None,
            "original_sla_percent": round(original_ontime / denominator * 100, 2)
            if denominator
            else None,
        }

    def get_sales_chart_dataset(self, user, period="this_month", filters=None):
        return self.get_sales_kpi_mart(user, period, filters)["timeline"]


datamart = DataMartService()
