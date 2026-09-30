import json
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.db import connections, transaction
from django.db.models import Q
from django.utils import timezone
from jsonschema import Draft202012Validator

from .. import access
from ..datamart import period_bounds
from ..models import (
    AsyncOperation,
    FinancialRecord,
    Commitment,
    Project,
    SalesTarget,
    Team,
    UserProfile,
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
        **{f"payment_{k}": ("payment_date", "date") for k in ["day", "week", "month"]},
    },
    "commitments": {
        "responsible_manager": ("manager__full_name", "text"),
        "project_id": ("project_id", "id"),
        "team": ("project__team__name", "text"),
        "project_status": ("project__status", "text"),
        "status": ("status", "text"),
        **{
            f"effective_deadline_{k}": ("effective_deadline", "date")
            for k in ["day", "week", "month"]
        },
    },
    "targets": {
        "manager": ("profile__full_name", "text"),
        "team": ("team__name", "text"),
        "month": ("month", "date"),
    },
}
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


def validate(schema, value):
    if list(Draft202012Validator(schema).iter_errors(value)):
        fail()


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
            .filter(project_id__in=project_ids)
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
                access.projects_for(user)
                .using(ALIAS)
                .filter(is_verified=True, version__gt=0)
                .values("id", "name")[:200]
            ),
            "limit": 200,
            "note": "Identity preview only; omitted names require clarification, not guessed IDs.",
        }

    def zone(self):
        profile = UserProfile.objects.filter(user_id=self.requested_by_id).first()
        return profile.timezone if profile else "Asia/Almaty"

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
            if self.defaults.get(key) and not any(f["field"] == key for f in filters):
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
        if dataset == "projects" and arguments.get("date_range"):
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
                    qs = qs.filter(**{f"{path}__{lookup}": f["value"]})
                if dataset == "payments":
                    qs = qs.filter(
                        payment_date__gte=start,
                        payment_date__lt=min(end, today + timedelta(days=1)),
                    )
                elif dataset == "targets":
                    qs = qs.filter(month__gte=start, month__lt=end)
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
                teams = list(
                    Team.objects.using(ALIAS)
                    .filter(id__in=access.team_ids(user))
                    .values_list("history_complete_from", flat=True)
                )
                complete = bool(teams) and all(
                    t is not None and t <= start for t in teams
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
        orders = arguments.get("order_by", [])
        if any(o["field"] not in dims + measures for o in orders):
            fail()
        result.sort(key=lambda r: json.dumps(r, sort_keys=True))
        for order in reversed(orders):
            name = order["field"]
            result.sort(
                key=lambda r: (
                    r[name] is not None,
                    Decimal(str(r[name]))
                    if MEASURES[dataset].get(name) in ["money", "count", "percent"]
                    and r[name] is not None
                    else r[name]
                    if r[name] is not None
                    else "",
                ),
                reverse=order["direction"] == "desc",
            )
        total = len(result)
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
            "columns": columns,
            "rows": result,
            "normalized_query": {
                **arguments,
                "filters": filters,
                "currency": currency,
                "date_range": {
                    "start": start.isoformat(),
                    "end_exclusive": end.isoformat(),
                }
                if dataset != "projects"
                else None,
            },
            "timezone": zone,
            "effective_end_exclusive": min(end, today + timedelta(days=1)).isoformat()
            if dataset == "payments"
            else None,
            "coverage": {
                "status": "complete" if complete and not unknown_costs else "partial",
                "message": f"Стоимость не подтверждена для {unknown_costs} проектов; маржа только по известным данным"
                if unknown_costs
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
        self.registry[output["dataset_id"]] = output
        return output


def definition(dataset):
    return {
        "payments": "Подтверждённые received-поступления, включая отрицательные корректировки; менеджер — credited_profile, не текущий владелец проекта.",
        "projects": "Подтверждённые неархивные проекты; договорная маржа по подтверждённой стоимости, не бухгалтерская прибыль.",
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
    prefix = "project__" if dataset in ["payments", "commitments"] else ""
    fields = {
        "team_id": (prefix + "team_id", "id"),
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
    projects = (
        access.projects_for(user)
        .using(ALIAS)
        .filter(is_verified=True, version__gt=0, currency=currency)
    )
    if dataset == "projects":
        return projects.select_related("manager", "team")
    if dataset == "payments":
        return (
            FinancialRecord.objects.using(ALIAS)
            .filter(
                project__in=projects,
                currency=currency,
                status="received",
                is_verified=True,
            )
            .select_related("project__team", "credited_profile")
        )
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
                else attribute(obj, path)
            )
            if kind == "date" and v is not None:
                if d.endswith("_week"):
                    v -= timedelta(days=v.weekday())
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
        if dataset == "projects":
            b["contract"] += obj.contract_amount
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
