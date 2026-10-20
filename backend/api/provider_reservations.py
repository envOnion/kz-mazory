"""Atomic autonomous request/token quota and bounded provider concurrency."""

import json
import math
from datetime import timedelta
from decimal import Decimal
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from .models import AISettings, OutboxEvent, ProviderReservation, ProviderUsage
from .providers import ProviderUnavailable


def reserve(cfg, payload, operation, *, lease_seconds=600):
    from .ai_service import usage_event_id
    event = OutboxEvent.objects.filter(pk=usage_event_id.get()).first() if usage_event_id.get() else None
    purpose = "history" if event and event.payload.get("priority") == "history" else "live"
    # UTF-8 bytes provide a conservative reservation rather than the unsafe
    # characters/4 heuristic. Native token counts remain in ProviderUsage.
    tokens = len(json.dumps(payload, ensure_ascii=False).encode()) + (0 if operation in ("embedding", "model_metadata", "token_count") else cfg.max_completion_tokens)
    with transaction.atomic():
        policy = AISettings.objects.select_for_update().filter(is_primary=True, is_active=True).first()
        locked = policy if policy and policy.pk == cfg.pk else AISettings.objects.select_for_update().get(pk=cfg.pk)
        policy = policy or locked
        if not locked.is_active:
            raise ProviderUnavailable("provider_config_inactive")
        # Take the clock after the lock: another worker may have held it across
        # a minute/day boundary. Reservations count request starts, not finishes.
        now = timezone.now()
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if locked.minute_request_limit:
            recent = ProviderReservation.objects.filter(
                config=locked, created_at__gt=now - timedelta(seconds=60),
            ).order_by("created_at", "pk")
            count = recent.count()
            if count >= locked.minute_request_limit:
                first_available = recent[count - locked.minute_request_limit].created_at + timedelta(seconds=60)
                raise ProviderUnavailable("ai_minute_limit_exhausted", retry_after=max(1, math.ceil((first_available - now).total_seconds())))
        reservations = ProviderReservation.objects.filter(config=locked, created_at__gte=day)
        global_reservations = ProviderReservation.objects.filter(created_at__gte=day)
        active = global_reservations.filter(state="reserved", lease_until__gt=now)
        limit = max(1, policy.autonomous_max_in_flight)
        if policy.autonomous_enabled and (active.count() >= limit or (operation == "extraction" and active.filter(operation="extraction").exists())):
            raise ProviderUnavailable("provider_in_flight_budget", retry_after=10,
                                      diagnostics={"request_admission_denied": True, "budget_scope": "autonomous_shared"})
        global_usage = ProviderUsage.objects.filter(created_at__gte=day)
        usage = global_usage.filter(Q(config=locked) | Q(config__isnull=True))
        failures = list(global_usage.filter(config=locked, operation=operation).order_by("-id").values("error_code", "created_at")[:3])
        if len(failures) == 3 and all(row["error_code"] in ("provider_server_error", "provider_overloaded", "provider_timeout", "provider_connection_failed") for row in failures) and failures[0]["created_at"] > now - timedelta(seconds=60):
            raise ProviderUnavailable("provider_circuit_open", retry_after=60,
                                      diagnostics={"request_admission_denied": True})
        unsettled = reservations.filter(usage__isnull=True)
        if locked.daily_request_limit and usage.count() + unsettled.count() >= locked.daily_request_limit:
            raise ProviderUnavailable("ai_daily_budget_exhausted")
        known = usage.aggregate(cost=Sum("cost_usd"), input=Sum("input_tokens"), output=Sum("output_tokens"))
        if locked.daily_budget_usd and (known["cost"] or Decimal(0)) >= locked.daily_budget_usd:
            raise ProviderUnavailable("ai_daily_budget_exhausted")
        unknown_usage = global_reservations.filter(Q(usage__isnull=True) | Q(usage__input_tokens__isnull=True) | Q(usage__output_tokens__isnull=True))
        global_known = global_usage.aggregate(input=Sum("input_tokens"), output=Sum("output_tokens"))
        used_tokens = (global_known["input"] or 0) + (global_known["output"] or 0) + (unknown_usage.aggregate(total=Sum("reserved_tokens"))["total"] or 0)
        if policy.autonomous_enabled and policy.autonomous_daily_token_limit and used_tokens + tokens > policy.autonomous_daily_token_limit:
            raise ProviderUnavailable("ai_daily_budget_exhausted", diagnostics={
                "request_admission_denied": True, "budget_scope": "autonomous_shared"})
        if policy.autonomous_enabled and purpose == "history" and policy.autonomous_daily_token_limit:
            history_tokens = global_reservations.filter(purpose="history").aggregate(total=Sum("reserved_tokens"))["total"] or 0
            if history_tokens + tokens > policy.autonomous_daily_token_limit // 5:
                raise ProviderUnavailable("history_budget_reserved", retry_after=60, diagnostics={
                    "request_admission_denied": True, "budget_scope": "autonomous_shared"})
        return ProviderReservation.objects.create(
            config=locked,
            operation=operation,
            purpose=purpose,
            reserved_tokens=tokens,
            lease_until=now + timedelta(seconds=lease_seconds),
        )
