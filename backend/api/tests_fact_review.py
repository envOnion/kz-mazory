from datetime import timedelta
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from .models import (
    Team,
    TeamMembership,
    UserProfile,
    WhatsAppConfig,
    RawMessage,
    MessageProcessingTrace,
    FactCandidate,
    FactEvidence,
    Project,
    FinancialRecord,
    ChatAccess,
)


@override_settings(ALLOWED_HOSTS=["testserver"])
class FactReviewContextTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Sales")
        self.user = User.objects.create_user("79991234567")
        self.profile = UserProfile.objects.create(user=self.user, full_name="Manager")
        TeamMembership.objects.create(
            user=self.user, team=self.team, role="team_lead", status="active"
        )
        self.config = WhatsAppConfig.objects.create(
            team=self.team, name="Рабочий чат", group_jid="sales@g.us"
        )
        self.source = self.message(0)
        self.trace = MessageProcessingTrace.objects.create(
            raw_message=self.source,
            whatsapp_message_id=self.source.message_id,
            whatsapp_content=self.source.content,
        )
        self.candidate = FactCandidate.objects.create(
            trace=self.trace,
            team=self.team,
            source_key="fact:1",
            fact_type="project",
            proposed_changes={"object_name": "Объект", "contract_amount": "1000"},
        )
        FactEvidence.objects.create(
            candidate=self.candidate, raw_message=self.source, quote="Сообщение 0"
        )
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.url = f"/api/candidates/{self.candidate.pk}/context/"

    def message(self, offset, config=None, **extra):
        config = config or self.config
        return RawMessage.objects.create(
            config=config,
            team=config.team,
            chat_id=config.group_jid,
            message_id=f"{config.pk}:{offset}",
            timestamp=timezone.now() + timedelta(minutes=offset),
            sender_name="Автор",
            content=f"Сообщение {offset}",
            **extra,
        )

    def test_chronological_neighbors_and_both_pagination_directions(self):
        earlier = [self.message(-i) for i in range(1, 41)]
        later = [self.message(i) for i in range(1, 41)]
        response = self.api.get(self.url)
        self.assertEqual(response.status_code, 200)
        data = response.data
        self.assertEqual(len(data["messages"]), 25)
        self.assertEqual([m["is_source"] for m in data["messages"]].count(True), 1)
        self.assertEqual(data["messages"][0]["id"], earlier[11].id)
        self.assertEqual(data["messages"][-1]["id"], later[11].id)
        before = self.api.get(
            self.url, {"direction": "before", "anchor": data["before"]}
        ).data
        self.assertEqual(len(before["messages"]), 25)
        self.assertEqual(before["messages"][-1]["id"], earlier[12].id)
        after = self.api.get(
            self.url, {"direction": "after", "anchor": data["after"]}
        ).data
        self.assertEqual(after["messages"][0]["id"], later[12].id)
        self.assertFalse(
            set(m["id"] for m in data["messages"])
            & set(m["id"] for m in before["messages"])
        )

    def test_ai_snapshot_is_partial_and_foreign_source_is_hidden(self):
        earlier = self.message(-1)
        foreign_team = Team.objects.create(name="Private")
        foreign_config = WhatsAppConfig.objects.create(
            team=foreign_team, group_jid="private@g.us"
        )
        secret = self.message(-2, config=foreign_config)
        self.trace.earlier_messages_context = [
            {
                "raw_message_id": earlier.id,
                "content": "Последняя часть",
                "partial": True,
            },
            {
                "raw_message_id": secret.id,
                "content": "SECRET",
                "sender_name": "SECRET AUTHOR",
            },
        ]
        self.trace.context_metadata = {"history_complete_in_request": False}
        self.trace.save()
        response = self.api.get(self.url)
        self.assertEqual(len(response.data["ai_messages"]), 1)
        self.assertEqual(response.data["ai_messages"][0]["content"], "Последняя часть")
        self.assertTrue(response.data["ai_messages"][0]["partial"])
        self.assertNotIn("SECRET", response.content.decode())
        self.assertNotIn(secret.id, [m["id"] for m in response.data["messages"]])
        self.assertEqual(self.api.get(self.url, {"anchor": secret.pk}).status_code, 404)

    def test_candidate_and_source_permissions_and_authentication(self):
        stranger = User.objects.create_user("stranger")
        self.api.force_authenticate(stranger)
        self.assertEqual(self.api.get(self.url).status_code, 404)
        self.api.force_authenticate(None)
        self.assertEqual(self.api.get(self.url).status_code, 401)
        TeamMembership.objects.filter(user=self.user).update(role="manager")
        self.candidate.manager = self.profile
        self.candidate.save()
        self.api.force_authenticate(self.user)
        hidden = self.api.get(self.url)
        self.assertEqual(hidden.status_code, 200)
        self.assertIsNone(hidden.data["source"])
        self.assertEqual(hidden.data["messages"], [])
        self.assertNotIn("Сообщение 0", hidden.content.decode())
        ChatAccess.objects.create(user=self.user, config=self.config)
        self.assertIsNotNone(self.api.get(self.url).data["source"])

    def test_deleted_source_empty_and_unknown_timestamp(self):
        self.trace.raw_message = None
        self.trace.save()
        self.assertIsNone(self.api.get(self.url).data["source"])
        self.trace.raw_message = self.source
        self.trace.save()
        self.source.sent_at_known = False
        self.source.save()
        data = self.api.get(self.url).data
        self.assertIsNone(data["source"]["sent_at"])
        self.assertIn("Сохранённой истории", data["coverage"])

    def test_ai_snapshot_has_pagination(self):
        messages = [self.message(-i) for i in range(1, 31)]
        self.trace.earlier_messages_context = [
            {"raw_message_id": m.pk, "content": m.content} for m in reversed(messages)
        ]
        self.trace.save()
        first = self.api.get(self.url).data
        self.assertEqual(len(first["ai_messages"]), 25)
        self.assertEqual(first["ai_next_page"], 2)
        self.assertEqual(
            len(self.api.get(self.url, {"ai_page": 2}).data["ai_messages"]), 5
        )

    def test_current_values_and_payment_choices_are_scoped(self):
        project = Project.objects.create(
            name="Объект", team=self.team, contract_amount="500", cost_amount="100"
        )
        self.candidate.project = project
        self.candidate.save()
        data = self.api.get(f"/api/candidates/{self.candidate.pk}/").data
        self.assertEqual(data["current_values"]["contract_amount"], "500.00")
        self.assertEqual(data["chat_name"], "Рабочий чат")
        payment_url = f"/api/candidates/{self.candidate.pk}/payments/"
        self.assertEqual(self.api.get(payment_url).status_code, 403)
        TeamMembership.objects.create(
            user=self.user, team=self.team, role="finance", status="active"
        )
        payment = FinancialRecord.objects.create(
            project=project,
            amount="50",
            payment_date=timezone.localdate(),
            is_verified=True,
        )
        other = Project.objects.create(
            name="Other", team=Team.objects.create(name="Other")
        )
        FinancialRecord.objects.create(
            project=other,
            amount="99",
            payment_date=timezone.localdate(),
            is_verified=True,
        )
        self.assertEqual(
            [p["id"] for p in self.api.get(payment_url).data["results"]], [payment.id]
        )

    def test_newer_neighbor_revision_replaces_old_without_losing_source(self):
        old = self.message(-1)
        new = RawMessage.objects.create(
            config=self.config,
            team=self.team,
            chat_id=self.config.group_jid,
            message_id=old.message_id,
            source_revision="1",
            timestamp=old.timestamp,
            received_at=old.received_at + timedelta(seconds=1),
            content="Обновлённый текст",
        )
        ids = [m["id"] for m in self.api.get(self.url).data["messages"]]
        self.assertIn(new.id, ids)
        self.assertNotIn(old.id, ids)
        self.assertIn(self.source.id, ids)
