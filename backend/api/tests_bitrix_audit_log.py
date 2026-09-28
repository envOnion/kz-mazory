from datetime import timedelta, datetime, timezone as dtz
from unittest.mock import patch
from django.test import TestCase
from django.core.cache import cache
from django.utils import timezone
from api.testing.factories import setup_case, client_for
from api.models import (
    Commitment,
    Notification,
    NotificationDelivery,
    OutboxEvent,
    ReminderOccurrence,
    UserProfile,
)
from api.notifications import (
    create_notification,
    plan_reminders,
    change_commitment,
    next_delivery_time,
)
from api.tasks import run_outbox, dispatch_outbox, deliver_notification
import requests


class DurableDeliveryTests(TestCase):
    def setUp(self):
        setup_case(self)

    def test_notification_survives_cache_clear_and_is_private(self):
        n = create_notification(
            self.manager, "Title", "Message", "info", "key", project=self.project
        )
        create_notification(
            self.manager, "Title", "Message", "info", "key", project=self.project
        )
        cache.clear()
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(
            client_for(self.manager).get("/api/notifications/").json()["count"], 1
        )
        self.assertEqual(
            client_for(self.other).get("/api/notifications/").json()["count"], 0
        )

    @patch(
        "api.tasks.send_waha_whatsapp_message_task", return_value={"id": "provider-1"}
    )
    def test_sent_is_not_delivered_and_repeated_task_is_noop(self, send):
        n = create_notification(
            self.manager,
            "Title",
            "Message",
            "info",
            "send",
            project=self.project,
            whatsapp=True,
        )
        event = OutboxEvent.objects.get(event_type="notification")
        run_outbox(event.id)
        run_outbox(event.id)
        send.assert_called_once()
        self.assertEqual(NotificationDelivery.objects.get(notification=n).state, "sent")

    @patch("api.tasks.send_waha_whatsapp_message_task", side_effect=requests.Timeout())
    def test_unknown_outcome_is_not_retried(self, send):
        n = create_notification(
            self.manager,
            "Title",
            "Message",
            "info",
            "unknown",
            project=self.project,
            whatsapp=True,
        )
        event = OutboxEvent.objects.get(event_type="notification")
        run_outbox(event.id)
        run_outbox(event.id)
        send.assert_called_once()
        event.refresh_from_db()
        self.assertEqual(event.state, "unknown")
        self.assertEqual(n.deliveries.get().state, "unknown")

    def test_expired_delivery_lease_is_unknown(self):
        n = create_notification(
            self.manager,
            "Title",
            "Message",
            "info",
            "lease",
            project=self.project,
            whatsapp=True,
        )
        event = OutboxEvent.objects.get(event_type="notification")
        event.state = "processing"
        event.lease_until = timezone.now() - timedelta(seconds=1)
        event.save()
        n.deliveries.update(state="sending")
        dispatch_outbox()
        event.refresh_from_db()
        self.assertEqual(event.state, "unknown")
        self.assertEqual(n.deliveries.get().state, "unknown")

    @patch("api.tasks.send_waha_whatsapp_message_task")
    def test_fulfilled_commitment_cancels_queued_reminder(self, send):
        c = Commitment.objects.create(
            project=self.project,
            manager=self.manager.profile,
            commitment_text="Call",
            deadline_at=timezone.now() + timedelta(minutes=10),
            is_verified=True,
        )
        plan_reminders()
        plan_reminders()
        self.assertEqual(ReminderOccurrence.objects.count(), 1)
        change_commitment(self.manager, c.id, 1, "fulfill")
        run_outbox(OutboxEvent.objects.get(event_type="notification").id)
        send.assert_not_called()
        self.assertEqual(NotificationDelivery.objects.get().state, "cancelled")

    def test_quiet_hours_and_disabled_reminders(self):
        profile = self.manager.profile
        profile.timezone = "Asia/Almaty"
        profile.notification_preferences = {
            "quiet_start": "21:00",
            "quiet_end": "09:00",
            "reminder": False,
        }
        profile.save()
        # Almaty is UTC+5: 17 UTC is 22 local, next morning is 04 UTC.
        result = next_delivery_time(
            self.manager, datetime(2026, 9, 1, 17, tzinfo=dtz.utc)
        )
        self.assertEqual(
            result.astimezone(dtz.utc), datetime(2026, 9, 2, 4, tzinfo=dtz.utc)
        )
        Commitment.objects.create(
            project=self.project,
            manager=profile,
            commitment_text="Call",
            deadline_at=timezone.now() + timedelta(minutes=10),
            is_verified=True,
        )
        plan_reminders()
        self.assertFalse(Notification.objects.exists())

    def test_read_ack_does_not_fulfill_commitment(self):
        c = Commitment.objects.create(
            project=self.project,
            manager=self.manager.profile,
            commitment_text="Call",
            is_verified=True,
        )
        n = create_notification(
            self.manager,
            "Title",
            "Message",
            "reminder",
            "ack",
            project=self.project,
            commitment=c,
        )
        self.assertEqual(
            client_for(self.manager)
            .post(f"/api/notifications/{n.id}/ack/")
            .status_code,
            200,
        )
        c.refresh_from_db()
        self.assertEqual(c.status, "pending")
