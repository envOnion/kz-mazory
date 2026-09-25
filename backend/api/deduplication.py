import re
import logging
from contextlib import contextmanager
from django.conf import settings
import redis

logger = logging.getLogger(__name__)

def get_redis_client():
    host = getattr(settings, 'REDIS_HOST', '127.0.0.1')
    port = getattr(settings, 'REDIS_PORT', 6379)
    return redis.Redis(host=host, port=port, db=0, socket_timeout=5)

def normalize_deal_name(raw_name: str) -> str:
    """
    Нормализует название сделки/объекта для строгой дедупликации:
    - Удаляет кавычки, спецсимволы, пунктуацию
    - Удаляет префиксы 'жк', 'бц', 'тоо', 'ао', 'мжд', 'объект', 'мкр'
    - Приводит к нижнему регистру и удаляет лишние пробелы
    Пример: 'ЖК «Медео»' -> 'медео', 'ЖК Медео' -> 'медео'
    """
    if not raw_name:
        return ""
    text = raw_name.lower().strip()
    # Удаление кавычек и скобок
    text = re.sub(r'[«»""\'\(\)\[\]\{\}]', ' ', text)
    # Удаление префиксов
    prefixes = [r'\bжк\b', r'\bбц\b', r'\bтоо\b', r'\bао\b', r'\bмжд\b', r'\bобъект\b', r'\bмкр\b', r'\bмикрорайон\b']
    for p in prefixes:
        text = re.sub(p, ' ', text)
    # Удаление спецсимволов кроме дефиса
    text = re.sub(r'[^a-zа-яё0-9\-]', ' ', text)
    # Схлопывание пробелов
    text = re.sub(r'\s+', ' ', text).strip()
    return text

@contextmanager
def get_deal_lock(normalized_name: str, timeout: int = 30):
    """
    Распределенная блокировка в Redis для предотвращения race conditions
    при одновременной обработке сообщений по одному объекту.
    """
    if not normalized_name:
        yield True
        return

    lock = None
    acquired = False
    try:
        client = get_redis_client()
        lock_key = f"mazory:lock:deal:{normalized_name}"
        lock = client.lock(lock_key, timeout=timeout, blocking_timeout=15)
        acquired = lock.acquire()
    except Exception as e:
        logger.warning("Redis deal lock fallback for %s: %s", normalized_name, e)
        acquired = False

    try:
        yield acquired
    finally:
        if acquired and lock is not None:
            try:
                lock.release()
            except Exception:
                pass

