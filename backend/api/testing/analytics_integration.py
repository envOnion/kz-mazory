"""Run only against an isolated, migrated PostgreSQL test database."""

import json
import time
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connections, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from api import access
from api.analytics.data import OperationContext
from api.models import (
    AsyncOperation,
    Team,
    TeamMembership,
    UserProfile,
    Project,
    FinancialRecord,
    SalesTarget,
    Commitment,
    CrmProjectSnapshot,
    WhatsAppConfig,
    RawMessage,
)
from api.providers import ProviderUnavailable
from api.tasks import dispatch_outbox


def verify():
    assert (
        settings.MAZORY_ENV == "test" and connections["default"].vendor == "postgresql"
    ), "Isolated test PostgreSQL required"
    suffix = uuid.uuid4().hex[:10]
    user = User.objects.create_user(
        "analytics-review-" + suffix, password="isolated-fixture-password"
    )
    other = User.objects.create_user("analytics-hidden-" + suffix)
    a = UserProfile.objects.create(user=user, full_name="Attributed")
    b = UserProfile.objects.create(user=other, full_name="Current owner")
    team = Team.objects.create(
        name="Review-" + suffix,
        history_complete_from=timezone.now().date().replace(day=1),
    )
    hidden = Team.objects.create(name="Hidden-" + suffix)
    TeamMembership.objects.create(
        user=user, team=team, role="team_lead", status="active"
    )
    TeamMembership.objects.create(
        user=other, team=team, role="manager", status="active"
    )
    TeamMembership.objects.create(
        user=other, team=hidden, role="manager", status="active"
    )
    p = Project.objects.create(
        name="<img src=x onerror=alert(1)>",
        team=team,
        manager=b,
        version=1,
        is_verified=True,
        contract_amount="10000000.00",
        cost_amount="5000000.00",
        cost_confirmed=True,
        project_type="state",
        status="completed",
    )
    Project.objects.create(
        name="Second",
        team=team,
        manager=b,
        version=1,
        is_verified=True,
        contract_amount="90000000.00",
        cost_amount="81000000.00",
        cost_confirmed=True,
        project_type="state",
        status="completed",
    )
    secret = Project.objects.create(
        name="Hidden", team=hidden, manager=b, version=1, is_verified=True
    )
    old = Project.objects.create(
        name="Archived",
        team=team,
        manager=b,
        version=1,
        is_verified=True,
        archived=True,
    )
    today = timezone.now().date()
    for project, amount, verified in [
        (p, "100.00", True),
        (p, "-20.00", True),
        (p, "10.00", False),
        (secret, "999.00", True),
        (old, "555.00", True),
    ]:
        FinancialRecord.objects.create(
            project=project,
            amount=amount,
            payment_date=today,
            is_verified=verified,
            credited_profile=a,
            currency="KZT",
            status="received",
        )
    SalesTarget.objects.create(
        team=team, profile=b, month=today.replace(day=1), amount="1000.00"
    )
    SalesTarget.objects.create(
        team=hidden, profile=b, month=today.replace(day=1), amount="9000.00"
    )
    Commitment.objects.create(
        manager=a,
        team=team,
        project=None,
        commitment_text="Own standalone",
        deadline_at=timezone.now() - timedelta(hours=2),
        is_verified=True,
    )
    imported = Project.objects.create(
        name="CRM identity", team=team, identity_confirmed=True
    )
    CrmProjectSnapshot.objects.create(
        project=imported,
        external_stage_id="WON",
        external_stage_name="Выиграна",
        opportunity="500.00",
    )
    config = WhatsAppConfig.objects.create(
        team=team, group_jid="review-" + suffix + "@g.us"
    )
    for revision, state, known in [
        ("1", "received", True),
        ("2", "received", True),
        ("unknown", "received", False),
        ("deleted", "deleted", True),
    ]:
        RawMessage.objects.create(
            config=config,
            team=team,
            message_id=suffix if revision in ["1", "2"] else suffix + revision,
            source_revision=revision,
            timestamp=timezone.now(),
            processing_state=state,
            sent_at_known=known,
            content="Original",
        )
    business = lambda: {
        m.__name__: list(m.objects.order_by("id").values())
        for m in [
            Project,
            FinancialRecord,
            SalesTarget,
            Commitment,
            CrmProjectSnapshot,
            RawMessage,
        ]
    }
    before = business()
    op = AsyncOperation.objects.create(
        requested_by=user,
        idempotency_key="integration-dynamic",
        status="running",
        expires_at=timezone.now() + timedelta(hours=1),
        access_fingerprint=access.fingerprint(user),
        request={},
    )
    ctx = OperationContext(
        op.id,
        user.id,
        op.access_fingerprint,
        op.expires_at,
        time.monotonic() + 150,
        {"period": "this_month", "currency": "KZT"},
    )
    schema = ctx.schema()
    assert "projects" in schema["datasets"]
    payments = ctx.query(
        {
            "dataset": "payments",
            "dimensions": ["credited_manager", "payment_week"],
            "measures": ["received_amount", "payment_count"],
            "filters": [
                {"field": "project_type", "op": "eq", "value": "state"},
                {"field": "contract_amount", "op": "gt", "value": "5000000.00"},
            ],
        }
    )
    assert payments["rows"][0]["received_amount"] == "80.00", payments
    assert payments["rows"][0]["credited_manager"] == f"Attributed (#{a.id})"
    margin = ctx.query(
        {
            "dataset": "projects",
            "dimensions": [],
            "measures": ["contract_margin_percent"],
        }
    )
    assert margin["rows"][0]["contract_margin_percent"] == 14.0, margin
    targets = ctx.query(
        {"dataset": "targets", "dimensions": ["manager"], "measures": ["target_amount"]}
    )
    assert targets["rows"][0]["target_amount"] == "1000.00", targets
    commitments = ctx.query(
        {
            "dataset": "commitments",
            "dimensions": ["responsible_manager"],
            "measures": ["overdue_count"],
        }
    )
    assert commitments["rows"][0]["overdue_count"] == 1
    messages = ctx.query(
        {
            "dataset": "messages",
            "dimensions": ["message_day"],
            "measures": ["message_count"],
            "currency": "USD",
        }
    )
    assert messages["rows"][0]["message_count"] == 1, messages
    assert messages["normalized_query"]["currency"] is None
    assert "1 сообщений" in messages["coverage"]["message"]
    crm = ctx.query(
        {
            "dataset": "crm_projects",
            "dimensions": ["status"],
            "measures": ["project_count", "crm_amount"],
        }
    )
    assert crm["rows"] == [
        {"status": "Выиграна", "project_count": 1, "crm_amount": "500.00"}
    ], crm
    unknown_stages = ctx.query(
        {
            "dataset": "projects",
            "dimensions": ["status"],
            "measures": ["project_count"],
            "filters": [{"field": "status", "op": "eq", "value": "qualification"}],
        }
    )
    assert not unknown_stages["rows"], unknown_stages
    records = ctx.records({"overdue_only": True, "limit": 5})
    assert records["rows"][0]["text"] == "Own standalone", records
    try:
        ctx.query(
            {
                "dataset": "payments",
                "dimensions": ["credited_manager"],
                "measures": ["received_amount"],
                "filters": [{"field": "manager_id", "op": "eq", "value": str(a.id)}],
            }
        )
    except ProviderUnavailable:
        pass
    else:
        raise AssertionError("String business ID accepted")
    connection = connections["analytics_readonly"]
    for statement in [
        "UPDATE api_project SET name='mutated'",
        "INSERT INTO api_team (name,is_active,stalled_days,low_margin_percent,rules_version) VALUES ('injected',true,3,15,1)",
        "DELETE FROM api_financialrecord",
        "CREATE TABLE public.analytics_injected(id int)",
        "SELECT password FROM auth_user",
        "SELECT * FROM api_aisettings",
    ]:
        try:
            with transaction.atomic(using="analytics_readonly"):
                with connection.cursor() as c:
                    c.execute("SET TRANSACTION READ WRITE")
                    c.execute(statement)
        except Exception:
            pass
        else:
            raise AssertionError("Read-only role allowed: " + statement)
    # Real HTTP view receipt, real MCP protocol, deterministic provider fixture.
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.post(
        "/api/chat/query/",
        {
            "prompt": "Недельные поступления только по государственным проектам",
            "period": "this_month",
            "currency": "KZT",
            "idempotency_key": "http-dynamic",
        },
        format="json",
    )
    assert response.status_code == 202, response.data
    dynamic = AsyncOperation.objects.get(id=response.data["operation_id"])

    def turn(messages, tools, deadline):
        if len(messages) == 2:
            return {
                "text": "",
                "tool_calls": [
                    {"id": "describe", "name": "describe_schema", "arguments": {}}
                ],
            }
        last = json.loads(messages[-1]["content"])
        if "datasets" in last:
            return {
                "text": "",
                "tool_calls": [
                    {
                        "id": "query",
                        "name": "query_dataset",
                        "arguments": {
                            "dataset": "payments",
                            "dimensions": ["credited_manager", "payment_week"],
                            "measures": ["received_amount"],
                            "filters": [
                                {"field": "project_type", "op": "eq", "value": "state"}
                            ],
                        },
                    }
                ],
            }
        if "dataset_id" in last:
            return {
                "text": "",
                "tool_calls": [
                    {
                        "id": "build",
                        "name": "build_presentation",
                        "arguments": {
                            "version": "1.0",
                            "title": "Поступления",
                            "blocks": [
                                {
                                    "id": "weekly",
                                    "kind": "line",
                                    "dataset_id": last["dataset_id"],
                                    "encoding": {
                                        "category": "payment_week",
                                        "series": "credited_manager",
                                        "value": "received_amount",
                                    },
                                    "size": "wide",
                                }
                            ],
                        },
                    }
                ],
            }
        return {"text": "Результат представлен ниже.", "tool_calls": []}

    from api.operations import execute_operation

    with patch("api.analytics.host.AIService.analytics_turn", side_effect=turn):
        execute_operation(dynamic.id)
    result = client.get(f"/api/operations/{dynamic.id}/")
    assert result.data["status"] == "succeeded", result.data
    assert result.data["result"]["presentation"]["blocks"][0]["kind"] == "line"
    assert before == business(), "Analytics mutated business facts"
    TeamMembership.objects.filter(user=user).update(status="revoked")
    try:
        ctx.check()
    except ProviderUnavailable:
        pass
    else:
        raise AssertionError("Revoked scope allowed")
    # Restore grants for a genuine queued standard operation processed by Q2.
    TeamMembership.objects.filter(user=user).update(status="active")
    receipt = client.post(
        "/api/chat/query/",
        {
            "prompt": "Покажи график поступлений",
            "period": "this_month",
            "currency": "KZT",
            "idempotency_key": "queue-standard",
        },
        format="json",
    )
    assert receipt.status_code == 202
    dispatch_outbox()
    print(
        json.dumps(
            {
                "checks": "scopes, attribution, reversal, weighted margin, target scope, standalone commitments, role DML/DDL/secrets, MCP loop, no mutations, revocation",
                "queued_operation_id": receipt.data["operation_id"],
            }
        )
    )


if __name__ == "__main__":
    verify()
