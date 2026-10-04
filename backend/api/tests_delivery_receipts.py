from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import Notification, NotificationDelivery, OutboxEvent
from .providers import ProviderUnavailable
from .tasks import apply_delivery_ack, run_outbox


class DeliveryReceiptTests(TestCase):
    def setUp(self):
        self.payload = {"session": "default", "message_id": "receipt-id", "ack": 2}
        self.notification = Notification.objects.create(
            recipient=User.objects.create_user(username="recipient"),
            deduplication_key="receipt-test", title="Reminder", message="Message",
        )

    def delivery(self, **values):
        return NotificationDelivery.objects.create(notification=self.notification, **values)

    def test_unrelated_receipt_completes_without_error(self):
        event = OutboxEvent.objects.create(
            event_type="delivery_ack", deduplication_key="unrelated", payload=self.payload,
        )
        run_outbox(event.id)
        event.refresh_from_db()
        self.assertEqual(event.state, "done")
        self.assertEqual(event.error_code, "")
        self.assertEqual(event.payload["receipt_outcome"], "unrelated")
        self.assertFalse(NotificationDelivery.objects.exists())

    def test_early_receipt_retries_then_matches_real_send(self):
        delivery = self.delivery(state="sending")
        with self.assertRaisesMessage(ProviderUnavailable, "delivery_receipt_waiting_for_send"):
            apply_delivery_ack(self.payload)
        delivery.provider_message_id, delivery.state = "receipt-id", "sent"
        delivery.save()
        self.assertEqual(apply_delivery_ack(self.payload), "matched")
        delivery.refresh_from_db()
        self.assertEqual(delivery.state, "delivered")

    def test_stale_send_does_not_make_unrelated_receipt_fail(self):
        delivery = self.delivery(state="sending")
        NotificationDelivery.objects.filter(pk=delivery.id).update(
            updated_at=timezone.now() - timedelta(minutes=11),
        )
        self.assertEqual(apply_delivery_ack(self.payload), "unrelated")
        delivery.refresh_from_db()
        self.assertEqual(delivery.state, "sending")

    def test_negative_and_duplicate_receipts_preserve_correct_state(self):
        delivery = self.delivery(state="sent", provider_message_id="receipt-id")
        apply_delivery_ack({**self.payload, "ack": -1})
        delivery.refresh_from_db()
        self.assertEqual(delivery.state, "failed")
        apply_delivery_ack(self.payload)
        delivery.refresh_from_db()
        self.assertEqual(delivery.state, "failed")

    def test_other_session_and_non_delivery_ack_are_ignored(self):
        self.assertEqual(apply_delivery_ack({**self.payload, "session": "other"}), "ignored")
        self.assertEqual(apply_delivery_ack({**self.payload, "ack": 1}), "ignored")
