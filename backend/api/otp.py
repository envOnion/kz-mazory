"""Atomic OTP consumption and rolling abuse counters. No plaintext OTP in task payloads."""

import hashlib
import hmac
import secrets
import threading
import uuid
from django.conf import settings
from django.core.cache import cache
from cryptography.fernet import Fernet
import base64
import redis

OTP_TTL = 300
COOLDOWN_TTL = 45
MAX_ATTEMPTS = 5
_test_lock = threading.Lock()


def connection():
    return redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=1,
        socket_connect_timeout=2,
        socket_timeout=2,
        decode_responses=True,
    )


def digest(phone, code):
    return hmac.new(
        settings.SECRET_KEY.encode(), f"{phone}:{code}".encode(), hashlib.sha256
    ).hexdigest()


def cipher():
    return Fernet(
        base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    )


ISSUE_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
for i=3,5 do
    local v=tonumber(redis.call('GET',KEYS[i]) or '0')
    if v >= tonumber(ARGV[i]) then return 0 end
end
for i=3,5 do
    local n=redis.call('INCR',KEYS[i])
    if n==1 then redis.call('EXPIRE',KEYS[i],i==4 and 86400 or 3600) end
end
redis.call('SET',KEYS[1],'1','EX',45)
redis.call('HSET',KEYS[2],'digest',ARGV[1],'attempts',0)
redis.call('EXPIRE',KEYS[2],300)
redis.call('SET',KEYS[6],ARGV[2],'EX',300)
return 1
"""

VERIFY_SCRIPT = """
local saved=redis.call('HGET',KEYS[1],'digest')
if not saved then return 0 end
local n=redis.call('HINCRBY',KEYS[1],'attempts',1)
if n>5 then redis.call('DEL',KEYS[1]); return 0 end
if saved==ARGV[1] then redis.call('DEL',KEYS[1]); return 1 end
if n==5 then redis.call('DEL',KEYS[1]) end
return 0
"""


def issue(phone, ip):
    code = f"{secrets.randbelow(9000) + 1000}"
    delivery_id = uuid.uuid4().hex
    encrypted = cipher().encrypt(code.encode()).decode()
    hashed = digest(phone, code)
    ip_key = hashlib.sha256(ip.encode()).hexdigest()
    keys = [
        f"otp:cool:{phone}",
        f"otp:challenge:{phone}",
        f"otp:hour:{phone}",
        f"otp:day:{phone}",
        f"otp:ip:{ip_key}",
        f"otp:delivery:{delivery_id}",
    ]
    limits = [
        settings.OTP_PHONE_HOUR_LIMIT,
        settings.OTP_PHONE_DAY_LIMIT,
        settings.OTP_IP_HOUR_LIMIT,
    ]
    if settings.TESTING and settings.CACHES["default"]["BACKEND"].endswith(
        "LocMemCache"
    ):
        with _test_lock:
            if cache.get(keys[0]) or any(
                cache.get(key, 0) >= limit for key, limit in zip(keys[2:5], limits)
            ):
                return None
            for key, ttl in zip(keys[2:5], [3600, 86400, 3600]):
                if cache.add(key, 1, ttl) is False:
                    cache.incr(key)
            cache.set(keys[0], True, COOLDOWN_TTL)
            cache.set(keys[1], {"digest": hashed, "attempts": 0}, OTP_TTL)
            cache.set(keys[5], encrypted, OTP_TTL)
    else:
        if not connection().eval(ISSUE_SCRIPT, 6, *keys, hashed, encrypted, *limits):
            return None
    return delivery_id


def verify(phone, code):
    key = f"otp:challenge:{phone}"
    hashed = digest(phone, code)
    if settings.TESTING and settings.CACHES["default"]["BACKEND"].endswith(
        "LocMemCache"
    ):
        with _test_lock:
            data = cache.get(key)
            if not data:
                return False
            valid = data["attempts"] < MAX_ATTEMPTS and hmac.compare_digest(
                data["digest"], hashed
            )
            data["attempts"] += 1
            if valid or data["attempts"] >= MAX_ATTEMPTS:
                cache.delete(key)
            else:
                cache.set(key, data, OTP_TTL)
            return valid
    return bool(connection().eval(VERIFY_SCRIPT, 1, key, hashed))


def delivery_code(delivery_id):
    key = f"otp:delivery:{delivery_id}"
    if settings.TESTING and settings.CACHES["default"]["BACKEND"].endswith(
        "LocMemCache"
    ):
        encoded = cache.get(key)
    else:
        encoded = connection().get(key)
    return cipher().decrypt(encoded.encode()).decode() if encoded else None
