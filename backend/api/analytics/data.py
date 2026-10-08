import json
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.db import connections, transaction
from django.db.models import Q
from django.utils import timezone
from jsonschema import Draft202012Validator

from .answers import column_label, collect_facts
from .. import access
from ..datamart import scoped_projects
from ..models import (
    AsyncOperation,
    Commitment,
    Project,
    Team,
    UserProfile,
)
from ..notifications import effective_deadline
from ..providers import ProviderUnavailable

ALIAS = "analytics_readonly"
MAX_SCAN = 20000
CURRENCIES = ["KZT", "USD", "EUR", "RUB"]
from .catalog import FIELDS, CATALOG, GRAINS, describe
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
        "date_axis": {"type": "string"},
        "grain": {"enum": list(GRAINS)},
        "aggregation": {"enum": ["default", "sum", "avg", "min", "max"]},
    },
}


def fail(code="invalid_tool_arguments", message=None):
    raise ProviderUnavailable(code, diagnostics={"validation_errors": [message]} if message else None)


RECORDS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "overdue_only": {"type": "boolean"},
        "group_by": {"enum": ["responsible", "project", "status", "deadline_day"]},
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
    intent: dict = field(default_factory=dict)
    trace: list = field(default_factory=list)

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
            "datasets": {k: describe(k) for k in CATALOG},
            "intent": self.intent,
            "defaults": self.defaults,
            "timezone": self.zone(),
            "today": timezone.now()
            .astimezone(ZoneInfo(self.zone()))
            .date()
            .isoformat(),
            "limits": {"rows": 1000, "statement_timeout_ms": 5000},
            "operators": ["eq", "in", "gt", "gte", "lt", "lte"],
            "identities": self.identities(),
            "notes": "AND filters; monthly targets only full months. Explicit question conditions replace defaults. Manager IDs are UserProfile IDs. dates are [start,end_exclusive). Last 90 days includes today + 89 preceding days. Grouping manager labels includes ID to avoid ambiguity.",
        }

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
                chosen = commitments if arguments.get('group_by') else commitments[: arguments.get("limit", 20)]
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
                if arguments.get('group_by'):
                    groups = defaultdict(int)
                    group = arguments['group_by']
                    for commitment, row in zip(chosen, rows):
                        if group == 'responsible':
                            key = (commitment.manager_id, row['responsible'])
                        elif group == 'project':
                            key = (commitment.project_id, row['project'])
                        elif group == 'deadline_day':
                            key = (row['deadline'][:10] if row['deadline'] else None,)
                        else:
                            key = (row['status'],)
                        groups[key] += 1
                    rows = [{group: key[-1], **({'group_id': key[0]} if len(key) == 2 else {}),
                             'commitment_count': count} for key, count in groups.items()]
                    rows.sort(key=lambda row: (-row['commitment_count'], str(row[group])))
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
        if arguments.get('group_by'):
            group = arguments['group_by']
            dataset['columns'] = [
                {'name': group, 'type': 'date' if group == 'deadline_day' else 'text',
                 'unit': None, 'label': 'День срока' if group == 'deadline_day' else column_label(group, 'commitments'),
                 'source': 'commitments', 'semantic_role': 'dimension'},
                {'name': 'commitment_count', 'type': 'count', 'unit': None,
                 'label': column_label('commitment_count', 'commitments'), 'source': 'commitments', 'semantic_role': 'measure'},
            ]
            if group in {'responsible', 'project'}:
                dataset['columns'].append({'name': 'group_id', 'type': 'id', 'unit': None,
                                           'label': 'ID группы', 'source': 'commitments', 'semantic_role': 'dimension'})
            dataset.update(returned_count=len(rows), total_groups=len(rows), truncated=False)
            dataset['coverage']['message'] = 'Все подтверждённые обязательства по выбранным условиям. Просрочки включены независимо от месяца исходного срока.'
        else:
            self.facts[f'{dataset["dataset_id"]}:records:total'] = {
                'dataset_id': dataset['dataset_id'], 'measure': 'commitment_count', 'formula': 'sum',
                'value': total, 'display': str(total), 'label': 'Найдено обязательств', 'type': 'count', 'unit': None,
                'scope': dataset['normalized_query'],
            }
        self.facts.update(collect_facts(dataset))
        self.registry[dataset["dataset_id"]] = dataset
        return dataset

    def query(self, arguments):
        from .sql import query
        return query(self, arguments)


def date_bucket(value, dimension):
    if dimension.endswith('_week'):
        value -= timedelta(days=value.weekday())
    elif dimension.endswith('_month') or dimension == 'month':
        value = value.replace(day=1)
    elif dimension.endswith('_quarter'):
        value = value.replace(month=((value.month - 1) // 3) * 3 + 1, day=1)
    elif dimension.endswith('_year'):
        value = value.replace(month=1, day=1)
    return value.isoformat()
