import json
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.db import connections, transaction
from django.db.models import Q, Exists, OuterRef
from django.utils import timezone
from jsonschema import Draft202012Validator

from .answers import column_label, collect_facts
from .. import access
from ..datamart import period_bounds, scoped_projects, confirmed_payments, project_stage
from ..models import (
    AsyncOperation,
    FinancialRecord,
    Commitment,
    Project,
    SalesTarget,
    Team,
    UserProfile,
    RawMessage,
    CrmProjectSnapshot,
)
from ..notifications import effective_deadline
from ..providers import ProviderUnavailable

ALIAS = "analytics_readonly"
MAX_SCAN = 20000
CURRENCIES = ["KZT", "USD", "EUR", "RUB"]
FIELDS = {
    "projects": {
        "project_id": ("id", "id"),
        "project_manager": ("manager__full_name", "text"),
        "team": ("team__name", "text"),
        "status": ("status", "text"),
        "project_type": ("project_type", "text"),
    },
    "payments": {
        "credited_manager": ("credited_profile__full_name", "text"),
        "team": ("project__team__name", "text"),
        "project_id": ("project_id", "id"),
        "status": ("project__status", "text"),
        "project_type": ("project__project_type", "text"),
        **{f"payment_{k}": ("payment_date", "date") for k in ["day", "week", "month", "quarter", "year"]},
    },
    "commitments": {
        "responsible_manager": ("manager__full_name", "text"),
        "project_id": ("project_id", "id"),
        "team": ("project__team__name", "text"),
        "project_status": ("project__status", "text"),
        "status": ("status", "text"),
        **{
            f"effective_deadline_{k}": ("effective_deadline", "date")
            for k in ["day", "week", "month", "quarter", "year"]
        },
    },
    "messages": {
        "team": ("team__name", "text"),
        "chat": ("chat_id", "text"),
        **{f"message_{k}": ("timestamp", "date") for k in ["day", "week", "month", "quarter", "year"]},
    },
    "crm_projects": {
        "project_id": ("project_id", "id"),
        "status": ("external_stage_name", "text"),
        "crm_manager": ("external_manager_name", "text"),
        "team": ("project__team__name", "text"),
    },
    "targets": {
        "manager": ("profile__full_name", "text"),
        "team": ("team__name", "text"),
        "month": ("month", "date"),
    },
}
for source in ['projects', 'payments', 'commitments', 'messages', 'crm_projects', 'targets']:
    FIELDS[source]['team_id'] = ('project__team_id' if source in ['payments', 'crm_projects'] else 'team_id', 'id')
for source, path in [('projects', 'manager_id'), ('payments', 'credited_profile_id'), ('commitments', 'manager_id'), ('targets', 'profile_id')]:
    FIELDS[source]['profile_id'] = (path, 'id')
MEASURES = {
    "projects": {
        "project_count": "count",
        "contract_amount": "money",
        "confirmed_cost": "money",
        "contract_margin_percent": "percent",
    },
    "payments": {"payment_count": "count", "received_amount": "money"},
    "commitments": {"commitment_count": "count", "overdue_count": "count"},
    "targets": {"target_amount": "money"},
    "messages": {"message_count": "count"},
    "crm_projects": {"project_count": "count", "crm_amount": "money"},
}
QUERY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["dataset", "dimensions", "measures"],
    "properties": {
        "dataset": {"enum": list(FIELDS)},
        "dimensions": {
            "type": "array",
            "items": {"type": "string"},
            "uniqueItems": True,
            "maxItems": 3,
        },
        "measures": {
            "type": "array",
            "items": {"type": "string"},
            "uniqueItems": True,
            "minItems": 1,
            "maxItems": 4,
        },
        "filters": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "op", "value"],
                "properties": {
                    "field": {"type": "string"},
                    "op": {"enum": ["eq", "in", "gt", "gte", "lt", "lte"]},
                    "value": {
                        "oneOf": [
                            {"type": "string", "maxLength": 128},
                            {"type": "integer", "minimum": 1},
                            {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 100,
                                "items": {
                                    "oneOf": [
                                        {"type": "string", "maxLength": 128},
                                        {"type": "integer", "minimum": 1},
                                    ]
                                },
                            },
                        ]
                    },
                },
            },
        },
        "date_range": {
            "type": "object",
            "additionalProperties": False,
            "required": ["start", "end_exclusive"],
            "properties": {
                "start": {"type": "string", "format": "date"},
                "end_exclusive": {"type": "string", "format": "date"},
            },
        },
        "currency": {"enum": CURRENCIES},
        "order_by": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "direction"],
                "properties": {
                    "field": {"type": "string"},
                    "direction": {"enum": ["asc", "desc"]},
                },
            },
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        "top_n": {"type": "boolean"},
    },
}


def fail(code="invalid_tool_arguments"):
    raise ProviderUnavailable(code)


RECORDS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "overdue_only": {"type": "boolean"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    },
}


def validate(schema, value):
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        raise ProviderUnavailable(
            "invalid_tool_arguments",
            diagnostics={
                "validation_errors": [
                    "/".join(str(part) for part in error.path)
                    + ": "
                    + error.message[:1200]
                    for error in errors[:5]
                ]
            },
        )


@dataclass
class OperationContext:
    operation_id: int
    requested_by_id: int
    fingerprint: str
    expires_at: object
    deadline: float
    defaults: dict
    registry: dict = field(default_factory=dict)
    queries: int = 0
    sources: dict = field(default_factory=dict)
    facts: dict = field(default_factory=dict)

    def check(self):
        if time.monotonic() >= self.deadline:
            fail("analytics_timeout")
        op = AsyncOperation.objects.get(pk=self.operation_id)
        user = User.objects.get(pk=self.requested_by_id)
        if op.status != "running":
            fail("operation_cancelled")
        if (
            op.expires_at <= timezone.now()
            or not user.is_active
            or access.is_client(user)
            or access.fingerprint(user) != self.fingerprint
        ):
            fail("access_or_lifetime_changed")
        return user

    def schema(self):
        self.check()
        return {
            "enums": {
                "project_type": dict(Project.PROJECT_TYPE_CHOICES),
                "project_status": dict(Project.STATUS_CHOICES),
                "commitment_status": dict(Commitment.STATUS_CHOICES),
            },
            "datasets": {
                k: {
                    "dimensions": list(FIELDS[k]),
                    "measures": MEASURES[k],
                    "filters": list(filter_fields(k)),
                    "definition": definition(k),
                }
                for k in FIELDS
            },
            "defaults": self.defaults,
            "timezone": self.zone(),
            "today": timezone.now()
            .astimezone(ZoneInfo(self.zone()))
            .date()
            .isoformat(),
            "limits": {"rows": 1000, "scan_rows": MAX_SCAN},
            "operators": ["eq", "in", "gt", "gte", "lt", "lte"],
            "identities": self.identities(),
            "notes": "AND filters; monthly targets only full months. Explicit question conditions replace defaults. Manager IDs are UserProfile IDs. dates are [start,end_exclusive). Last 90 days includes today + 89 preceding days. Grouping manager labels includes ID to avoid ambiguity.",
        }

    def evidence(self, user, dataset, rows):
        project_ids = [
            r.id if dataset == "projects" else getattr(r, "project_id", None)
            for r in rows
        ]
        return list(
            access.messages_for(user)
            .using(ALIAS)
            .filter(
                Q(id__in=[r.id for r in rows])
                if dataset == "messages"
                else Q(project_id__in=project_ids)
            )
            .order_by("-timestamp")
            .values("id", "sender_name")[:20]
        )

    def identities(self):
        user = self.check()
        if ALIAS not in connections:
            fail("analytics_not_configured")
        return {
            "managers": list(
                access.profiles_for(user).using(ALIAS).values("id", "full_name")[:200]
            ),
            "teams": list(
                Team.objects.using(ALIAS)
                .filter(id__in=access.team_ids(user))
                .values("id", "name")[:200]
            ),
            "projects": list(
                scoped_projects(user, self.defaults, using=ALIAS)[0].values(
                    "id", "name"
                )[:200]
            ),
            "limit": 200,
            "note": "Identity preview only; omitted names require clarification, not guessed IDs.",
        }

    def zone(self):
        profile = UserProfile.objects.filter(user_id=self.requested_by_id).first()
        return profile.timezone if profile else "Asia/Almaty"

    def records(self, arguments):
        """Current open obligations; old overdue deadlines remain actionable."""
        user = self.check()
        validate(RECORDS_SCHEMA, arguments)
        self.queries += 1
        if self.queries > 8:
            fail("query_limit_exceeded")
        if ALIAS not in connections:
            fail("analytics_not_configured")
        conn = connections[ALIAS]
        try:
            with transaction.atomic(using=ALIAS):
                if conn.vendor != "postgresql":
                    fail("analytics_not_configured")
                with conn.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        [
                            str(
                                max(
                                    1,
                                    min(
                                        5000,
                                        int((self.deadline - time.monotonic()) * 1000),
                                    ),
                                )
                            )
                        ],
                    )
                qs = access.commitments_for(user).using(ALIAS).filter(is_verified=True)
                for key in ["team_id", "manager_id", "project_id"]:
                    if self.defaults.get(key):
                        qs = qs.filter(**{key: self.defaults[key]})
                if arguments.get("overdue_only", True):
                    qs = qs.filter(status__in=["pending", "overdue"])
                commitments = list(
                    qs.select_related("project", "manager").order_by(
                        "-promised_at", "-id"
                    )[: MAX_SCAN + 1]
                )
                if len(commitments) > MAX_SCAN:
                    fail("dataset_limit_use_filters")
                if arguments.get("overdue_only", True):
                    commitments = [
                        c
                        for c in commitments
                        if effective_deadline(c)
                        and effective_deadline(c) < timezone.now()
                    ]
                total = len(commitments)
                chosen = commitments[: arguments.get("limit", 20)]
                rows = [
                    {
                        "commitment_id": c.id,
                        "text": c.commitment_text[:2000],
                        "project_id": c.project_id,
                        "project": c.project.name if c.project else "Без проекта",
                        "responsible": c.manager.full_name
                        if c.manager
                        else c.responsible_name or "Не назначен",
                        "deadline": effective_deadline(c)
                        .astimezone(ZoneInfo(self.zone()))
                        .isoformat()
                        if effective_deadline(c)
                        else None,
                        "status": c.get_status_display(),
                    }
                    for c in chosen
                ]
                evidence = list(
                    access.messages_for(user)
                    .using(ALIAS)
                    .filter(
                        id__in=[
                            c.source_message_id for c in chosen if c.source_message_id
                        ]
                    )
                    .values("id", "sender_name")[:20]
                )
        except ProviderUnavailable:
            raise
        except Exception:
            fail("analytics_query_failed")
        self.check()
        dataset = {
            "dataset_id": str(uuid.uuid4()),
            "columns": [
                {"name": name, "type": kind, "unit": None, "label": column_label(name, "commitments"), "source": "commitments", "semantic_role": "dimension"}
                for name, kind in [
                    ("commitment_id", "id"),
                    ("text", "text"),
                    ("project_id", "id"),
                    ("project", "text"),
                    ("responsible", "text"),
                    ("deadline", "date"),
                    ("status", "text"),
                ]
            ],
            "rows": rows,
            "normalized_query": {
                "dataset": "commitment_records",
                "filters": [
                    {"field": key, "op": "eq", "value": value}
                    for key, value in self.defaults.items()
                    if key in ["team_id", "manager_id", "project_id"]
                ],
                **arguments,
            },
            "timezone": self.zone(),
            "coverage": {
                "status": "partial",
                "message": "Последние зарегистрированные обязательства. Просрочки включены независимо от месяца исходного срока.",
            },
            "returned_count": len(rows),
            "total_groups": total,
            "truncated": total > len(rows),
            "definition": "Последние подтверждённые обязательства по дате регистрации; только доступные источники.",
            "evidence": evidence,
        }
        self.registry[dataset["dataset_id"]] = dataset
        return dataset

    def query(self, arguments):
        user = self.check()
        validate(QUERY_SCHEMA, arguments)
        if type(arguments.get("limit", 1000)) is not int:
            fail()
        self.queries += 1
        if self.queries > 8:
            fail("query_limit_exceeded")
        dataset = arguments["dataset"]
        dims, measures = arguments["dimensions"], arguments["measures"]
        if (
            not set(dims) <= FIELDS[dataset].keys()
            or not set(measures) <= MEASURES[dataset].keys()
        ):
            fail("unsupported_query")
        filters = list(arguments.get("filters", []))
        for key in ["team_id", "manager_id", "project_id"]:
            if (
                key in filter_fields(dataset)
                and self.defaults.get(key)
                and not any(f["field"] == key for f in filters)
            ):
                filters.append({"field": key, "op": "eq", "value": self.defaults[key]})
        currency = arguments.get("currency", self.defaults.get("currency", "KZT"))
        zone = self.zone()
        start, end, today = period_bounds(
            self.defaults.get("period", "this_month"), zone
        )
        if arguments.get("date_range"):
            try:
                start = date.fromisoformat(arguments["date_range"]["start"])
                end = date.fromisoformat(arguments["date_range"]["end_exclusive"])
            except ValueError:
                fail()
        if start >= end or (end - start).days > 3660:
            fail("unsupported_query")
        if dataset in ["projects", "crm_projects"] and arguments.get("date_range"):
            fail("unsupported_query")
        if dataset == "targets" and (
            start.day != 1
            or end.day != 1
            or any(f["field"] not in ["manager_id", "team_id"] for f in filters)
        ):
            fail("unsupported_query")
        if ALIAS not in connections:
            fail("analytics_not_configured")
        connection = connections[ALIAS]
        # No writable fallback, including connection/configuration errors.
        try:
            with transaction.atomic(using=ALIAS):
                if connection.vendor != "postgresql":
                    fail("analytics_not_configured")
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        [
                            str(
                                max(
                                    1,
                                    min(
                                        5000,
                                        int((self.deadline - time.monotonic()) * 1000),
                                    ),
                                )
                            )
                        ],
                    )
                qs = queryset(dataset, user, currency)
                for f in filters:
                    path, kind = filter_fields(dataset).get(f["field"], (None, None))
                    if path is None:
                        fail("unsupported_query")
                    vals = f["value"] if f["op"] == "in" else [f["value"]]
                    if (
                        f["op"] == "in"
                        and not isinstance(f["value"], list)
                        or f["op"] != "in"
                        and isinstance(f["value"], list)
                    ):
                        fail()
                    if kind in ["id", "enum"] and f["op"] not in ["eq", "in"]:
                        fail()
                    for val in vals:
                        if kind == "id" and (type(val) is not int or val <= 0):
                            fail()
                        if kind == "enum" and (
                            not isinstance(val, str)
                            or val not in enum_values(f["field"], dataset)
                        ):
                            fail()
                        if kind == "money":
                            try:
                                if (
                                    not isinstance(val, str)
                                    or not re.fullmatch(
                                        r"-?\d{1,16}(?:\.\d{1,2})?", val
                                    )
                                    or not Decimal(val).is_finite()
                                ):
                                    fail()
                            except InvalidOperation:
                                fail()
                    lookup = {"eq": "exact", "in": "in"}.get(f["op"], f["op"])
                    # A model default is not evidence of a local project stage.
                    if (
                        dataset in ["projects", "payments"] and f["field"] == "status"
                    ) or (dataset == "commitments" and f["field"] == "project_status"):
                        prefix = "" if dataset == "projects" else "project__"
                        qs = qs.filter(
                            **{prefix + "whatsapp_fields__contains": ["stage"]}
                        )
                    qs = qs.filter(**{f"{path}__{lookup}": f["value"]})
                unknown_dates = 0
                if dataset == "payments":
                    unknown_dates = qs.filter(payment_date__isnull=True).count()
                    qs = qs.filter(
                        payment_date__gte=start,
                        payment_date__lt=min(end, today + timedelta(days=1)),
                    )
                elif dataset == "targets":
                    qs = qs.filter(month__gte=start, month__lt=end)
                if dataset == "messages":
                    unknown_dates = qs.filter(sent_at_known=False).count()
                    qs = qs.filter(
                        sent_at_known=True,
                        timestamp__gte=datetime.combine(
                            start, datetime.min.time(), tzinfo=ZoneInfo(zone)
                        ),
                        timestamp__lt=datetime.combine(
                            min(end, today + timedelta(days=1)),
                            datetime.min.time(),
                            tzinfo=ZoneInfo(zone),
                        ),
                    )
                source_rows = list(qs.order_by("id")[: MAX_SCAN + 1])
                if len(source_rows) > MAX_SCAN:
                    fail("dataset_limit_use_filters")
                result = aggregate(
                    dataset,
                    source_rows,
                    dims,
                    measures,
                    start,
                    end,
                    zone,
                    self.deadline,
                )
                coverage_teams = Team.objects.using(ALIAS).filter(id__in=access.team_ids(user))
                for selected_filter in filters:
                    if selected_filter['field'] == 'team_id':
                        if selected_filter['op'] == 'eq':
                            coverage_teams = coverage_teams.filter(id=selected_filter['value'])
                        elif selected_filter['op'] == 'in':
                            coverage_teams = coverage_teams.filter(id__in=selected_filter['value'])
                teams = list(
                    coverage_teams
                    .values_list("history_complete_from", flat=True)
                )
                complete = (
                    bool(teams)
                    and all(t is not None and t <= start for t in teams)
                    and not unknown_dates
                )
        except ProviderUnavailable:
            raise
        except Exception:
            fail("analytics_query_failed")
        if dataset == "projects" and any(
            m in measures for m in ["confirmed_cost", "contract_margin_percent"]
        ):
            unknown_costs = sum(not row.cost_confirmed for row in source_rows)
        else:
            unknown_costs = 0
        self.check()
        if complete and dataset in ['payments', 'messages'] and len(dims) == 1 and FIELDS[dataset][dims[0]][1] == 'date':
            dimension = dims[0]
            current = start
            actual_end = min(end, today + timedelta(days=1))
            existing = {r[dimension] for r in result}
            while current < actual_end:
                bucket = date_bucket(current, dimension)
                if bucket not in existing:
                    result.append({dimension: bucket, **{m: '0.00' if MEASURES[dataset][m] == 'money' else 0 for m in measures}})
                    existing.add(bucket)
                if len(existing) > 1000:
                    fail('dataset_limit_use_filters')
                current += timedelta(days=1)
        orders = arguments.get("order_by") or [{"field": d, "direction": "asc"} for d in dims if FIELDS[dataset][d][1] == "date"]
        if not orders and dims:
            orders = [{"field": measures[0], "direction": "desc"}]
        if any(o["field"] not in dims + measures for o in orders):
            fail()
        result.sort(key=lambda r: json.dumps(r, sort_keys=True))
        for order in reversed(orders):
            name = order["field"]
            result.sort(
                key=lambda r: (
                    r[name] is None,
                    Decimal(str(r[name]))
                    if MEASURES[dataset].get(name) in ["money", "count", "percent"]
                    and r[name] is not None
                    else r[name]
                    if r[name] is not None
                    else "",
                ),
                reverse=order["direction"] == "desc",
            )
        result = [r for r in result if all(r[o["field"]] is not None for o in orders)] + [r for r in result if any(r[o["field"]] is None for o in orders)]
        total = len(result)
        full_result = list(result)
        limit = arguments.get("limit", 1000)
        if total > limit and not arguments.get("top_n", False):
            fail("dataset_limit_use_filters")
        result = result[:limit]
        columns = [
            {"name": d, "type": FIELDS[dataset][d][1], "unit": None} for d in dims
        ] + [
            {
                "name": m,
                "type": MEASURES[dataset][m],
                "unit": currency
                if MEASURES[dataset][m] == "money"
                else "%"
                if MEASURES[dataset][m] == "percent"
                else None,
            }
            for m in measures
        ]
        output = {
            "dataset_id": str(uuid.uuid4()),
            "columns": [{**c, "label": column_label(c["name"], dataset), "source": dataset, "semantic_role": "measure" if c["type"] in ["money", "count", "percent"] else "dimension"} for c in columns],
            "rows": result,
            "normalized_query": {
                **arguments,
                "filters": filters,
                "order_by": orders,
                "currency": None if dataset == "messages" else currency,
                "date_range": {
                    "start": start.isoformat(),
                    "end_exclusive": end.isoformat(),
                }
                if dataset not in ["projects", "crm_projects"]
                else None,
            },
            "timezone": zone,
            "effective_end_exclusive": min(end, today + timedelta(days=1)).isoformat()
            if dataset in ["payments", "messages"]
            else None,
            "coverage": {
                "status": "complete" if complete and not unknown_costs and dataset not in ['crm_projects', 'projects', 'targets'] and end <= today + timedelta(days=1) else "partial",
                "message": "Текущий срез CRM; история переходов между стадиями отсутствует."
                if dataset == 'crm_projects'
                else "Только утверждённые месячные планы; отсутствующий план не считается нулём."
                if dataset == 'targets'
                else f"У {unknown_dates} {'платежей' if dataset == 'payments' else 'сообщений'} неизвестна исходная дата; они не включены. Полнота истории не подтверждена."
                if unknown_dates
                else f"Стоимость не подтверждена для {unknown_costs} проектов; маржа только по известным данным"
                if unknown_costs
                else "Текущий срез доступных проектов."
                if dataset == 'projects'
                else f"Данные по состоянию на {today.isoformat()}; период ещё не завершён."
                if end > today + timedelta(days=1)
                else "Полная история"
                if complete
                else "Полнота истории не подтверждена",
            },
            "returned_count": len(result),
            "total_groups": total,
            "truncated": total > limit,
            "definition": definition(dataset),
            "evidence": self.evidence(user, dataset, source_rows),
        }
        if len(json.dumps(output, ensure_ascii=False).encode()) > 256 * 1024:
            fail("dataset_limit_use_filters")
        self.facts.update(collect_facts(output, full_result))
        self.registry[output["dataset_id"]] = output
        return output


def definition(dataset):
    return {
        "payments": "Подтверждённые поступления, включая отрицательные корректировки. Менеджер — сотрудник, на которого записан платёж.",
        "messages": "Доступные оригиналы WhatsApp по исходной дате; дубли, старые версии и неизвестные даты исключены. Валюта не применяется.",
        "crm_projects": "Текущие снимки CRM: стадия, ответственный и сумма сделки, не фактические поступления и не подтверждённый договор.",
        "projects": "Доступные неархивные проекты с подтверждённой идентичностью или платежом; договорная маржа по подтверждённой стоимости, не бухгалтерская прибыль.",
        "commitments": "Подтверждённые обязательства; просрочка по effective deadline, неизвестный срок не считается известным.",
        "targets": "Активные утверждённые месячные планы, без пропорционального распределения.",
    }[dataset]


def enum_values(name, dataset):
    from ..models import Commitment

    if name == "project_type":
        return dict(Project.PROJECT_TYPE_CHOICES)
    return dict(
        Commitment.STATUS_CHOICES
        if dataset == "commitments" and name == "status"
        else Project.STATUS_CHOICES
    )


def filter_fields(dataset):
    if dataset == "messages":
        return {
            "team_id": ("team_id", "id"),
            "project_id": ("project_id", "id"),
            "chat": ("chat_id", "text"),
        }
    if dataset == "crm_projects":
        return {
            "team_id": ("project__team_id", "id"),
            "project_id": ("project_id", "id"),
            "manager_id": ("project__manager_id", "id"),
            "status": ("external_stage_id", "text"),
            "stage_name": ("external_stage_name", "text"),
            "crm_manager_id": ("external_manager_id", "text"),
            "crm_manager_name": ("external_manager_name", "text"),
        }
    prefix = "project__" if dataset in ["payments", "commitments"] else ""
    fields = {
        "team_id": (
            "team_id" if dataset == "commitments" else prefix + "team_id",
            "id",
        ),
        "project_id": ("id" if dataset == "projects" else "project_id", "id"),
        "manager_id": (
            {
                "projects": "manager_id",
                "payments": "credited_profile_id",
                "commitments": "manager_id",
                "targets": "profile_id",
            }[dataset],
            "id",
        ),
    }
    if dataset == "targets":
        return {k: v for k, v in fields.items() if k != "project_id"}
    fields.update(
        {
            "status": (
                prefix + "status" if dataset != "commitments" else "status",
                "enum",
            ),
            "project_type": (prefix + "project_type", "enum"),
            "contract_amount": (prefix + "contract_amount", "money"),
        }
    )
    if dataset == "payments":
        fields["amount"] = ("amount", "money")
    if dataset == "commitments":
        fields["project_status"] = ("project__status", "enum")
    return fields


def queryset(dataset, user, currency):
    projects = scoped_projects(user, {"currency": currency}, using=ALIAS)[0]
    if dataset == "messages":
        messages = (
            access.messages_for(user)
            .using(ALIAS)
            .filter(source__in=["waha", "whatsapp_export"])
            .exclude(
                processing_state__in=[
                    "deleted",
                    "superseded",
                    "export_staged",
                    "deduplication_ambiguous",
                ]
            )
        )
        newer = RawMessage.objects.using(ALIAS).filter(
            config_id=OuterRef("config_id"),
            source=OuterRef("source"),
            session_name=OuterRef("session_name"),
            message_id=OuterRef("message_id"),
            id__gt=OuterRef("id"),
        )
        return (
            messages.annotate(newer_version=Exists(newer))
            .filter(newer_version=False)
            .only(
                "id",
                "config_id",
                "team_id",
                "project_id",
                "chat_id",
                "timestamp",
                "sent_at_known",
            )
            .select_related("team")
        )
    if dataset == "crm_projects":
        return (
            CrmProjectSnapshot.objects.using(ALIAS)
            .filter(project__in=projects, currency=currency)
            .select_related("project__team")
        )
    if dataset == "projects":
        return projects.select_related("manager", "team", "crm_snapshot")
    if dataset == "payments":
        return confirmed_payments(
            user, {"currency": currency}, using=ALIAS
        ).select_related("project__team", "credited_profile")
    if dataset == "commitments":
        return (
            access.commitments_for(user)
            .using(ALIAS)
            .filter(is_verified=True)
            .filter(Q(project__isnull=True) | Q(project__in=projects))
            .select_related("project__team", "manager")
        )
    return (
        SalesTarget.objects.using(ALIAS)
        .filter(
            team_id__in=access.team_ids(user),
            profile__in=access.profiles_for(user),
            is_active=True,
            currency=currency,
        )
        .select_related("profile", "team")
    )


def attribute(obj, path):
    value = obj
    for part in path.split("__"):
        value = getattr(value, part, None) if value is not None else None
    return value


def aggregate(dataset, objects, dims, measures, start, end, zone, deadline):
    buckets = defaultdict(
        lambda: {
            "rows": 0,
            "amount": Decimal(0),
            "contract": Decimal(0),
            "cost": Decimal(0),
            "margin_contract": Decimal(0),
            "margin_cost": Decimal(0),
            "known_cost": 0,
            "known_contract": 0,
            "overdue": 0,
        }
    )
    now = timezone.now()
    for obj in objects:
        if time.monotonic() >= deadline:
            fail("analytics_timeout")
        due = effective_deadline(obj) if dataset == "commitments" else None
        if dataset == "commitments" and (
            due is None or not start <= due.astimezone(ZoneInfo(zone)).date() < end
        ):
            continue
        key = []
        for d in dims:
            path, kind = FIELDS[dataset][d]
            v = (
                due.astimezone(ZoneInfo(zone)).date()
                if path == "effective_deadline" and due
                else project_stage(obj)[1]
                if dataset == "projects" and d == "status"
                else attribute(obj, path)
            )
            if isinstance(v, datetime):
                v = v.astimezone(ZoneInfo(zone)).date()
            if kind == "date" and v is not None:
                if d.endswith("_week"):
                    v -= timedelta(days=v.weekday())
                elif d.endswith("_year"):
                    v = v.replace(month=1, day=1)
                elif d.endswith("_quarter"):
                    v = v.replace(month=((v.month - 1) // 3) * 3 + 1, day=1)
                elif d.endswith("_month"):
                    v = v.replace(day=1)
                v = v.isoformat()
            if (
                d
                in [
                    "project_manager",
                    "credited_manager",
                    "responsible_manager",
                    "manager",
                ]
                and v
            ):
                id_path = {
                    "project_manager": "manager_id",
                    "credited_manager": "credited_profile_id",
                    "responsible_manager": "manager_id",
                    "manager": "profile_id",
                }[d]
                v = f"{v} (#{attribute(obj, id_path)})"
            key.append(v)
        b = buckets[tuple(key)]
        b["rows"] += 1
        if dataset in ["payments", "targets"]:
            b["amount"] += obj.amount
        if dataset == "crm_projects" and obj.opportunity is not None:
            b["amount"] += obj.opportunity
            b["known_contract"] += 1
        if dataset == "projects":
            b["contract"] += obj.contract_amount
            b["known_contract"] += int(
                getattr(obj, "contract_known", False) or obj.contract_amount > 0
            )
            if obj.cost_confirmed:
                b["known_cost"] += 1
                b["cost"] += obj.cost_amount
                if obj.contract_amount > 0:
                    b["margin_contract"] += obj.contract_amount
                    b["margin_cost"] += obj.cost_amount
        if dataset == "commitments":
            b["overdue"] += int(
                obj.status in ["pending", "overdue"] and due is not None and due < now
            )
    if not dims and not buckets:
        buckets[
            ()
        ]  # Empty aggregate has a real zero, with partial coverage separately.
    result = []
    for key, b in buckets.items():
        row = dict(zip(dims, key))
        for m in measures:
            if m.endswith("_count"):
                value = b["overdue"] if m == "overdue_count" else b["rows"]
            elif m == "contract_margin_percent":
                value = (
                    float(
                        (
                            (b["margin_contract"] - b["margin_cost"])
                            / b["margin_contract"]
                            * 100
                        ).quantize(Decimal(".01"))
                    )
                    if b["margin_contract"]
                    else None
                )
            elif (
                m in ["contract_amount", "crm_amount"]
                and b["known_contract"] != b["rows"]
            ):
                value = None
            elif m == "confirmed_cost":
                value = (
                    str(b["cost"].quantize(Decimal(".01")))
                    if b["known_cost"] == b["rows"] and b["rows"]
                    else None
                )
            else:
                value = str(
                    (b["contract"] if m == "contract_amount" else b["amount"]).quantize(
                        Decimal(".01")
                    )
                )
            row[m] = value
        result.append(row)
    return result


def date_bucket(value, dimension):
    if dimension.endswith('_week'):
        value -= timedelta(days=value.weekday())
    elif dimension.endswith('_month'):
        value = value.replace(day=1)
    elif dimension.endswith('_quarter'):
        value = value.replace(month=((value.month - 1) // 3) * 3 + 1, day=1)
    elif dimension.endswith('_year'):
        value = value.replace(month=1, day=1)
    return value.isoformat()
