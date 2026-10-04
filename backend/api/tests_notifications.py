from decimal import Decimal
from datetime import datetime, date
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from .models import (
    Team,
    TeamMembership,
    UserProfile,
    Company,
    Project,
    FactCandidate,
    RawMessage,
    MessageProcessingTrace,
    Notification,
    NotificationDelivery,
    OutboxEvent,
)
from .notifications import notify_on_new_candidate, notify_on_candidate_approved


@override_settings(ALLOWED_HOSTS=["testserver"])
class CandidateNotificationsTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Команда Продаж")
        self.manager_user = User.objects.create_user("77011112233", first_name="Айбек")
        self.manager_profile = UserProfile.objects.create(
            user=self.manager_user,
            full_name="Айбек Менеджер",
            phone="77011112233",
            notification_preferences={"whatsapp": True, "commitment_detected": True},
        )
        TeamMembership.objects.create(
            user=self.manager_user,
            team=self.team,
            role="manager",
            status="active",
        )

        self.lead_user = User.objects.create_user("77022223344", first_name="Бауржан")
        self.lead_profile = UserProfile.objects.create(
            user=self.lead_user,
            full_name="Бауржан Тимлид",
            phone="77022223344",
            notification_preferences={"whatsapp": True, "commitment_detected": True},
        )
        TeamMembership.objects.create(
            user=self.lead_user,
            team=self.team,
            role="team_lead",
            status="active",
        )

        self.company = Company.objects.create(name="BI Group", bitrix_company_id="101")
        self.project = Project.objects.create(
            team=self.team,
            company=self.company,
            name="ЖК Grand Park",
            manager=self.manager_profile,
            contract_amount=Decimal("148500000.00"),
            currency="KZT",
            status="in_progress",
        )

        self.raw_message = RawMessage.objects.create(
            team=self.team,
            message_id="msg-100",
            timestamp=timezone.now(),
            sender_name="Бауржан Касымов",
            sender_phone="+77022223344",
            content="Согласовали котельные ЖК Grand Park для BI Group на 148.5 млн ₸, ТУ до пятницы",
        )
        self.trace = MessageProcessingTrace.objects.create(
            raw_message=self.raw_message,
            whatsapp_message_id=self.raw_message.message_id,
            whatsapp_content=self.raw_message.content,
        )

    def test_notify_on_new_candidate_commitment(self):
        candidate = FactCandidate.objects.create(
            trace=self.trace,
            team=self.team,
            project=self.project,
            manager=self.manager_profile,
            fact_type="commitment",
            source_key="source:candidate:1",
            proposed_changes={
                "company_name": "BI Group",
                "object_name": "ЖК Grand Park",
                "commitment_text": "Передать ТУ и согласовать график поставок",
                "amount": "148500000",
                "deadline": "2026-10-10T18:00:00",
            },
        )

        notifications = notify_on_new_candidate(candidate)
        self.assertEqual(len(notifications), 2)

        recipients = {n.recipient_id for n in notifications}
        self.assertIn(self.manager_user.id, recipients)
        self.assertIn(self.lead_user.id, recipients)

        notif = notifications[0]
        self.assertEqual(notif.title, "Новое обязательство из чата")
        self.assertEqual(notif.category, "commitment_detected")

        # Check body lines
        self.assertIn("Контрагент (Bitrix): BI Group", notif.message)
        self.assertIn("Сделка / Объект: ЖК Grand Park", notif.message)
        self.assertIn("Суть: Передать ТУ и согласовать график поставок", notif.message)
        self.assertIn("Сумма: 148 500 000 ₸", notif.message)
        self.assertIn("Срок: 10.10.2026 18:00", notif.message)
        self.assertIn("Отправитель WhatsApp: Бауржан Касымов", notif.message)

        # Check NotificationDelivery and OutboxEvent for WAHA
        deliveries = NotificationDelivery.objects.filter(notification__in=notifications)
        self.assertEqual(deliveries.count(), 2)
        for d in deliveries:
            self.assertEqual(d.channel, "whatsapp")
            self.assertEqual(d.state, "queued")

        outbox_events = OutboxEvent.objects.filter(event_type="notification")
        self.assertEqual(outbox_events.count(), 2)

    def test_notify_on_new_candidate_project(self):
        candidate = FactCandidate.objects.create(
            trace=self.trace,
            team=self.team,
            manager=self.manager_profile,
            fact_type="project",
            source_key="source:candidate:2",
            proposed_changes={
                "company_name": "Bazis-A",
                "object_name": "ЖК Highvill",
                "description": "Поставка насосного оборудования",
                "contract_amount": "55000000",
                "deadline": "2026-11-01",
            },
        )

        notifications = notify_on_new_candidate(candidate)
        self.assertEqual(len(notifications), 2)
        notif = notifications[0]
        self.assertEqual(notif.title, "Выявлена новая сделка")
        self.assertIn("Контрагент (Bitrix): Bazis-A", notif.message)
        self.assertIn("Сделка / Объект: ЖК Highvill", notif.message)
        self.assertIn("Суть: Поставка насосного оборудования", notif.message)
        self.assertIn("Сумма: 55 000 000 ₸", notif.message)
        self.assertIn("Срок: 01.11.2026", notif.message)

    def test_notify_on_new_candidate_payment(self):
        candidate = FactCandidate.objects.create(
            trace=self.trace,
            team=self.team,
            manager=self.manager_profile,
            fact_type="payment",
            source_key="source:candidate:3",
            proposed_changes={
                "company_name": "BI Group",
                "object_name": "ЖК Grand Park",
                "commitment_text": "Аванс 30% по договору",
                "amount": "44550000",
            },
        )

        notifications = notify_on_new_candidate(candidate)
        self.assertEqual(len(notifications), 2)
        notif = notifications[0]
        self.assertEqual(notif.title, "Договоренность об оплате")
        self.assertIn("Суть: Аванс 30% по договору", notif.message)
        self.assertIn("Сумма: 44 550 000 ₸", notif.message)

    def test_notify_on_candidate_approved(self):
        candidate = FactCandidate.objects.create(
            trace=self.trace,
            team=self.team,
            project=self.project,
            manager=self.manager_profile,
            fact_type="commitment",
            source_key="source:candidate:4",
            proposed_changes={
                "company_name": "BI Group",
                "object_name": "ЖК Grand Park",
                "commitment_text": "Поставка задвижек",
                "amount": "12000000",
            },
        )

        notifications = notify_on_candidate_approved(candidate)
        self.assertEqual(len(notifications), 2)
        notif = notifications[0]
        self.assertEqual(notif.title, "Подтверждено обязательство")
        self.assertEqual(notif.category, "commitment_detected")
        self.assertIn("Контрагент (Bitrix): BI Group", notif.message)
        self.assertIn("Сделка / Объект: ЖК Grand Park", notif.message)
        self.assertIn("Суть: Поставка задвижек", notif.message)
        self.assertIn("Сумма: 12 000 000 ₸", notif.message)

        # Confirm deliveries and outbox events created
        deliveries = NotificationDelivery.objects.filter(notification__in=notifications)
        self.assertEqual(deliveries.count(), 2)
        outbox_events = OutboxEvent.objects.filter(event_type="notification")
        self.assertEqual(outbox_events.count(), 2)

    def test_deduplication(self):
        candidate = FactCandidate.objects.create(
            trace=self.trace,
            team=self.team,
            manager=self.manager_profile,
            fact_type="commitment",
            source_key="source:candidate:5",
            proposed_changes={"commitment_text": "Повторный вызов"},
        )

        notifs_1 = notify_on_new_candidate(candidate)
        self.assertEqual(len(notifs_1), 2)

        # Calling again should not duplicate Notification records
        notifs_2 = notify_on_new_candidate(candidate)
        self.assertEqual(len(notifs_2), 2)
        self.assertEqual(
            Notification.objects.filter(category="commitment_detected").count(), 2
        )
