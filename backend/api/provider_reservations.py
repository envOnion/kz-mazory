"""Atomic autonomous request/token quota and bounded provider concurrency."""

import json
from datetime import timedelta
from decimal import Decimal
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from .models import AISettings, OutboxEvent, ProviderReservation, ProviderUsage
from .providers import ProviderUnavailable


def reserve(cfg, payload, operation):
    if not cfg.autonomous_enabled:
        return None
    from .ai_service import usage_event_id
    event = OutboxEvent.objects.filter(pk=usage_event_id.get()).first() if usage_event_id.get() else None
    purpose = "history" if event and event.payload.get("priority") == "history" else "live"
    now = timezone.now()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # UTF-8 bytes provide a conservative reservation rather than the unsafe
    # characters/4 heuristic. Native token counts remain in ProviderUsage.
    tokens = len(json.dumps(payload, ensure_ascii=False).encode()) + (0 if operation in ("embedding", "model_metadata", "token_count") else cfg.max_completion_tokens)
    with transaction.atomic():
        locked = AISettings.objects.select_for_update().get(pk=cfg.pk)
        reservations = ProviderReservation.objects.filter(config=locked, created_at__gte=day)
        active = reservations.filter(state="reserved", lease_until__gt=now)
        limit = max(1, locked.autonomous_max_in_flight)
        if active.count() >= limit or (operation == "extraction" and active.filter(operation="extraction").exists()):
            raise ProviderUnavailable("provider_in_flight_budget", retry_after=10)
        usage = ProviderUsage.objects.filter(created_at__gte=day)
        failures = list(usage.filter(operation=operation).order_by("-id").values("error_code", "created_at")[:3])
        if len(failures) == 3 and all(row["error_code"] in ("provider_server_error", "provider_overloaded", "provider_timeout", "provider_connection_failed") for row in failures) and failures[0]["created_at"] > now - timedelta(seconds=60):
            raise ProviderUnavailable("provider_circuit_open", retry_after=60)
        unsettled = reservations.filter(usage__isnull=True)
        if locked.daily_request_limit and usage.count() + unsettled.count() >= locked.daily_request_limit:
            raise ProviderUnavailable("ai_daily_budget_exhausted")
        known = usage.aggregate(cost=Sum("cost_usd"), input=Sum("input_tokens"), output=Sum("output_tokens"))
        if locked.daily_budget_usd and (known["cost"] or Decimal(0)) >= locked.daily_budget_usd:
            raise ProviderUnavailable("ai_daily_budget_exhausted")
        unknown_usage = reservations.filter(Q(usage__isnull=True) | Q(usage__input_tokens__isnull=True) | Q(usage__output_tokens__isnull=True))
        used_tokens = (known["input"] or 0) + (known["output"] or 0) + (unknown_usage.aggregate(total=Sum("reserved_tokens"))["total"] or 0)
        if locked.autonomous_daily_token_limit and used_tokens + tokens > locked.autonomous_daily_token_limit:
            raise ProviderUnavailable("ai_daily_budget_exhausted")
        if purpose == "history" and locked.autonomous_daily_token_limit:
            history_tokens = reservations.filter(purpose="history").aggregate(total=Sum("reserved_tokens"))["total"] or 0
            if history_tokens + tokens > locked.autonomous_daily_token_limit // 5:
                raise ProviderUnavailable("history_budget_reserved", retry_after=60)
        return ProviderReservation.objects.create(
            config=locked,
            operation=operation,
            purpose=purpose,
            reserved_tokens=tokens,
            lease_until=now + timedelta(minutes=10),
        )
