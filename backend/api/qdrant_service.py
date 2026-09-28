import hashlib
import uuid
from django.conf import settings
from qdrant_client import QdrantClient, models as qm
from .models import AISettings, RawMessage
from .ai_service import AIService
from .providers import ProviderUnavailable
from .plain_text import plain_text


class QdrantService:
    @property
    def client(self):
        return QdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY or None,
            timeout=10,
            check_compatibility=False,
        )

    @property
    def collection_name(self):
        cfg = AISettings.get_active()
        version = hashlib.sha256(
            f"{cfg.embedding_model_name}:{cfg.embedding_dimension}".encode()
        ).hexdigest()[:12]
        return f"{settings.QDRANT_COLLECTION}_{version}"

    def ensure_collection(self):
        cfg = AISettings.get_active()
        if not cfg.is_active:
            raise ProviderUnavailable("ai_disabled")
        client = self.client
        name = self.collection_name
        if not client.collection_exists(name):
            try:
                client.create_collection(
                    name,
                    vectors_config=qm.VectorParams(
                        size=cfg.embedding_dimension, distance=qm.Distance.COSINE
                    ),
                )
            except Exception:
                if not client.collection_exists(name):
                    raise
        return True

    def upsert_message(self, message_id, content, payload):
        content = plain_text(content)
        if not content:
            return None
        self.ensure_collection()
        raw_id = payload["raw_message_id"]
        point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mazory:{raw_id}"))
        self.client.upsert(
            self.collection_name,
            points=[
                qm.PointStruct(
                    id=point_id,
                    vector=AIService.get_embedding(content),
                    payload={
                        **payload,
                        "message_id": message_id,
                        "content": content,
                        "sender_name": plain_text(payload.get("sender_name", "")),
                    },
                )
            ],
        )
        return point_id

    def search_similar(
        self,
        query,
        limit=5,
        score_threshold=0.35,
        config_ids=None,
        exclude_id=None,
        before=None,
    ):
        query = plain_text(query)
        if not config_ids or not query:
            return []
        self.ensure_collection()
        must = [
            qm.FieldCondition(key="config_id", match=qm.MatchAny(any=list(config_ids)))
        ]
        if before:
            must.append(
                qm.FieldCondition(
                    key="sent_at_epoch", range=qm.Range(lt=before.timestamp())
                )
            )
        must_not = (
            [
                qm.FieldCondition(
                    key="raw_message_id", match=qm.MatchValue(value=exclude_id)
                )
            ]
            if exclude_id
            else []
        )
        hits = self.client.query_points(
            self.collection_name,
            query=AIService.get_embedding(query),
            query_filter=qm.Filter(must=must, must_not=must_not),
            limit=limit,
            score_threshold=score_threshold,
        ).points
        # Recheck persisted sources so deleted/revoked/stale index entries cannot leak.
        raw_ids = [hit.payload.get("raw_message_id") for hit in hits if hit.payload]
        sources = {
            m.id: m
            for m in RawMessage.objects.filter(id__in=raw_ids, config_id__in=config_ids)
        }
        results = []
        for hit in hits:
            raw = sources.get((hit.payload or {}).get("raw_message_id"))
            if raw:
                content = plain_text(raw.content)
                if not content:
                    continue
                results.append(
                    {
                        "id": raw.id,
                        "message_id": raw.message_id,
                        "content": content,
                        "sender_name": plain_text(raw.sender_name),
                        "sent_at": raw.timestamp.isoformat(),
                        "score": hit.score,
                        "source_url": f"/api/messages/{raw.id}/",
                    }
                )
        return results

    def search(self, query, limit=5, config_ids=None):
        return self.search_similar(
            query, limit=limit, score_threshold=0.2, config_ids=config_ids
        )


qdrant_service = QdrantService()
