from django.http import JsonResponse
from django.db import connection
from django.core.cache import cache


def health(request, mode):
    if mode == "live":
        return JsonResponse({"status": "alive"})
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        cache.set("health:ready", True, 10)
        if cache.get("health:ready") is not True:
            raise RuntimeError()
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ready"})
