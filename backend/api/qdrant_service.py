import logging
import hashlib
from typing import List, Dict, Any, Optional
from django.conf import settings
from qdrant_client import QdrantClient
from qdrant_client.http import models as rest_models
from api.models import AISettings
from api.ai_service import AIService

logger = logging.getLogger(__name__)

class QdrantService:
    """
    Интеграция с векторным хранилищем Qdrant для семантического поиска
    сообщений и фактов в WhatsApp-переписках с использованием модели эмбеддингов.
    """
    def __init__(self):
        self.url = getattr(settings, 'QDRANT_URL', 'http://qdrant:6333')
        self.collection_name = getattr(settings, 'QDRANT_COLLECTION', 'mazory_messages')
        self._client: Optional[QdrantClient] = None

    @property
    def client(self) -> QdrantClient:
        if self._client is None:
            self._client = QdrantClient(url=self.url, timeout=10.0, check_compatibility=False)
        return self._client

    def ensure_collection(self) -> bool:
        """
        Создает коллекцию в Qdrant с размерностью из AISettings (по умолчанию 1024).
        Если коллекция уже существует со старой размерностью, пересоздает ее.
        """
        try:
            cfg = AISettings.get_active()
            vector_size = cfg.embedding_dimension or 1024
            
            collections = self.client.get_collections().collections
            exists = any(c.name == self.collection_name for c in collections)
            if exists:
                try:
                    info = self.client.get_collection(self.collection_name)
                    curr_size = None
                    if hasattr(info.config.params.vectors, 'size'):
                        curr_size = info.config.params.vectors.size
                    elif isinstance(info.config.params.vectors, dict) and 'size' in info.config.params.vectors:
                        curr_size = info.config.params.vectors['size']
                    if curr_size and curr_size != vector_size:
                        logger.warning("Recreating collection '%s' with dimension %d (was %d)", self.collection_name, vector_size, curr_size)
                        self.client.delete_collection(self.collection_name)
                        exists = False
                except Exception as ex:
                    logger.warning("Could not inspect collection %s: %s", self.collection_name, ex)

            if not exists:
                logger.info("Creating Qdrant collection '%s' with vector size %d", self.collection_name, vector_size)
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=rest_models.VectorParams(
                        size=vector_size,
                        distance=rest_models.Distance.COSINE
                    )
                )
            return True
        except Exception as e:
            logger.error("Failed to connect or ensure Qdrant collection: %s", e)
            return False

    def upsert_message(self, message_id: str, content: str, payload: Dict[str, Any]) -> Optional[str]:
        """
        Генерирует эмбеддинг через AI сервис и сохраняет сообщение в Qdrant.
        """
        try:
            self.ensure_collection()
            point_id = hashlib.md5(message_id.encode('utf-8')).hexdigest()
            vector = AIService.get_embedding(content)
            
            point = rest_models.PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "message_id": message_id,
                    "content": content,
                    **payload
                }
            )
            self.client.upsert(
                collection_name=self.collection_name,
                points=[point]
            )
            return point_id
        except Exception as e:
            logger.error("Failed to upsert message into Qdrant: %s", e)
            return None

    def search_similar(self, query: str, limit: int = 5, score_threshold: float = 0.35) -> List[Dict[str, Any]]:
        """
        Семантический поиск похожих сообщений для формирования контекста RAG.
        """
        try:
            self.ensure_collection()
            vector = AIService.get_embedding(query)
            
            if hasattr(self.client, 'query_points'):
                res = self.client.query_points(
                    collection_name=self.collection_name,
                    query=vector,
                    limit=limit,
                    score_threshold=score_threshold
                )
                points = res.points if hasattr(res, 'points') else res
            elif hasattr(self.client, 'search'):
                points = self.client.search(
                    collection_name=self.collection_name,
                    query_vector=vector,
                    limit=limit,
                    score_threshold=score_threshold
                )
            else:
                points = []

            return [
                {
                    "score": getattr(hit, 'score', 0.0),
                    "id": getattr(hit, 'id', ''),
                    **(getattr(hit, 'payload', {}) or {})
                }
                for hit in points
            ]
        except Exception as e:
            logger.error("Failed to search Qdrant for '%s': %s", query, e)
            return []

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Удобный алиас для поиска по сообщениям"""
        return self.search_similar(query, limit=limit, score_threshold=0.2)

qdrant_service = QdrantService()
