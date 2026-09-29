"""The complete source history, bounded only by the native model token budget."""

from django.db.models import Exists, OuterRef, Q

from .context_tokens import (
    canonical_json,
    context_runtime,
    extraction_payload,
    payload_hash,
)
from .models import RawMessage
from .plain_text import plain_text
from .providers import ProviderUnavailable

POLICY = "chat-history-256k-v1"


def source_scope(raw):
    team = raw.config.team if raw.config_id else raw.team
    if not team or not team.is_active or (raw.team_id and raw.team_id != team.id):
        raise ProviderUnavailable("context_source_unavailable")
    if raw.config_id:
        if not raw.config.is_active:
            raise ProviderUnavailable("context_source_unavailable")
        return RawMessage.objects.filter(
            config_id=raw.config_id,
            source=raw.source,
            session_name=raw.session_name,
            chat_id=raw.chat_id,
        ).filter(Q(team_id=team.id) | Q(team_id__isnull=True))
    if not raw.project_id or raw.project.team_id != team.id or raw.project.archived:
        raise ProviderUnavailable("context_source_unavailable")
    return RawMessage.objects.filter(
        team_id=team.id,
        source=raw.source,
        project_id=raw.project_id,
        config__isnull=True,
    )


def history_queryset(raw, snapshot_id):
    scope = source_scope(raw).filter(id__lte=snapshot_id)
    # Resolve revisions before applying time filters, so an old revision cannot reappear.
    newer = scope.filter(
        source=OuterRef("source"),
        session_name=OuterRef("session_name"),
        message_id=OuterRef("message_id"),
    ).filter(
        Q(received_at__gt=OuterRef("received_at"))
        | Q(received_at=OuterRef("received_at"), id__gt=OuterRef("id"))
    )
    qs = (
        scope.annotate(has_newer=Exists(newer))
        .filter(has_newer=False)
        .exclude(
            source=raw.source,
            session_name=raw.session_name,
            message_id=raw.message_id,
        )
        .exclude(processing_state__in=["superseded", "deleted"])
    )
    time_field = "timestamp" if raw.sent_at_known else "received_at"
    before = getattr(raw, time_field)
    qs = qs.filter(
        Q(**{f"{time_field}__lt": before}) | Q(**{time_field: before, "id__lt": raw.id})
    )
    if raw.sent_at_known:
        qs = qs.filter(sent_at_known=True)
    return qs.order_by(f"-{time_field}", "-id"), time_field


def build_context(raw, cfg, known_projects, snapshot_id):
    counter, endpoint = context_runtime(cfg)
    qs, time_field = history_queryset(raw, snapshot_id)
    source_timezone = (
        raw.config.snapshot.get("timezone", "Asia/Almaty")
        if raw.config_id
        else "Asia/Almaty"
    )

    def payload(nearest):
        return extraction_payload(
            cfg,
            endpoint,
            raw.content,
            raw.sender_name,
            list(reversed(nearest)),
            known_projects,
            raw.timestamp.isoformat() if raw.sent_at_known else None,
            source_timezone,
        )

    max_input = (
        cfg.context_window_tokens
        - cfg.max_completion_tokens
        - cfg.context_safety_tokens
    )
    fixed = counter.count_payload(payload([]))
    if fixed > max_input:
        raise ProviderUnavailable("context_fixed_input_too_large")
    nearest, available, empty = [], 0, 0
    estimated, full = fixed, False
    available_first, available_last = None, None

    def fit_boundary(items):
        # Find the largest complete suffix. Only the next, oldest item may be partial.
        low, high = 0, len(items)
        while low < high:
            mid = (low + high + 1) // 2
            if counter.count_payload(payload(items[:mid])) <= max_input:
                low = mid
            else:
                high = mid - 1
        accepted = items[:low]
        if low == len(items):
            return accepted
        boundary = items[low]
        content = boundary["content"]
        starts = [offset for offset in counter.offsets(content) if offset > 0]

        def partial(start):
            return {
                **boundary,
                "content": content[start:],
                "partial": True,
                "omission_reason": "token_budget",
                "original_characters": len(content),
                "included_character_range": [start, len(content)],
            }

        lo, hi = 0, len(starts)
        while lo < hi:
            mid = (lo + hi) // 2
            if (
                counter.count_payload(payload(accepted + [partial(starts[mid])]))
                <= max_input
            ):
                hi = mid
            else:
                lo = mid + 1
        if lo < len(starts):
            accepted.append(partial(starts[lo]))
        return accepted

    fields = (
        "id",
        "message_id",
        "source_revision",
        "content",
        "sender_name",
        "timestamp",
        "received_at",
        "sent_at_known",
    )
    for row in qs.values(*fields).iterator(chunk_size=256):
        text = plain_text(row["content"])
        if not text:
            empty += 1
            continue
        available += 1
        when = row[time_field].isoformat()
        available_first = when
        if available_last is None:
            available_last = when
        if full:
            continue
        item = {
            "raw_message_id": row["id"],
            "message_id": row["message_id"],
            "source_revision": row["source_revision"],
            "content": text,
            "sender_name": plain_text(row["sender_name"]),
            "timestamp": row["timestamp"].isoformat() if row["sent_at_known"] else None,
            "received_at": row["received_at"].isoformat(),
            "partial": False,
        }
        nearest.append(item)
        estimated += counter.count_text(canonical_json(item)) + 4
        if estimated > max_input:
            actual = counter.count_payload(payload(nearest))
            if actual > max_input:
                nearest = fit_boundary(nearest)
                full = True
            else:
                estimated = actual
    if counter.count_payload(payload(nearest)) > max_input:
        nearest = fit_boundary(nearest)
    request = payload(nearest)
    input_tokens = counter.count_payload(request)
    if input_tokens > max_input:
        raise ProviderUnavailable("context_preflight_overflow")
    context = list(reversed(nearest))
    partial_count = sum(item["partial"] for item in context)
    included = len(context)
    metadata = {
        "schema_version": 1,
        "input_serialization": "target-last-v1",
        "policy_version": POLICY,
        "source": "chat_history",
        "model": cfg.chat_model_name,
        "provider": endpoint["tag"],
        "provider_window_tokens": endpoint["context_length"],
        "tokenizer_id": cfg.tokenizer_id,
        "tokenizer_revision": cfg.tokenizer_revision,
        "snapshot_max_id": snapshot_id,
        "time_basis": time_field,
        "window_tokens": cfg.context_window_tokens,
        "completion_reserve_tokens": cfg.max_completion_tokens,
        "safety_tokens": cfg.context_safety_tokens,
        "fixed_input_tokens": fixed,
        "history_budget_tokens": max_input - fixed,
        "input_tokens_preflight": input_tokens,
        "input_tokens_actual": None,
        "request_state": "prepared",
        "payload_sha256": payload_hash(request),
        "available_messages_count": available,
        "included_messages_count": included,
        "fully_included_messages_count": included - partial_count,
        "partial_messages_count": partial_count,
        "omitted_messages_count": available - included,
        "empty_messages_count": empty,
        "history_complete_in_request": included == available and partial_count == 0,
        "omission_reason": "token_budget"
        if included < available or partial_count
        else "",
        "available_period": [available_first, available_last],
        "included_period": [context[0][time_field], context[-1][time_field]]
        if context
        else [None, None],
        "external_import_completeness": "unknown",
    }
    return context, metadata, request
