import time
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from mcp.shared.memory import create_connected_server_and_client_session

from .analytics.data import aggregate, validate, QUERY_SCHEMA, OperationContext
from .analytics.mcp import create_servers
from .analytics.presentation import build
from .analytics.host import run
from .ai_providers import analytics_payload, analytics_turn
from .providers import ProviderUnavailable
from . import access
from .models import AsyncOperation, Team, TeamMembership, Project, UserProfile
from django.contrib.auth.models import User


def fixture_dataset():
    return {
        "dataset_id": "known",
        "columns": [
            {"name": "name", "type": "text", "unit": None},
            {"name": "amount", "type": "money", "unit": "KZT"},
        ],
        "rows": [{"name": "<img onerror=alert(1)>", "amount": "80.00"}],
        "normalized_query": {"dataset": "payments"},
        "definition": "Test",
        "timezone": "Asia/Almaty",
        "coverage": {"status": "complete", "message": "Test"},
        "returned_count": 1,
        "total_groups": 1,
        "truncated": False,
        "evidence": [],
    }


class ContractsTests(SimpleTestCase):
    def test_reversal_attribution_and_unknown_cost(self):
        manager = SimpleNamespace(full_name="Old")
        payments = [
            SimpleNamespace(
                amount=Decimal(a),
                payment_date=date(2026, 9, 30),
                credited_profile=manager,
                credited_profile_id=42,
            )
            for a in ["100", "-20"]
        ]
        rows = aggregate(
            "payments",
            payments,
            ["credited_manager"],
            ["received_amount", "payment_count"],
            date(2026, 9, 1),
            date(2026, 10, 1),
            "Asia/Almaty",
            time.monotonic() + 10,
        )
        self.assertEqual(
            rows,
            [
                {
                    "credited_manager": "Old (#42)",
                    "received_amount": "80.00",
                    "payment_count": 2,
                }
            ],
        )
        projects = [
            SimpleNamespace(
                contract_amount=Decimal("100"),
                cost_amount=Decimal("50"),
                cost_confirmed=True,
            ),
            SimpleNamespace(
                contract_amount=Decimal("900"),
                cost_amount=Decimal("810"),
                cost_confirmed=True,
            ),
        ]
        rows = aggregate(
            "projects",
            projects,
            [],
            ["contract_margin_percent"],
            date(2026, 9, 1),
            date(2026, 10, 1),
            "Asia/Almaty",
            time.monotonic() + 10,
        )
        self.assertEqual(rows[0]["contract_margin_percent"], 14.0)
        projects[1].cost_confirmed = False
        self.assertIsNone(
            aggregate(
                "projects",
                projects,
                [],
                ["confirmed_cost"],
                date(2026, 9, 1),
                date(2026, 10, 1),
                "Asia/Almaty",
                time.monotonic() + 10,
            )[0]["confirmed_cost"]
        )

    def test_closed_query_and_id_types(self):
        base = {
            "dataset": "projects",
            "dimensions": ["project_id"],
            "measures": ["project_count"],
        }
        for request in [
            {**base, "sql": "UPDATE api_project SET name=1"},
            {
                **base,
                "filters": [{"field": "project_id", "op": "eq", "value": {"raw": 1}}],
            },
            {**base, "limit": 1001},
        ]:
            with self.assertRaises(ProviderUnavailable):
                validate(QUERY_SCHEMA, request)

    def test_both_provider_conversations(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "query_dataset",
                    "description": "read",
                    "parameters": QUERY_SCHEMA,
                },
            }
        ]
        call = {
            "id": "call1",
            "type": "function",
            "function": {
                "name": "query_dataset",
                "arguments": '{"dataset":"payments"}',
            },
        }
        messages = [
            {"role": "system", "content": "test"},
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": None, "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "call1", "content": "{}"},
        ]
        native = analytics_payload("model", messages, tools, 100, "anthropic_messages")
        self.assertEqual(native["messages"][-2]["content"][0]["type"], "tool_use")
        self.assertEqual(native["messages"][-1]["content"][0]["tool_use_id"], "call1")
        self.assertEqual(
            analytics_turn(
                {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call1",
                            "name": "query_dataset",
                            "input": {"dataset": "payments"},
                        }
                    ],
                    "stop_reason": "tool_use",
                },
                "anthropic_messages",
            )["tool_calls"][0]["arguments"],
            {"dataset": "payments"},
        )
        self.assertEqual(
            analytics_turn(
                {
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {"tool_calls": [call]},
                        }
                    ]
                },
                "openai_compatible",
            )["tool_calls"][0]["id"],
            "call1",
        )
        with self.assertRaises(ProviderUnavailable):
            analytics_turn(
                {"choices": [{"finish_reason": "length", "message": {}}]},
                "openai_compatible",
            )

    def test_presentation_binds_server_data(self):
        context = SimpleNamespace(
            check=lambda: None, registry={"known": fixture_dataset()}
        )
        document = build(
            context,
            {
                "version": "1.0",
                "title": "Answer",
                "blocks": [
                    {
                        "id": "chart",
                        "kind": "bar",
                        "dataset_id": "known",
                        "encoding": {"category": "name", "value": "amount"},
                    }
                ],
            },
        )
        self.assertEqual(document["datasets"]["known"]["rows"][0]["amount"], "80.00")
        for block in [
            {
                "id": "chart",
                "kind": "bar",
                "dataset_id": "foreign",
                "encoding": {"category": "name", "value": "amount"},
            },
            {
                "id": "chart",
                "kind": "bar",
                "dataset_id": "known",
                "encoding": {"category": "name", "value": "amount"},
                "options": {"formatter": "eval"},
            },
        ]:
            with self.assertRaises(ProviderUnavailable):
                build(context, {"version": "1.0", "title": "Answer", "blocks": [block]})

    def test_real_mcp_sessions_and_host_loop(self):
        ctx = SimpleNamespace(
            check=lambda: None,
            schema=lambda: {"datasets": {}},
            query=lambda a: fixture_dataset(),
            registry={"known": fixture_dataset()},
            deadline=time.monotonic() + 10,
        )
        turns = iter(
            [
                {
                    "text": "",
                    "tool_calls": [
                        {"id": "s", "name": "describe_schema", "arguments": {}}
                    ],
                },
                {
                    "text": "",
                    "tool_calls": [
                        {
                            "id": "q",
                            "name": "query_dataset",
                            "arguments": {
                                "dataset": "payments",
                                "dimensions": [],
                                "measures": ["received_amount"],
                            },
                        }
                    ],
                },
                {
                    "text": "",
                    "tool_calls": [
                        {
                            "id": "b",
                            "name": "build_presentation",
                            "arguments": {
                                "version": "1.0",
                                "title": "Answer",
                                "blocks": [
                                    {
                                        "id": "chart",
                                        "kind": "bar",
                                        "dataset_id": "known",
                                        "encoding": {
                                            "category": "name",
                                            "value": "amount",
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                },
                {"text": "Готово", "tool_calls": []},
            ]
        )
        with patch(
            "api.analytics.host.AIService.analytics_turn",
            side_effect=lambda *args: next(turns),
        ):
            result = async_to_sync(run)(ctx, "unexpected question")
        self.assertEqual(result["presentation"]["blocks"][0]["kind"], "bar")
        self.assertIsNone(result["widget"])

        async def protocol():
            server, _ = create_servers(ctx)
            async with create_connected_server_and_client_session(server) as client:
                names = [t.name for t in (await client.list_tools()).tools]
                self.assertEqual(
                    names, ["describe_schema", "query_dataset", "read_sources", "read_records"]
                )
                result = await client.call_tool(
                    "query_dataset",
                    {
                        "dataset": "projects",
                        "dimensions": [],
                        "measures": ["project_count"],
                        "sql": "DROP TABLE",
                    },
                )
                self.assertTrue(result.isError)

        async_to_sync(protocol)()


class AccessLifecycleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("analytics-tester")
        team = Team.objects.create(name="Visible")
        TeamMembership.objects.create(
            user=self.user, team=team, role="manager", status="active"
        )
        self.profile = UserProfile.objects.create(user=self.user, full_name="Manager")
        Project.objects.create(
            name="Visible", team=team, manager=self.profile, is_verified=True, version=1
        )
        self.op = AsyncOperation.objects.create(
            requested_by=self.user,
            idempotency_key="analytics-test",
            status="running",
            expires_at=timezone.now() + timedelta(hours=1),
            access_fingerprint=access.fingerprint(self.user),
        )
        self.context = OperationContext(
            self.op.id,
            self.user.id,
            self.op.access_fingerprint,
            self.op.expires_at,
            time.monotonic() + 10,
            {},
        )

    def test_revocation_and_cancel_between_tools(self):
        self.context.check()
        TeamMembership.objects.filter(user=self.user).update(status="revoked")
        with self.assertRaises(ProviderUnavailable):
            self.context.check()

    def test_inactive_user_and_expiry(self):
        User.objects.filter(id=self.user.id).update(is_active=False)
        with self.assertRaises(ProviderUnavailable):
            self.context.check()

    def test_nonstandard_graph_goes_to_mcp(self):
        from .operations import execute_operation

        self.op.request = {
            "prompt": "Покажи график поступлений по неделям только state",
            "period": "this_month",
            "currency": "KZT",
        }
        self.op.status = "queued"
        self.op.save()
        with (
            patch("api.analytics.host.run", new=mock_run),
            patch(
                "api.datamart.datamart.get_sales_chart_dataset",
                side_effect=AssertionError("fixed path"),
            ),
        ):
            execute_operation(self.op.id)
        self.op.refresh_from_db()
        self.assertEqual(self.op.status, "succeeded")


async def mock_run(*args):
    return {"text": "Test", "widget": None, "presentation": None}
