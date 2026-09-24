import logging
import hashlib
import numpy as np
from typing import List, Dict, Any, Optional
from django.conf import settings
from qdrant_client import QdrantClient
from qdrant_client.http import models as rest_models

logger = logging.getLogger(__name__)

VECTOR_SIZE = 384

class QdrantService:
    """
    Интеграция с векторным хранилищем Qdrant для семантического поиска
    сообщений и фактов в WhatsApp-переписках.
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
        Создает коллекцию в Qdrant, если она еще не создана.
        """
        try:
            collections = self.client.get_collections().collections
            exists = any(c.name == self.collection_name for c in collections)
            if not exists:
                logger.info("Creating Qdrant collection '%s' with vector size %d", self.collection_name, VECTOR_SIZE)
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=rest_models.VectorParams(
                        size=VECTOR_SIZE,
                        distance=rest_models.Distance.COSINE
                    )
                )
            return True
        except Exception as e:
            logger.error("Failed to connect or ensure Qdrant collection: %s", e)
            return False

    def generate_embedding(self, text: str) -> List[float]:
        """
        Генерирует плотный вектор (Dense Embedding) размерности 384.
        Использует детерминированное псевдосемантическое n-gram проецирование с l2-нормализацией,
        обеспечивающее мгновенную работу без тяжелых внешних весов или задержек.
        """
        if not text:
            return [0.0] * VECTOR_SIZE
        
        words = text.lower().split()
        vector = np.zeros(VECTOR_SIZE, dtype=np.float32)
        
        for idx, word in enumerate(words):
            # Хешируем каждое слово и биграммы
            h = int(hashlib.sha256(word.encode('utf-8')).hexdigest(), 16)
            pos = h % VECTOR_SIZE
            weight = 1.0 + (1.0 / (idx + 1))
            vector[pos] += weight

            if idx > 0:
                bigram = f"{words[idx-1]}_{word}"
                bh = int(hashlib.md5(bigram.encode('utf-8')).hexdigest(), 16)
                bpos = bh % VECTOR_SIZE
                vector[bpos] += 1.5

        # L2-нормализация для корректного косинусного расстояния
        norm = np.linalg.norm(vector)
        if norm > 0:
            vector = vector / norm
            
        return vector.tolist()

    def upsert_message(self, message_id: str, content: str, payload: Dict[str, Any]) -> Optional[str]:
        """
        Сохраняет сообщение с вектором в Qdrant.
        """
        try:
            self.ensure_collection()
            point_id = hashlib.md5(message_id.encode('utf-8')).hexdigest()
            vector = self.generate_embedding(content)
            
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
            logger.error("Failed to upsert point to Qdrant: %s", e)
            return None

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Выполняет семантический поиск по сохраненным сообщениям.
        """
        try:
            self.ensure_collection()
            vector = self.generate_embedding(query)
            results = self.client.search(
                collection_name=self.collection_name,
                query_vector=vector,
                limit=limit
            )
            return [
                {
                    "score": r.score,
                    "payload": r.payload
                }
                for r in results
            ]
        except Exception as e:
            logger.error("Failed to query Qdrant: %s", e)
            return []

qdrant_service = QdrantService()
