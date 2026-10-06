from datetime import date
from unittest.mock import patch

from django.test import SimpleTestCase

from e2e.providers import classify


class ProviderFixtureDatesTests(SimpleTestCase):
    def test_payments_use_each_source_day_across_midnight(self):
        value = {
            "target_message_id": 2,
            "content": "БЦ Южный: оплата 20 000 000 ₸ поступила сегодня",
            "sent_at": "2026-10-07T00:01:00+03:00",
            "context": [{
                "raw_message_id": 1,
                "content": "БЦ Север: оплата 50 000 000 ₸ поступила сегодня",
                "timestamp": "2026-10-06T23:59:00+03:00",
            }],
        }
        with patch("e2e.providers.timezone.localdate", return_value=date(2026, 10, 8)):
            facts = classify(value)["facts"]
        self.assertEqual(
            {row["evidence_message_id"]: row["payment_date"] for row in facts},
            {1: "2026-10-06", 2: "2026-10-07"},
        )
