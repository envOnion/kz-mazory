"""Deterministic reports and scoped read API; no LLM arithmetic or writes."""

import hashlib
import json
from datetime import timedelta

from django.db.models import Count, OuterRef, Q, Subquery, Sum
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from .facts import json_value
from rest_framework.response import Response
from rest_framework.views import APIView

from . import access
from .bitrix_config import autonomous_crm_status
from .models import AISettings, BitrixSettings, Commitment, FactCandidate, FactDecision, FactEvent, FinancialRecord, OutboxEvent, PaymentScheduleItem, Project, RawMessage, SourceCheckpoint, Team


def financial_summary(projects, days=30):
    records = FinancialRecord.objects.filter(project__in=projects, is_verified=True, status="received")
    totals = list(records.values("currency", "direction", "amount_precision").annotate(amount=Sum("amount"), count=Count("id")).order_by("currency", "direction", "amount_precision"))
    cutoff = timezone.localdate() - timedelta(days=days)
    series = list(records.filter(payment_date__gte=cutoff).values("payment_date", "currency", "direction", "amount_precision").annotate(amount=Sum("amount")).order_by("payment_date", "currency", "direction"))
    return {"totals": [{**item, "amount": str(item["amount"])} for item in totals], "series": [{**item, "payment_date": item["payment_date"].isoformat(), "amount": str(item["amount"])} for item in series]}


def refresh_reports():
    from .autonomous import refresh_checkpoint
    cfg = AISettings.get_active()
    if not cfg.autonomous_enabled:
        return
    for team in Team.objects.filter(is_active=True):
        projects = Project.objects.filter(team=team, archived=False)
        config_ids = list(RawMessage.objects.filter(config__team=team).values_list("config_id", flat=True).distinct())
        for config_id in config_ids:
            raw = RawMessage.objects.filter(config_id=config_id).select_related("config__team").order_by("-id").first()
            if raw:
                refresh_checkpoint(raw)
        latest = FactEvent.objects.filter(team=team,is_superseded=False).exclude(event_type="daily_report").order_by("-id").first()
        if not latest:
            continue
        coverage = list(SourceCheckpoint.objects.filter(team=team).values("source_scope", "complete_through", "gaps", "counts"))
        from .facts import json_value
        coverage_hash = hashlib.sha256(json.dumps(json_value(coverage), sort_keys=True).encode()).hexdigest()[:16]
        key = f"daily-report:{team.id}:{timezone.localdate()}:{latest.id}:{coverage_hash}"
        if FactEvent.objects.filter(event_key=key).exists():
            continue
        finances = financial_summary(projects)
        tasks = Commitment.objects.filter(team=team, is_verified=True)
        payload = {**finances, "projects": projects.count(), "contract_known": projects.filter(Q(contract_known=True) | Q(contract_amount__gt=0)).count(), "open_commitments": tasks.filter(status__in=("pending", "overdue")).count(), "missing_deadline": tasks.filter(status__in=("pending", "overdue"), deadline_at__isnull=True).count(), "coverage": list(SourceCheckpoint.objects.filter(team=team).values("source_scope", "complete_through", "gaps", "counts")), "source": "WhatsApp", "cutoff": timezone.now().isoformat(), "projection_event_id": latest.id}
        from .facts import json_value
        FactEvent.objects.get_or_create(event_key=key, defaults={"team": team, "event_type": "daily_report", "occurred_at": timezone.now(), "payload": json_value(payload)})


class AutonomousOverviewView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if access.is_client(request.user):
            raise PermissionDenied("Финансовый отчет доступен сотрудникам с доступом к соответствующим проектам.")
        cfg = AISettings.get_active()
        integration = BitrixSettings.objects.first() or BitrixSettings(is_active=False)
        projects = access.projects_for(request.user, include_client=True)
        candidates = access.candidates_for(request.user)
        team_ids = access.team_ids(request.user, ["team_lead", "finance"])
        if request.user.is_superuser:
            team_ids = Team.objects.filter(is_active=True).values_list("id", flat=True)
        latest_decision = FactDecision.objects.filter(candidate_id=OuterRef("candidate_id")).order_by("-id").values("id")[:1]
        decisions = FactDecision.objects.filter(candidate__in=candidates, id=Subquery(latest_decision))
        waiting = []
        # Latest decision only; old failures are not counted as current issues.
        for candidate in candidates.filter(status="pending").prefetch_related("decisions").order_by("-id")[:50]:
            decision = max(candidate.decisions.all(), key=lambda item: item.id, default=None)
            if decision:
                waiting.append({"candidate_id": candidate.id, "project_id": candidate.project_id, "outcome": decision.outcome, "reason_code": decision.reason_code, "explanation": decision.explanation})
        return Response(json_value({
            "enabled": cfg.autonomous_enabled, "crm_enabled": cfg.autonomous_crm_enabled,
            "crm_status": autonomous_crm_status(cfg, integration),
            "policy_version": cfg.autonomous_policy_version, "as_of": timezone.now(),
            "source": "WhatsApp", **financial_summary(projects),
            "decisions": list(decisions.values("outcome").annotate(count=Count("id"))),
            "waiting": waiting,
            "coverage": list(SourceCheckpoint.objects.filter(team_id__in=team_ids).values("source_scope", "complete_through", "gaps", "counts", "updated_at")),
            "reports": list(FactEvent.objects.filter(team_id__in=team_ids, event_type="daily_report").order_by("-id").values("id", "created_at", "payload")[:10]),
            "observations": list(FactEvent.objects.filter(Q(project__in=projects) | Q(decision__candidate__in=candidates)).filter(is_superseded=False,event_type__in=["reported_cumulative", "reported_balance", "reported_debt", "reported_invoice", "reported_transfer", "payment_schedule"]).order_by("-id").values("id", "project_id", "project__name", "event_type", "occurred_at", "payload")[:100]),
            "crm_deliveries": list(OutboxEvent.objects.filter(crmdelivery__external_object_link__team_id__in=team_ids).exclude(state="done").order_by("-id").values("id", "event_type", "state", "error_code", "next_attempt_at")[:50]),
            "schedule": list(PaymentScheduleItem.objects.filter(project__in=projects, is_verified=True, state="active").order_by("due_date").values("id", "project_id", "project__name", "due_date", "amount", "currency", "direction", "amount_precision")[:100]),
        }))
