from datetime import timedelta
from decimal import Decimal
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from .models import (
    Team,
    TeamMembership,
    UserProfile,
    Company,
    Project,
    RawMessage,
    MessageProcessingTrace,
    FactCandidate,
    FactEvidence,
    Notification,
    NotificationDelivery,
    OutboxEvent,
)
from .notifications import (
    notify_on_new_candidate,
    notify_on_candidate_approved,
)


class CandidateNotificationsTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Отдел продаж")
        self.manager_user = User.objects.create_user(username="77011112233")
        self.manager_profile = UserProfile.objects.create(
            user=self.manager_user,
            full_name="Менеджер Иванов",
            notification_preferences={"whatsapp": True},
        )
        TeamMembership.objects.create(
            team=self.team,
            user=self.manager_user,
            role="manager",
            status="active",
        )

        self.team_lead_user = User.objects.create_user(username="77019998877")
        self.team_lead_profile = UserProfile.objects.create(
            user=self.team_lead_user,
            full_name="Тимлид Петров",
            notification_preferences={"whatsapp": True},
        )
        TeamMembership.objects.create(
            team=self.team,
            user=self.team_lead_user,
            role="team_lead",
            status="active",
        )

        self.company = Company.objects.create(
            name="BI Group",
            bitrix_company_id="1001",
        )
        self.project = Project.objects.create(
            name="ЖК Grand Park",
            team=self.team,
            company=self.company,
            manager=self.manager_profile,
            contract_amount=148500000.0,
            is_verified=True,
        )

        self.raw_message = RawMessage.objects.create(
            team=self.team,
            project=self.project,
            message_id="waha_msg_9999",
            sender_name="Асан Сериков",
            sender_phone="+77015554433",
            timestamp=timezone.now(),
            content="Согласовали котельные ЖК Grand Park для BI Group на 148.5 млн ₸, ТУ до 15.10.2026",
        )
        self.trace = MessageProcessingTrace.objects.create(
            raw_message=self.raw_message,
            whatsapp_message_id=self.raw_message.message_id,
            whatsapp_content=self.raw_message.content,
        )

        self.candidate = FactCandidate.objects.create(
            trace=self.trace,
            team=self.team,
            project=self.project,
            manager=self.manager_profile,
            fact_type="commitment",
            source_key="cand_commitment_test_1",
            confidence=0.95,
            proposed_changes={
                "company_name": "BI Group",
                "object_name": "ЖК Grand Park",
                "commitment_text": "Поставка 4 БМК и согласование ТУ",
                "amount": "148500000",
                "deadline": "2026-10-15",
                "sender_name": "Асан Сериков",
            },
        )
        FactEvidence.objects.create(
            candidate=self.candidate,
            raw_message=self.raw_message,
            quote="Согласовали котельные ЖК Grand Park для BI Group на 148.5 млн ₸, ТУ до 15.10.2026",
            field_name="commitment",
        )

    def test_candidate_creation_generates_notifications_and_waha_delivery(self):
        created_notifs = notify_on_new_candidate(self.candidate)
        self.assertEqual(len(created_notifs), 2)

        # 1. Assert Notification is created for both manager and team_lead
        manager_notif = Notification.objects.filter(
            recipient=self.manager_user,
            category="commitment_detected",
        ).first()
        self.assertIsNotNone(manager_notif, "Notification for manager should exist")
        self.assertEqual(manager_notif.title, "Новое обязательство из чата")

        team_lead_notif = Notification.objects.filter(
            recipient=self.team_lead_user,
            category="commitment_detected",
        ).first()
        self.assertIsNotNone(team_lead_notif, "Notification for team_lead should exist")
        self.assertEqual(team_lead_notif.title, "Новое обязательство из чата")

        # 2. Assert notification message contains the company name, project name, amount in ₸, and deadline
        for notif in [manager_notif, team_lead_notif]:
            self.assertIn("BI Group", notif.message)
            self.assertIn("ЖК Grand Park", notif.message)
            self.assertIn("148 500 000 ₸", notif.message)
            self.assertIn("15.10.2026", notif.message)
            self.assertIn("Поставка 4 БМК и согласование ТУ", notif.message)
            self.assertIn("Асан Сериков", notif.message)

        # 3. Assert NotificationDelivery is created with channel="whatsapp", state="queued"
        manager_delivery = NotificationDelivery.objects.filter(
            notification=manager_notif
        ).first()
        self.assertIsNotNone(manager_delivery, "Delivery for manager should exist")
        self.assertEqual(manager_delivery.channel, "whatsapp")
        self.assertEqual(manager_delivery.state, "queued")

        team_lead_delivery = NotificationDelivery.objects.filter(
            notification=team_lead_notif
        ).first()
        self.assertIsNotNone(team_lead_delivery, "Delivery for team_lead should exist")
        self.assertEqual(team_lead_delivery.channel, "whatsapp")
        self.assertEqual(team_lead_delivery.state, "queued")

        # 4. Assert OutboxEvent with event_type="notification" is created
        outbox_events = OutboxEvent.objects.filter(event_type="notification")
        self.assertGreaterEqual(outbox_events.count(), 2)

        manager_outbox = outbox_events.filter(
            payload__delivery_id=manager_delivery.id
        ).first()
        self.assertIsNotNone(manager_outbox, "OutboxEvent for manager delivery should exist")
        self.assertEqual(manager_outbox.state, "pending")

        team_lead_outbox = outbox_events.filter(
            payload__delivery_id=team_lead_delivery.id
        ).first()
        self.assertIsNotNone(team_lead_outbox, "OutboxEvent for team_lead delivery should exist")
        self.assertEqual(team_lead_outbox.state, "pending")

    def test_candidate_approval_generates_notification(self):
        created_notifs = notify_on_candidate_approved(self.candidate)
        self.assertEqual(len(created_notifs), 2)

        # Assert confirmation notification is created
        approval_notifs = Notification.objects.filter(
            deduplication_key__startswith=f"candidate_approved:{self.candidate.id}"
        )
        self.assertEqual(approval_notifs.count(), 2)

        manager_approval = approval_notifs.filter(recipient=self.manager_user).first()
        self.assertIsNotNone(manager_approval)
        self.assertEqual(manager_approval.title, "Подтверждено обязательство")
        self.assertIn("BI Group", manager_approval.message)
        self.assertIn("ЖК Grand Park", manager_approval.message)
        self.assertIn("148 500 000 ₸", manager_approval.message)

        team_lead_approval = approval_notifs.filter(recipient=self.team_lead_user).first()
        self.assertIsNotNone(team_lead_approval)
        self.assertEqual(team_lead_approval.title, "Подтверждено обязательство")

        # Deliveries created for approval notifications
        self.assertTrue(
            NotificationDelivery.objects.filter(
                notification=manager_approval, channel="whatsapp", state="queued"
            ).exists()
        )
        self.assertTrue(
            NotificationDelivery.objects.filter(
                notification=team_lead_approval, channel="whatsapp", state="queued"
            ).exists()
        )

    def test_deal_candidate_creation_and_approval_titles(self):
        deal_candidate = FactCandidate.objects.create(
            trace=self.trace,
            team=self.team,
            project=self.project,
            manager=self.manager_profile,
            fact_type="project",
            source_key="cand_deal_test_2",
            confidence=0.91,
            proposed_changes={
                "company_name": "BI Group",
                "object_name": "ЖК Grand Park",
                "contract_amount": "250000000",
            },
        )
        notifs = notify_on_new_candidate(deal_candidate)
        self.assertEqual(len(notifs), 2)
        self.assertEqual(notifs[0].title, "Выявлена новая сделка")
        self.assertIn("250 000 000 ₸", notifs[0].message)

        approved_notifs = notify_on_candidate_approved(deal_candidate)
        self.assertEqual(len(approved_notifs), 2)
        self.assertEqual(approved_notifs[0].title, "Подтверждена сделка")
