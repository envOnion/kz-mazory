"""Validated proposals and transactional domain application with audited actors."""

from decimal import Decimal
from datetime import datetime
import math
from zoneinfo import ZoneInfo
from django.db import IntegrityError, transaction
from django.db.models import Sum, Q
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
    BitrixSettings,
    CandidateCrmMatch,
    Team,
)
from .security import Conflict
from .message_time import source_zone
from .dialogue_threads import lock_candidate_source


SUPPORTED_CURRENCIES = ("KZT", "USD", "EUR", "RUB")


class EvidenceSchema(serializers.Serializer):
    raw_message_id = serializers.IntegerField(min_value=1)
    quote = serializers.CharField(max_length=32000)
    role = serializers.ChoiceField(choices=["request", "promise", "deadline", "fulfillment", "cancellation", "source", "identity", "amount", "date", "contract", "party"])


class FactSchema(serializers.Serializer):
    thread_key = serializers.CharField(max_length=64, required=False)
    evidence_message_id = serializers.IntegerField(min_value=1, required=False)
    fact_type = serializers.ChoiceField(choices=["project", "payment", "commitment"])
    object_name = serializers.CharField(max_length=255, allow_blank=True, default="")
    company_name = serializers.CharField(max_length=255, allow_blank=True, default="")
    party_role = serializers.ChoiceField(choices=["unknown", "customer", "contractor", "supplier", "payer", "designer"], default="unknown")
    direction = serializers.ChoiceField(choices=["income", "expense"], default="income")
    amount_precision = serializers.ChoiceField(choices=["exact", "approximate", "range", "unknown"], default="exact")
    contract_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0, required=False
    )
    cost_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0, required=False
    )
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    currency = serializers.ChoiceField(choices=SUPPORTED_CURRENCIES, required=False)
    payment_date = serializers.DateField(required=False, allow_null=True)
    payment_kind = serializers.ChoiceField(
        choices=["increment", "cumulative", "promise", "reversal", "balance", "debt", "invoice", "transfer"], default="increment"
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
    assignment_kind = serializers.ChoiceField(choices=["promise", "assignment", "reported_promise"], default="promise")
    responsible_name = serializers.CharField(max_length=255, allow_blank=True, default="")
    commitment_status = serializers.ChoiceField(choices=["pending", "fulfilled", "cancelled"], default="pending")
    deadline_basis = serializers.ChoiceField(choices=["explicit", "morning_default", "unknown"], default="unknown")
    deadline_message_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    fulfillment_message_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    fulfilled_at = serializers.DateTimeField(required=False, allow_null=True)
    commitment_id = serializers.IntegerField(min_value=1, required=False)
    base_commitment_version = serializers.IntegerField(min_value=1, required=False)
    promise_message_id = serializers.IntegerField(min_value=1, required=False)
    evidence_messages = EvidenceSchema(many=True, default=list)
    sender_phone = serializers.CharField(max_length=32, allow_blank=True, default="")
    evidence = serializers.CharField(max_length=32000, allow_blank=False)
    confidence = serializers.FloatField(min_value=0, max_value=1, default=0)
    uncertainties = serializers.ListField(
        child=serializers.CharField(max_length=255), max_length=20, default=list
    )
    in_progress = serializers.BooleanField(required=False, default=False)

    def validate_confidence(self, value):
        if not math.isfinite(value):
            raise serializers.ValidationError(
                "Уверенность должна быть конечным числом."
            )
        return value

    def validate(self, data):
        if data["fact_type"] == "payment" and "amount" not in data and data["amount_precision"] != "unknown":
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


def same_commitment_origin(left, right):
    """Compare the actual promise evidence; wording/date changes are proposals."""
    def promises(data):
        return {(item.get("raw_message_id"), " ".join(item.get("quote", "").split())) for item in data.get("evidence_messages", []) if item.get("role") == "promise"}
    a, b = promises(left), promises(right)
    if a and b:
        return a == b
    return bool(left.get("evidence") and " ".join(left["evidence"].split()) == " ".join(right.get("evidence", "").split()))


def fact_identity(data):
    """Business identity within one message lineage, independent of AI confidence/quote."""
    import json

    fields = {
        "payment": (
            "amount",
            "currency",
            "direction",
            "amount_precision",
            "payment_date",
            "payment_kind",
            "reverses_id",
        ),
        "commitment": ("commitment_text", "deadline_at", "deadline_precision"),
        "project": (
            "company_name",
            "contract_amount",
            "cost_amount",
            "currency",
            "stage",
            "current_action",
            "next_action",
        ),
    }
    kind = data.get("fact_type")
    value = {
        key: json_value(
            "KZT" if key == "currency" and key not in data else "income" if key == "direction" and key not in data else "exact" if key == "amount_precision" and key not in data else data.get(key)
        )
        for key in fields.get(kind, ())
    }
    for key in ("amount", "contract_amount", "cost_amount"):
        if value.get(key) is not None:
            value[key] = str(Decimal(value[key]).normalize())
    value.update(
        fact_type=kind, object_name=normalize_deal_name(data.get("object_name", ""))
    )
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def snapshot(project):
    fields = (
        "id",
        "name",
        "contract_amount",
        "cost_amount",
        "cost_confirmed",
        "contract_known",
        "whatsapp_fields",
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


def _project_identity_conflict(team_id, name, normalized):
    """Check the shared pipeline/CRM canonical key under a team lock."""
    canonical = normalize_deal_name(name)
    identity_values = {value for value in (normalized, canonical) if value}
    projects = Project.objects.select_for_update(of=("self",)).filter(
        team_id=team_id, archived=False
    )
    if identity_values and projects.filter(
        normalized_name__in=identity_values
    ).exists():
        return True
    return any(
        normalize_deal_name(project.name) == canonical
        for project in projects.filter(Q(normalized_name="") | Q(source="bitrix_crm")).only("name")
    )


def _review_currency(data, currency_was_explicit, project=None, crm_match=None):
    if currency_was_explicit:
        return data["currency"]
    if project and project.currency:
        return project.currency
    if crm_match and crm_match.currency in SUPPORTED_CURRENCIES:
        return crm_match.currency
    return "KZT"


def _validate_project_currency(project, currency):
    if (
        project.pk
        and currency != project.currency
        and project.financial_records.exists()
    ):
        raise serializers.ValidationError("Валюта проекта с платежами неизменна.")


def _crm_project_for_match(candidate, crm_match):
    """Resolve a local mirror without inferring identity from mutable names."""
    bitrix_deal_id = crm_match.bitrix_deal_id.strip()
    if not bitrix_deal_id:
        raise serializers.ValidationError("У варианта CRM отсутствует ID сделки.")

    option_project = None
    if crm_match.project_id:
        option_project = Project.objects.select_for_update(of=("self",)).get(
            pk=crm_match.project_id
        )
        if (
            option_project.team_id != candidate.team_id
            or option_project.archived
            or option_project.bitrix_id not in (None, "", bitrix_deal_id)
        ):
            raise Conflict("Локальная связь варианта CRM противоречива.")
        if not option_project.bitrix_id and candidate.project_id != option_project.id:
            raise Conflict("Неподтверждённая локальная связь CRM устарела.")

    external_project = (
        Project.objects.select_for_update(of=("self",))
        .filter(bitrix_id=bitrix_deal_id)
        .first()
    )
    if external_project and (
        external_project.team_id != candidate.team_id or external_project.archived
    ):
        raise Conflict("Сделка CRM уже связана с проектом другой области доступа.")
    if option_project and external_project and option_project.pk != external_project.pk:
        raise Conflict("Сделка CRM имеет несколько противоречивых локальных связей.")
    return option_project or external_project


def select_crm_match(
    candidate_id,
    user,
    crm_match_id,
    crm_match_revision,
    base_version,
    reason,
):
    """Record a human CRM choice without approving the AI fact."""
    reason = reason.strip()
    if not reason:
        raise serializers.ValidationError("Укажите причину выбора сделки CRM.")

    with transaction.atomic():
        candidates = access.candidates_for(user)
        lock_candidate_source(candidates.get(pk=candidate_id))
        candidate = (
            candidates.select_for_update(of=("self",))
            .get(pk=candidate_id)
        )
        access.require_team_role(user, candidate.team_id, ["team_lead"])
        if candidate.status != "pending":
            raise Conflict("Предложение уже рассмотрено или заменено.")
        if candidate.crm_match_state not in ("ambiguous", "matched"):
            raise Conflict("Текущий результат CRM не допускает выбор сделки.")
        if candidate.crm_match_revision != crm_match_revision:
            raise Conflict("Результаты CRM обновились. Перезагрузите карточку.")

        try:
            crm_match = (
                CandidateCrmMatch.objects.select_for_update(of=("self",))
                .select_related("project")
                .get(
                    pk=crm_match_id,
                    candidate=candidate,
                    crm_match_revision=candidate.crm_match_revision,
                )
            )
        except CandidateCrmMatch.DoesNotExist:
            raise serializers.ValidationError(
                "Выбранный вариант не относится к текущему результату CRM."
            ) from None

        project = _crm_project_for_match(candidate, crm_match)
        if candidate.fact_type != "project" and not project:
            raise serializers.ValidationError("Сначала загрузите выбранную сделку в справочник CRM.")
        selected_matches = list(
            CandidateCrmMatch.objects.select_for_update(of=("self",)).filter(
                candidate=candidate, selection_state="selected"
            )
        )
        previous_selected = next(
            (item for item in selected_matches if item.pk != crm_match.pk), None
        )
        current_project = None
        if candidate.project_id:
            current_project = Project.objects.select_for_update(of=("self",)).get(
                pk=candidate.project_id
            )
            if (
                current_project.team_id != candidate.team_id
                or current_project.archived
            ):
                raise Conflict("Текущий проект кандидата недоступен для сопоставления.")
            if project and current_project.pk != project.pk:
                if not previous_selected or previous_selected.project_id != current_project.pk:
                    raise Conflict("Кандидат уже связан с другим локальным проектом.")
            if not project:
                if current_project.bitrix_id not in (
                    None,
                    "",
                    crm_match.bitrix_deal_id.strip(),
                ):
                    if (
                        not previous_selected
                        or previous_selected.project_id != current_project.pk
                    ):
                        raise Conflict(
                            "Сделка CRM конфликтует с текущей локальной связью кандидата."
                        )
                else:
                    project = current_project
        version_project = project
        if (
            version_project is None
            and current_project
            and previous_selected
            and previous_selected.project_id == current_project.pk
        ):
            version_project = current_project
        expected_version = version_project.version if version_project else 0
        if base_version != expected_version:
            raise Conflict()

        already_selected = any(item.pk == crm_match.pk for item in selected_matches)
        already_audited = AuditEvent.objects.filter(
            target_type="FactCandidate",
            target_id=candidate.id,
            action="match_crm",
            before_after__crm_match_id=crm_match.id,
            before_after__crm_match_revision=candidate.crm_match_revision,
        ).exists()
        if (
            already_selected
            and already_audited
            and candidate.crm_match_state == "matched"
            and candidate.project_id == (project.id if project else None)
            and candidate.base_project_version == expected_version
        ):
            return candidate

        previous_ids = [item.id for item in selected_matches]
        for item in selected_matches:
            if item.pk != crm_match.pk:
                item.selection_state = "dismissed"
                item.save(update_fields=["selection_state"])
        if not already_selected:
            crm_match.selection_state = "selected"
        if project and crm_match.project_id != project.id:
            crm_match.project = project
        crm_match.save(update_fields=["selection_state", "project"])

        previous_project_id = candidate.project_id
        candidate.project = project
        candidate.base_project_version = project.version if project else 0
        candidate.crm_match_state = "matched"
        candidate.crm_match_error_code = ""
        candidate.save(
            update_fields=[
                "project",
                "base_project_version",
                "crm_match_state",
                "crm_match_error_code",
            ]
        )
        if candidate.thread_revision_id and project:
            from .models import DialogueThread
            DialogueThread.objects.filter(pk=candidate.thread_revision.thread_id, team_id=candidate.team_id).update(project=project)
        AuditEvent.objects.create(
            actor=user,
            target_type="FactCandidate",
            target_id=candidate.id,
            action="match_crm",
            before_after={
                "previous_crm_match_ids": previous_ids,
                "crm_match_id": crm_match.id,
                "crm_match_revision": candidate.crm_match_revision,
                "bitrix_deal_id": crm_match.bitrix_deal_id,
                "previous_project_id": previous_project_id,
                "project_id": project.id if project else None,
                "reason": reason,
            },
        )
        return candidate


def _selected_crm_match(candidate):
    matches = list(
        CandidateCrmMatch.objects.select_for_update(of=("self",))
        .select_related("project")
        .filter(
            candidate=candidate,
            crm_match_revision=candidate.crm_match_revision,
            selection_state="selected",
        )[:2]
    )
    if candidate.crm_match_state == "matched":
        if len(matches) != 1:
            raise Conflict("Выбранная CRM-сделка не определена однозначно.")
        return matches[0]
    if matches:
        raise Conflict("CRM-состояние кандидата противоречит сохранённому выбору.")
    return None


def _crm_approval_requires_finance(candidate, crm_match, project):
    return bool(
        candidate.fact_type == "project"
        and crm_match
        and crm_match.opportunity is not None
        and project is None
        and candidate.project_id is None
    )


def _company_from_crm_match(crm_match, data):
    company_id = crm_match.bitrix_company_id.strip()
    company_name = (crm_match.company_name or data.get("company_name", "")).strip()
    if not company_id:
        if not company_name:
            return None
        try:
            with transaction.atomic():
                matches = list(Company.objects.filter(name=company_name)[:2])
                if len(matches) > 1:
                    raise Conflict("Название компании неоднозначно без внешнего ID.")
                return matches[0] if matches else Company.objects.create(name=company_name)
        except IntegrityError as exc:
            raise Conflict("Компания конфликтует с существующей записью.") from exc

    company = (
        Company.objects.select_for_update()
        .filter(bitrix_company_id=company_id)
        .first()
    )
    if company:
        return company
    if not company_name:
        raise serializers.ValidationError(
            "В снимке CRM отсутствует название выбранной компании."
        )
    try:
        with transaction.atomic():
            return Company.objects.create(
                name=company_name, bitrix_company_id=company_id
            )
    except IntegrityError as exc:
        raise Conflict("CRM-компания конфликтует с существующей записью.") from exc


def _materialize_crm_project(candidate, crm_match, data):
    bitrix_deal_id = crm_match.bitrix_deal_id.strip()
    name = (crm_match.deal_title or crm_match.object_label or data["object_name"]).strip()
    normalized = normalize_deal_name(name)
    if not bitrix_deal_id or not name or not normalized:
        raise serializers.ValidationError(
            "В выбранном снимке CRM недостаточно данных для создания проекта."
        )
    Team.objects.select_for_update(of=("self",)).get(pk=candidate.team_id)
    if Project.objects.select_for_update().filter(bitrix_id=bitrix_deal_id).exists():
        raise Conflict("Сделка CRM уже связана с другим локальным проектом.")
    if _project_identity_conflict(candidate.team_id, name, normalized):
        raise Conflict(
            "Проект с таким названием уже существует. Сопоставьте его явно."
        )

    company = _company_from_crm_match(crm_match, data)
    contract_amount = (
        crm_match.opportunity
        if crm_match.opportunity is not None
        else data.get("contract_amount", Decimal(0))
    )
    try:
        with transaction.atomic():
            project = Project.objects.create(
                team=candidate.team,
                manager=candidate.manager,
                company=company,
                name=name,
                normalized_name=normalized,
                source="bitrix_crm",
                bitrix_id=bitrix_deal_id,
                contract_amount=contract_amount,
                currency=data["currency"],
            )
    except IntegrityError as exc:
        raise Conflict("CRM-сделка конфликтует с существующим проектом.") from exc
    crm_match.project = project
    crm_match.save(update_fields=["project"])
    return project


def review(candidate_id, user, action, reason="", changes=None, base_version=None):
    if user is None or not user.is_authenticated:
        raise serializers.ValidationError("Требуется авторизованный пользователь.")
    return _apply_candidate(candidate_id, user, action, reason, changes, base_version)


def _apply_candidate(candidate_id, user, action, reason="", changes=None, base_version=None, *, system=False):
    with transaction.atomic():
        candidates = FactCandidate.objects.filter(team__is_active=True) if system else access.candidates_for(user)
        lock_candidate_source(candidates.get(pk=candidate_id))
        candidate = (
            candidates.select_for_update(of=("self",))
            .get(pk=candidate_id)
        )
        if action not in ("approve", "reject"):
            raise serializers.ValidationError("Неизвестное действие.")
        if candidate.status in ("approved", "rejected"):
            if candidate.status != ("approved" if action == "approve" else "rejected"):
                raise Conflict("Предложение уже рассмотрено с другим решением.")
            if not system and candidate.reviewed_by_id != user.id and not user.is_superuser:
                access.require_review(user, candidate)
            return candidate
        if candidate.status != "pending":
            raise Conflict("Предложение заменено новой версией.")

        crm_match = None
        project = None
        if action == "approve" and candidate.fact_type == "project":
            crm_match = _selected_crm_match(candidate)
            project = _crm_project_for_match(candidate, crm_match) if crm_match else None
        if not system and _crm_approval_requires_finance(candidate, crm_match, project):
            access.require_team_role(user, candidate.team_id, ["finance"])
        elif not system:
            access.require_review(user, candidate)

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
        currency_was_explicit = "currency" in data
        schema = FactSchema(data=data)
        schema.is_valid(raise_exception=True)
        data = schema.validated_data
        if data["fact_type"] != candidate.fact_type:
            raise serializers.ValidationError("Тип факта изменить нельзя.")
        if action == "approve":
            if candidate.project_id:
                candidate_project = Project.objects.select_for_update(
                    of=("self",)
                ).get(pk=candidate.project_id)
                if project and project.pk != candidate_project.pk:
                    raise Conflict(
                        "Выбранная CRM-сделка конфликтует с проектом кандидата."
                    )
                if not project:
                    if crm_match:
                        raise Conflict(
                            "Выбранная CRM-сделка не подтверждает текущую локальную связь."
                        )
                    project = candidate_project
            data["currency"] = _review_currency(
                data,
                currency_was_explicit,
                project=project,
                crm_match=crm_match,
            )
            if project and (
                base_version != project.version
                or candidate.base_project_version != project.version
            ):
                raise Conflict()
            before = snapshot(project) if project else {}
            raw = candidate.trace.raw_message
            if raw:
                approved = FactCandidate.objects.filter(
                    status="approved",
                    trace__raw_message__source=raw.source,
                    trace__raw_message__session_name=raw.session_name,
                    trace__raw_message__message_id=raw.message_id,
                ).exclude(pk=candidate.pk)
                if candidate.fact_type == "commitment" and not data.get("commitment_id") and any(item.fact_type == "commitment" and same_commitment_origin(item.proposed_changes, data) for item in approved):
                    raise Conflict("Это обещание уже подтверждено. Проверяйте изменение существующего обязательства.")
                if any(
                    fact_identity(item.proposed_changes) == fact_identity(data)
                    for item in approved
                ) and not data.get("commitment_id"):
                    raise Conflict(
                        "Этот факт из исходного сообщения уже подтверждён в другой попытке."
                    )
            old_stage = project.status if project else ""
            if candidate.fact_type == "project":
                if project is None:
                    if not system and candidate.thread_revision_id and not crm_match and candidate.crm_match_state != "not_found":
                        raise serializers.ValidationError("Перед созданием проекта нужен успешный поиск CRM без совпадений.")
                    if crm_match:
                        if base_version != 0:
                            raise Conflict()
                        project = _materialize_crm_project(candidate, crm_match, data)
                    elif not data["object_name"] or (not system and "contract_amount" in data and data["contract_amount"] <= 0):
                        raise serializers.ValidationError(
                            "Нужны название и положительная сумма договора."
                        )
                    else:
                        # Team-scoped canonical identity, never fuzzy auto-merging.
                        normalized = normalize_deal_name(data["object_name"])
                        Team.objects.select_for_update(of=("self",)).get(
                            pk=candidate.team_id
                        )
                        if _project_identity_conflict(
                            candidate.team_id, data["object_name"], normalized
                        ):
                            raise Conflict(
                                "Объект уже существует. Сопоставьте предложение с ним."
                            )
                        company = None
                        if data.get("company_name") and (not system or data["party_role"] == "customer"):
                            companies = list(Company.objects.filter(name=data["company_name"]).filter(Q(team=candidate.team) | Q(team__isnull=True))[:2])
                            if len(companies) > 1:
                                raise Conflict("Название заказчика неоднозначно: требуется доказанная идентичность компании.")
                            company = companies[0] if companies else None
                            if company is None:
                                company = Company.objects.create(name=data["company_name"], team=candidate.team)
                        project = Project(
                            team=candidate.team,
                            manager=candidate.manager,
                            company=company,
                            name=data["object_name"],
                            normalized_name=normalized,
                            source="chat",
                        )
                if crm_match:
                    bitrix_deal_id = crm_match.bitrix_deal_id.strip()
                    if project.bitrix_id in (None, ""):
                        project.bitrix_id = bitrix_deal_id
                    elif project.bitrix_id != bitrix_deal_id:
                        raise Conflict(
                            "Проект уже связан с другой сделкой CRM."
                        )
                _validate_project_currency(project, data["currency"])
                if not system and any(key in data for key in ("cost_amount", "contract_amount")):
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
                if "contract_amount" in data:
                    project.contract_known = True
                project.whatsapp_fields = sorted(set(project.whatsapp_fields) | {field for field in ("contract_amount", "cost_amount", "current_action", "next_action", "stage") if field in data and data[field] != ""})
                if "cost_amount" in data:
                    project.cost_confirmed = True
                if "stage" in data:
                    project.status = data["stage"]
            elif not project and candidate.fact_type != "commitment":
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
                        direction=data["direction"],
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
                    direction=data["direction"],
                    amount_precision=data["amount_precision"],
                    is_verified=True,
                    reverses=reversal,
                    notes=reason or "Подтверждено по источнику; подробности в истории проверки.",
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
                    is_verified=True, status="received", direction="income", amount_precision="exact"
                ).aggregate(s=Sum("amount"))["s"] or Decimal(0)
            elif candidate.fact_type == "commitment":
                from .commitment_evidence import validate_commitment
                from .pipeline import _source_quote
                reviewed_deadline = data.get("deadline_at")
                reviewed_precision = data["deadline_precision"]
                from .message_context import source_scope
                if candidate.thread_revision_id or system:
                    evidence_scope = source_scope(raw) if system else access.messages_for(user)
                    raw = evidence_scope.filter(pk=data.get("promise_message_id"), pk__in=candidate.evidence.values_list("raw_message_id", flat=True)).select_related("config").first()
                if not raw or not validate_commitment(data, raw, candidate.trace.context_metadata.get("snapshot_max_id", raw.id), _source_quote, threaded=bool(candidate.thread_revision_id), autonomous=True if system else None):
                    raise serializers.ValidationError("Нужны конкретное действие и доказательства обязательства.")
                evidence_ids = set(candidate.evidence.values_list("raw_message_id", flat=True))
                if set((source_scope(raw) if system else access.messages_for(user)).filter(pk__in=evidence_ids).values_list("pk", flat=True)) != evidence_ids:
                    raise serializers.ValidationError("Для подтверждения необходим доступ ко всем доказательствам.")
                saved = set(candidate.evidence.values_list("raw_message_id", "quote"))
                if any((ref["raw_message_id"], ref["quote"]) not in saved for ref in data["evidence_messages"]):
                    raise serializers.ValidationError("Доказательства должны соответствовать сохранённой переписке.")
                if not system and changes and "deadline_at" in changes and reason.strip():
                    data["deadline_at"], data["deadline_precision"] = reviewed_deadline, reviewed_precision
                    data["deadline_basis"] = "explicit" if reviewed_deadline else "unknown"
                    data["uncertainties"] = [note for note in data["uncertainties"] if not note.startswith("Время 09:00 уточнено")]
                deadline = data.get("deadline_at")
                commitment_values = dict(
                    project=project,
                    team=candidate.team,
                    manager=candidate.manager,
                    source_message=raw,
                    candidate=candidate,
                    commitment_text=data["commitment_text"],
                    responsible_name=data["responsible_name"],
                    status=data["commitment_status"],
                    fulfilled_at=data.get("fulfilled_at") if data["commitment_status"] == "fulfilled" else None,
                    deadline_at=deadline,
                    original_deadline_at=deadline,
                    deadline=deadline.astimezone(source_zone(raw)).date() if deadline else None,
                    deadline_precision=data["deadline_precision"],
                    is_verified=True,
                )
                if data.get("commitment_id"):
                    if data["commitment_id"] != candidate.proposed_changes.get("commitment_id"):
                        raise serializers.ValidationError("Связь с обязательством нельзя изменить.")
                    existing = Commitment.objects.select_for_update().get(pk=data["commitment_id"], team=candidate.team)
                    if existing.version != candidate.proposed_changes.get("base_commitment_version"):
                        raise Conflict("Обязательство изменилось. Повторите проверку.")
                    if existing.source_message_id != raw.id:
                        raise serializers.ValidationError("Доказательство постановки не соответствует обязательству.")
                    if not system and data["commitment_status"] != "fulfilled":
                        raise serializers.ValidationError("Ручное предложение может только подтвердить выполнение.")
                    before_commitment = {"status": existing.status, "version": existing.version}
                    existing.status = data["commitment_status"]
                    existing.fulfilled_at = data.get("fulfilled_at") if existing.status == "fulfilled" else None
                    updated = ["status", "fulfilled_at", "version"]
                    if system and existing.status == "pending" and deadline:
                        existing.deadline_at = deadline
                        existing.deadline = deadline.astimezone(source_zone(raw)).date()
                        existing.deadline_precision = data["deadline_precision"]
                        existing.postponed_reason = reason
                        updated += ["deadline_at", "deadline", "deadline_precision", "postponed_reason"]
                    existing.version += 1
                    existing.save(update_fields=updated)
                    AuditEvent.objects.create(actor=user, target_type="Commitment", target_id=existing.id, action="approve_fulfillment", before_after={"before": before_commitment, "candidate_id": candidate.id, "status": existing.status})
                else:
                    Commitment.objects.create(**commitment_values)
            if project is not None:
                if project.pk:
                    project.paid_amount = project.financial_records.filter(
                        is_verified=True, status="received", direction="income", amount_precision="exact"
                    ).aggregate(s=Sum("amount"))["s"] or Decimal(0)
                if candidate.fact_type in ("project", "payment"):
                    project.identity_confirmed = True
                project.is_verified = (
                    project.is_verified
                    or (candidate.fact_type == "project" and "contract_amount" in data)
                    or candidate.fact_type == "payment"
                )
                project.version += 1
                project.needs_bitrix_sync = project.is_verified or (candidate.fact_type == "project" and project.identity_confirmed)
                try:
                    with transaction.atomic():
                        project.save()
                except IntegrityError as exc:
                    raise Conflict("Проект конфликтует с существующей записью.") from exc
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
                if not system and project.needs_bitrix_sync and BitrixSettings.objects.filter(is_active=True).exists():
                    OutboxEvent.objects.get_or_create(
                        deduplication_key=f"crm:{project.id}:{project.version}",
                        defaults={
                            "event_type": "crm_sync",
                            "payload": {
                                "project_id": project.id,
                                "version": project.version,
                            },
                        },
                    )
        if action == "approve":
            candidate.proposed_changes = json_value(data)
            if project and candidate.thread_revision_id:
                from .models import DialogueThread
                DialogueThread.objects.filter(pk=candidate.thread_revision.thread_id, team_id=candidate.team_id).update(project=project)
                FactCandidate.objects.filter(thread_revision__thread_id=candidate.thread_revision.thread_id, team_id=candidate.team_id, status="pending", project__isnull=True).exclude(pk=candidate.pk).update(project=project, base_project_version=project.version)
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
