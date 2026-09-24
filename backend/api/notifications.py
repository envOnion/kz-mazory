import json
import time
import uuid
from django.core.cache import cache

NOTIFICATION_TTL = 86400 * 14  # 14 days

def format_time_ago(created_at: float) -> str:
    """Return human-readable relative time string in Russian."""
    if not created_at:
        return "Только что"
    diff = int(time.time() - created_at)
    if diff < 60:
        return "Только что"
    elif diff < 3600:
        mins = diff // 60
        return f"{mins} мин назад"
    elif diff < 86400:
        hours = diff // 3600
        return f"{hours} ч назад"
    else:
        days = diff // 86400
        return f"{days} д назад"

def get_redis_key(phone: str) -> str:
    clean = phone.replace('+', '').replace(' ', '').replace('(', '').replace(')', '').replace('-', '')
    if not clean:
        clean = "unknown"
    return f"mazory_notifications:{clean}"

def fetch_user_notifications(phone: str):
    """
    Fetches real targeted notifications for a specific user phone from Redis.
    Strictly NO mock data. If no notifications exist, returns empty list.
    """
    key = get_redis_key(phone)
    raw = cache.get(key)
    
    if raw is None:
        notifications = []
    else:
        try:
            notifications = json.loads(raw)
            if not isinstance(notifications, list):
                notifications = []
        except Exception:
            notifications = []

    # Update dynamic relative time for items
    for item in notifications:
        item["time"] = format_time_ago(item.get("created_at", time.time()))

    unread_count = sum(1 for n in notifications if not n.get("is_read", False))
    return notifications, unread_count

def mark_all_notifications_as_read(phone: str):
    key = get_redis_key(phone)
    notifications, _ = fetch_user_notifications(phone)
    for n in notifications:
        n["is_read"] = True
    cache.set(key, json.dumps(notifications), timeout=NOTIFICATION_TTL)
    return notifications

def push_notification_to_redis(phone: str, title: str, message: str, notif_type: str = "info"):
    """
    Saves a real targeted business notification for a specific user phone into Redis.
    """
    key = get_redis_key(phone)
    notifications, _ = fetch_user_notifications(phone)
    
    now = time.time()
    new_item = {
        "id": f"notif-{uuid.uuid4().hex[:8]}",
        "title": title,
        "message": message,
        "type": notif_type,
        "time": "Только что",
        "is_read": False,
        "created_at": now
    }
    
    notifications.insert(0, new_item)
    # keep max 50 items per user
    notifications = notifications[:50]
    cache.set(key, json.dumps(notifications), timeout=NOTIFICATION_TTL)
    unread_count = sum(1 for n in notifications if not n.get("is_read", False))
    return new_item, unread_count

