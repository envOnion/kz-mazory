from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from .ai_service import AIService, embedding_chunks
from .providers import ProviderUnavailable


class EmbeddingChunkTests(SimpleTestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(embedding_provider_url="https://provider.test/v1",
            embedding_model_name="embedding-model", embedding_dimension=2)

    def embed(self, text, response):
        with patch.object(AIService, "_config", return_value=self.cfg), \
             patch.object(AIService, "_credential", return_value="test-key"), \
             patch.object(AIService, "_post", side_effect=response) as post:
            return AIService.get_embedding(text), post

    def test_unicode_chunks_keep_entire_long_message(self):
        text = "Объект 🏗️, срок и платёж. " * 2000
        chunks = embedding_chunks(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(0 < len(c.encode("utf-8")) <= 480 for c in chunks))

    def test_short_message_preserves_scalar_request_and_vector(self):
        def response(url, payload, timeout, **kwargs):
            self.assertEqual(payload["input"], "Короткое сообщение")
            return {"data": [{"embedding": [0.123456789, 0.9]}]}
        vector, post = self.embed("Короткое сообщение", response)
        self.assertEqual(vector, [0.123456789, 0.9])
        self.assertEqual(post.call_count, 1)

    def test_complete_tail_multiple_batches_and_response_order(self):
        text = "a" * (480 * 33) + "tail"
        received = []
        def response(url, payload, timeout, **kwargs):
            batch = payload["input"]
            self.assertIsInstance(batch, list)
            self.assertLessEqual(len(batch), 32)
            received.extend(batch)
            return {"data": [{"index": i, "embedding": [1, 2]}
                for i in reversed(range(len(batch)))]}
        vector, post = self.embed(text, response)
        self.assertEqual("".join(received), text)
        self.assertEqual(vector, [1, 2])
        self.assertEqual(post.call_count, 2)

    def test_weighted_pooling_uses_all_fragments(self):
        def response(url, payload, timeout, **kwargs):
            return {"data": [{"index": 1, "embedding": [4, 8]},
                             {"index": 0, "embedding": [1, 2]}]}
        vector, _ = self.embed("a" * 480 + "b" * 240, response)
        self.assertEqual(vector, [2, 4])

    def test_incomplete_duplicate_nonfinite_and_wrong_dimension_fail(self):
        bad = [[], [{"index": 0, "embedding": [1, 2]}],
            [{"index": 0, "embedding": [1, 2]}] * 2,
            [{"index": 0, "embedding": [float("nan"), 2]}, {"index": 1, "embedding": [1, 2]}],
            [{"index": 0, "embedding": [1]}, {"index": 1, "embedding": [1, 2]}]]
        for rows in bad:
            with self.subTest(rows=rows), self.assertRaisesMessage(ProviderUnavailable, "invalid_embedding"):
                self.embed("a" * 600, lambda *args, **kwargs: {"data": rows})
