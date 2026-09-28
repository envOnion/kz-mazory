import hashlib, hmac, json, uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from django.db import connection, close_old_connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from api.testing.factories import setup_case, candidate_for
from api.models import RawMessage, OutboxEvent, FinancialRecord, User
from api.facts import review
from api.security import Conflict
from api import otp


@skipUnless(
    connection.vendor == "postgresql",
    "Row locking requires PostgreSQL; also run this suite in the isolated Compose stack.",
)
class ConcurrentEffectsTests(TransactionTestCase):
    def setUp(self):
        setup_case(self)

    @override_settings(WAHA_WEBHOOK_SECRET="parallel-secret")
    def test_ten_parallel_webhooks_create_one_durable_event(self):
        body = json.dumps(
            {
                "event": "message",
                "session": "default",
                "payload": {
                    "id": "parallel",
                    "from": "test@g.us",
                    "body": "message",
                    "timestamp": int(timezone.now().timestamp()),
                },
            }
        ).encode()
        signature = hmac.new(b"parallel-secret", body, hashlib.sha512).hexdigest()

        def call(_):
            close_old_connections()
            try:
                return (
                    APIClient()
                    .post(
                        "/api/messages/ingest/",
                        body,
                        content_type="application/json",
                        HTTP_X_WEBHOOK_HMAC=signature,
                    )
                    .status_code
                )
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=10) as pool:
            codes = list(pool.map(call, range(10)))
        self.assertEqual(codes.count(202), 1)
        self.assertEqual(codes.count(200), 9)
        self.assertEqual(RawMessage.objects.count(), 1)
        self.assertEqual(OutboxEvent.objects.count(), 1)

    def test_parallel_review_preserves_one_version_and_conflicts_other(self):
        first = candidate_for(self, key="a")
        second = candidate_for(self, key="b")

        def approve(pk):
            close_old_connections()
            try:
                try:
                    review(
                        pk,
                        User.objects.get(pk=self.finance.id),
                        "approve",
                        base_version=1,
                    )
                    return "approved"
                except Conflict:
                    return "conflict"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(approve, [first.id, second.id]))
        self.assertCountEqual(results, ["approved", "conflict"])
        self.assertEqual(FinancialRecord.objects.count(), 1)

    def test_redis_consumes_otp_once_under_parallel_verification(self):
        from django.conf import settings

        phone = "779" + str(uuid.uuid4().int)[:8]
        redis_cache = {
            "default": {
                "BACKEND": "django.core.cache.backends.redis.RedisCache",
                "LOCATION": f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/1",
            }
        }
        with override_settings(CACHES=redis_cache):
            delivery = otp.issue(phone, "parallel-test")
            code = otp.delivery_code(delivery)
            with ThreadPoolExecutor(max_workers=10) as pool:
                results = list(pool.map(lambda _: otp.verify(phone, code), range(10)))
            self.assertEqual(results.count(True), 1)
