"""Validated proposals and transactional human review. AI cannot write approved facts."""

from decimal import Decimal
from datetime import datetime
import math
from zoneinfo import ZoneInfo
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import serializers
from . import access
from .deduplication import normalize_deal_name
from .models import (
    Project,
    ProjectRevision,
    StageTransition,
    FactCandidate,
    FactEvidence,
    FinancialRecord,
    Commitment,
    UserProfile,
    AuditEvent,
    OutboxEvent,
    Company,
    PaymentAllocation,
    PaymentScheduleItem,
)
from .security import Conflict


class FactSchema(serializers.Serializer):
    fact_type = serializers.ChoiceField(choices=["project", "payment", "commitment"])
    object_name = serializers.CharField(max_length=255, allow_blank=True, default="")
    company_name = serializers.CharField(max_length=255, allow_blank=True, default="")
    contract_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0, required=False
    )
    cost_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0, required=False
    )
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    currency = serializers.ChoiceField(
        choices=["KZT", "USD", "EUR", "RUB"], default="KZT"
    )
    payment_date = serializers.DateField(required=False, allow_null=True)
    payment_kind = serializers.ChoiceField(
        choices=["increment", "cumulative", "promise", "reversal"], default="increment"
    )
    reverses_id = serializers.IntegerField(min_value=1, required=False)
    stage = serializers.ChoiceField(
        choices=[x[0] for x in Project.STATUS_CHOICES], required=False
    )
    current_action = serializers.CharField(
        max_length=1000, allow_blank=True, default=""
    )
    next_action = serializers.CharField(max_length=1000, allow_blank=True, default="")
    commitment_text = serializers.CharField(
        max_length=1000, allow_blank=True, default=""
    )
    deadline_at = serializers.DateTimeField(required=False, allow_null=True)
    deadline_precision = serializers.ChoiceField(
        choices=["unknown", "date", "datetime"], default="unknown"
    )
    sender_phone = serializers.CharField(max_length=32, allow_blank=True, default="")
    evidence = serializers.CharField(max_length=32000, allow_blank=False)
    confidence = serializers.FloatField(min_value=0, max_value=1, default=0)
    uncertainties = serializers.ListField(
        child=serializers.CharField(max_length=255), max_length=20, default=list
    )

    def validate_confidence(self, value):
        if not math.isfinite(value):
            raise serializers.ValidationError(
                "Уверенность должна быть конечным числом."
            )
        return value

    def validate(self, data):
        if data["fact_type"] == "payment" and "amount" not in data:
            raise serializers.ValidationError("Для платежа требуется сумма.")
        if data["fact_type"] == "commitment" and not data.get("commitment_text"):
            raise serializers.ValidationError("Требуется текст обязательства.")
        if data.get("deadline_at") is None:
            data["deadline_precision"] = "unknown"
        return data


def json_value(value):
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_value(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def snapshot(project):
    fields = (
        "id",
        "name",
        "contract_amount",
        "cost_amount",
        "cost_confirmed",
        "paid_amount",
        "due_amount",
        "currency",
        "status",
        "team_id",
        "manager_id",
        "company_id",
        "current_action",
        "next_action",
        "version",
    )
    return json_value({field: getattr(project, field) for field in fields})


def record_revision(project, actor, previous_stage=""):
    revision = ProjectRevision.objects.create(
        project=project,
        version=project.version,
        approved_by=actor,
        snapshot=snapshot(project),
    )
    if project.status != previous_stage:
        StageTransition.objects.create(
            project=project,
            project_revision=revision,
            from_stage=previous_stage,
            to_stage=project.status,
        )
    return revision


def review(candidate_id, user, action, reason="", changes=None, base_version=None):
    with transaction.atomic():
        candidate = (
            access.candidates_for(user)
            .select_for_update(of=("self",))
            .get(pk=candidate_id)
        )
        access.require_review(user, candidate)
        if candidate.status in ("approved", "rejected"):
            if candidate.status != ("approved" if action == "approve" else "rejected"):
                raise Conflict("Предложение уже рассмотрено с другим решением.")
            return candidate
        if candidate.status != "pending":
            raise Conflict("Предложение заменено новой версией.")
        if action not in ("approve", "reject"):
            raise serializers.ValidationError("Неизвестное действие.")
        if action == "reject" and not reason.strip():
            raise serializers.ValidationError("Укажите причину отклонения.")
        if (
            changes
            and any(candidate.proposed_changes.get(k) != v for k, v in changes.items())
            and not reason.strip()
        ):
            raise serializers.ValidationError(
                "Укажите причину исправления извлечённых значений."
            )
        data = {**candidate.proposed_changes, **(changes or {})}
        schema = FactSchema(data=data)
        schema.is_valid(raise_exception=True)
        data = schema.validated_data
        if data["fact_type"] != candidate.fact_type:
            raise serializers.ValidationError("Тип факта изменить нельзя.")
        if action == "approve":
            project = (
                Project.objects.select_for_update(of=("self",)).get(
                    pk=candidate.project_id
                )
                if candidate.project_id
                else None
            )
            if project and (
                base_version != project.version
                or candidate.base_project_version != project.version
            ):
                raise Conflict()
            before = snapshot(project) if project else {}
            old_stage = project.status if project else ""
            if candidate.fact_type == "project":
                if project is None:
                    if not data["object_name"] or data.get("contract_amount", 0) <= 0:
                        raise serializers.ValidationError(
                            "Нужны название и положительная сумма договора."
                        )
                    # Team-scoped canonical identity, never fuzzy auto-merging.
                    normalized = normalize_deal_name(data["object_name"])
                    if Project.objects.filter(
                        team=candidate.team, normalized_name=normalized, archived=False
                    ).exists():
                        raise Conflict(
                            "Объект уже существует. Сопоставьте предложение с ним."
                        )
                    company = None
                    if data.get("company_name"):
                        company, _ = Company.objects.get_or_create(
                            name=data["company_name"]
                        )
                    project = Project(
                        team=candidate.team,
                        manager=candidate.manager,
                        company=company,
                        name=data["object_name"],
                        normalized_name=normalized,
                        source="chat",
                    )
                if (
                    project.pk
                    and data["currency"] != project.currency
                    and project.financial_records.exists()
                ):
                    raise serializers.ValidationError(
                        "Валюта проекта с платежами неизменна."
                    )
                if any(key in data for key in ("cost_amount", "contract_amount")):
                    access.require_team_role(user, candidate.team_id, ["finance"])
                project.currency = data["currency"]
                for field in (
                    "contract_amount",
                    "cost_amount",
                    "current_action",
                    "next_action",
                ):
                    if field in data and data[field] != "":
                        setattr(project, field, data[field])
                if "cost_amount" in data:
                    project.cost_confirmed = True
                if "stage" in data:
                    project.status = data["stage"]
            elif not project:
                raise serializers.ValidationError(
                    "Сначала сопоставьте и подтвердите проект."
                )
            elif candidate.fact_type == "payment":
                if data["payment_kind"] not in ("increment", "reversal"):
                    raise serializers.ValidationError(
                        "Обещание или накопительный итог не являются новым платежом."
                    )
                if not data.get("payment_date"):
                    raise serializers.ValidationError(
                        "Укажите подтвержденную дату платежа."
                    )
                if data["payment_date"] > timezone.localdate():
                    raise serializers.ValidationError(
                        "Полученный платёж не может иметь будущую дату."
                    )
                if (
                    not reason.strip()
                    and FinancialRecord.objects.filter(
                        project=project,
                        is_verified=True,
                        amount=data["amount"],
                        payment_date=data["payment_date"],
                        currency=data["currency"],
                    ).exists()
                ):
                    raise serializers.ValidationError(
                        "Найден совпадающий платёж. Укажите, почему это отдельная операция."
                    )
                if data["currency"] != project.currency:
                    raise serializers.ValidationError(
                        "Валюта платежа должна совпадать с валютой проекта."
                    )
                reversal = None
                if data["payment_kind"] == "reversal":
                    reversal = FinancialRecord.objects.filter(
                        pk=data.get("reverses_id"), project=project, is_verified=True
                    ).first()
                    if not reversal or data["amount"] >= 0:
                        raise serializers.ValidationError(
                            "Укажите исходный платеж и отрицательную сумму корректировки."
                        )
                    reversed_total = reversal.financialrecord_set.aggregate(
                        s=Sum("amount")
                    )["s"] or Decimal(0)
                    if reversal.amount + reversed_total + data["amount"] < 0:
                        raise serializers.ValidationError(
                            "Корректировка превышает исходную оплату."
                        )
                elif data["amount"] <= 0:
                    raise serializers.ValidationError(
                        "Платеж должен быть положительным."
                    )
                payment = FinancialRecord.objects.create(
                    project=project,
                    candidate=candidate,
                    source_key=candidate.source_key,
                    amount=data["amount"],
                    currency=data["currency"],
                    payment_date=data["payment_date"],
                    credited_profile=reversal.credited_profile
                    if reversal
                    else project.manager,
                    status="received",
                    is_verified=True,
                    reverses=reversal,
                    notes="Подтверждено по источнику; подробности в истории проверки.",
                )
                if reversal:
                    # Reversal first consumes unallocated money, then reverses allocation entries,
                    # preserving every original entry and keeping aged receivables consistent.
                    from django.db.models import Q

                    related = PaymentAllocation.objects.filter(
                        Q(financial_record=reversal)
                        | Q(financial_record__reverses=reversal)
                    )
                    allocated = related.aggregate(s=Sum("amount"))["s"] or Decimal(0)
                    remaining = max(
                        Decimal(0),
                        -data["amount"]
                        - (reversal.amount + reversed_total - allocated),
                    )
                    grouped = list(
                        related.values("schedule_item_id")
                        .annotate(total=Sum("amount"))
                        .order_by("schedule_item_id")
                    )
                    for row in grouped:
                        if remaining <= 0:
                            break
                        take = min(remaining, row["total"])
                        if take > 0:
                            PaymentAllocation.objects.create(
                                financial_record=payment,
                                schedule_item_id=row["schedule_item_id"],
                                amount=-take,
                            )
                            remaining -= take
                project.paid_amount = project.financial_records.filter(
                    is_verified=True, status="received"
                ).aggregate(s=Sum("amount"))["s"] or Decimal(0)
            elif candidate.fact_type == "commitment":
                deadline = data.get("deadline_at")
                Commitment.objects.create(
                    project=project,
                    manager=candidate.manager or project.manager,
                    source_message=candidate.trace.raw_message,
                    candidate=candidate,
                    commitment_text=data["commitment_text"],
                    deadline_at=deadline,
                    original_deadline_at=deadline,
                    deadline=timezone.localtime(deadline).date() if deadline else None,
                    deadline_precision=data["deadline_precision"],
                    is_verified=True,
                )
            if project.pk:
                project.paid_amount = project.financial_records.filter(
                    is_verified=True, status="received"
                ).aggregate(s=Sum("amount"))["s"] or Decimal(0)
            project.is_verified = True
            project.version += 1
            project.needs_bitrix_sync = True
            project.save()
            candidate.project = project
            candidate.proposed_changes = json_value(data)
            record_revision(project, user, old_stage)
            AuditEvent.objects.create(
                actor=user,
                target_type="Project",
                target_id=project.id,
                action="approve_fact",
                before_after={
                    "before": before,
                    "after": snapshot(project),
                    "candidate_id": candidate.id,
                },
            )
            OutboxEvent.objects.get_or_create(
                deduplication_key=f"crm:{project.id}:{project.version}",
                defaults={
                    "event_type": "crm_sync",
                    "payload": {"project_id": project.id, "version": project.version},
                },
            )
        candidate.status = "approved" if action == "approve" else "rejected"
        candidate.reviewed_by, candidate.reviewed_at, candidate.review_reason = (
            user,
            timezone.now(),
            reason,
        )
        candidate.save()
        AuditEvent.objects.create(
            actor=user,
            target_type="FactCandidate",
            target_id=candidate.id,
            action=action,
            before_after={"reason": reason},
        )
        return candidate


def allocate_payment(user, payment_id, schedule_id, amount):
    with transaction.atomic():
        payment = (
            FinancialRecord.objects.select_for_update(of=("self",))
            .select_related("project")
            .get(pk=payment_id)
        )
        access.require_team_role(user, payment.project.team_id, ["finance"])
        schedule = PaymentScheduleItem.objects.select_for_update(of=("self",)).get(
            pk=schedule_id, project=payment.project, is_verified=True
        )
        existing = PaymentAllocation.objects.filter(
            financial_record=payment, schedule_item=schedule
        ).first()
        amount = Decimal(str(amount))
        if existing:
            if existing.amount == amount:
                return existing
            raise Conflict(
                "Распределение уже существует. Создайте корректировку платежа."
            )
        if (
            not payment.is_verified
            or payment.status != "received"
            or payment.currency != schedule.currency
            or amount <= 0
        ):
            raise serializers.ValidationError("Некорректное распределение оплаты.")
        used = payment.allocations.aggregate(s=Sum("amount"))["s"] or Decimal(0)
        paid = schedule.allocations.aggregate(s=Sum("amount"))["s"] or Decimal(0)
        reversed_amount = payment.financialrecord_set.aggregate(s=Sum("amount"))[
            "s"
        ] or Decimal(0)
        if (
            used + amount > payment.amount + reversed_amount
            or paid + amount > schedule.amount
        ):
            raise serializers.ValidationError(
                "Распределение превышает доступный остаток."
            )
        obj = PaymentAllocation.objects.create(
            financial_record=payment, schedule_item=schedule, amount=amount
        )
        AuditEvent.objects.create(
            actor=user,
            target_type="PaymentAllocation",
            target_id=obj.id,
            action="allocate",
            before_after={"amount": str(amount)},
        )
        return obj
