"""The complete source history, bounded only by the native model token budget."""

import re

from django.db.models import Exists, OuterRef, Q, F, Case, When, Value, IntegerField, DateTimeField, DurationField, ExpressionWrapper
from django.utils import timezone
from .message_time import source_metadata, source_zone

from .context_tokens import (
    canonical_json,
    context_runtime,
    extraction_payload,
    payload_hash,
)
from .models import RawMessage
from .plain_text import plain_text
from .providers import ProviderUnavailable

POLICY = "chat-history-threads-v3"
MAX_REMOTE_PREFLIGHT_PROBES = 4
MAX_REMOTE_BOUNDARY_PROBES = 12


def source_scope(raw):
    team = raw.config.team if raw.config_id else raw.team
    if not team or not team.is_active or (raw.team_id and raw.team_id != team.id):
        raise ProviderUnavailable("context_source_unavailable")
    if raw.config_id:
        if not raw.config.is_active:
            raise ProviderUnavailable("context_source_unavailable")
        from .whatsapp_identity import WHATSAPP_SOURCES
        return RawMessage.objects.filter(
            config_id=raw.config_id,
            source__in=WHATSAPP_SOURCES if raw.source in WHATSAPP_SOURCES else [raw.source],
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


def history_queryset(raw, snapshot_id, include_following=False):
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
        .exclude(processing_state__in=["superseded", "deleted", "deduplication_ambiguous", "export_staged"])
    )
    if include_following:
        at = raw.timestamp if raw.sent_at_known else raw.received_at
        qs = qs.annotate(order_time=Case(When(sent_at_known=True, then=F("timestamp")), default=F("received_at"), output_field=DateTimeField()))
        distance = Case(
            When(order_time__gte=at, then=ExpressionWrapper(F("order_time") - Value(at), output_field=DurationField())),
            default=ExpressionWrapper(Value(at) - F("order_time"), output_field=DurationField()),
            output_field=DurationField(),
        )
        return qs.annotate(distance=distance).order_by("distance", "order_time", "id"), "order_time"
    time_field = "timestamp" if raw.sent_at_known else "received_at"
    before = getattr(raw, time_field)
    qs = qs.filter(
        Q(**{f"{time_field}__lt": before}) | Q(**{time_field: before, "id__lt": raw.id})
    )
    if raw.sent_at_known:
        qs = qs.filter(sent_at_known=True)
    return qs.order_by(f"-{time_field}", "-id"), time_field


def build_context(raw, cfg, known_projects, snapshot_id, include_following=False, batch_ids=None, target_content=None, full_state=None):
    if getattr(cfg, 'full_history_policy', None) == 'thread-context-v2':
        from .full_history import prepare
        return prepare(raw, cfg, known_projects, snapshot_id, batch_ids, full_state or {})
    from .dialogue_threads import context_threads
    themes = context_threads(raw)
    counter, endpoint = context_runtime(cfg)
    qs, time_field = history_queryset(raw, snapshot_id, include_following)
    if themes and include_following:
        relevant_ids = [message_id for theme in themes for message_id in theme["message_ids"]]
        promise_ids = [task["source_message_id"] for theme in themes for task in theme.get("commitments", [])]
        qs = qs.annotate(theme_priority=Case(When(pk__in=(batch_ids or []), then=Value(0)), When(pk__in=promise_ids, then=Value(1)), When(pk__in=relevant_ids, then=Value(2)), default=Value(3), output_field=IntegerField())).order_by("theme_priority", "distance", "id")
    if batch_ids and include_following and not themes:
        qs = qs.annotate(batch_priority=Case(When(pk__in=batch_ids, then=Value(0)), default=Value(1), output_field=IntegerField())).order_by("batch_priority", "distance", "id")
    requested_batch = list(batch_ids or [])
    bounded = getattr(cfg, "analysis_policy", None) == "history-packets-v1"
    if bounded and include_following:
        # Lookup spans the whole immutable source; neighbors only seed retrieval.
        chronological = qs.order_by("distance", "order_time", "id")
        neighbors = list(chronological.values_list("id", flat=True)[:24])
        at = raw.timestamp if raw.sent_at_known else raw.received_at
        preceding = list(chronological.filter(Q(order_time__lt=at) | Q(order_time=at, id__lt=raw.id)).order_by("-order_time", "-id").values_list("id", flat=True)[:2])
        linked = {pk for theme in themes for pk in theme["message_ids"]}
        linked.update(task["source_message_id"] for theme in themes for task in theme.get("commitments", []))
        target_texts = list(source_scope(raw).filter(pk__in=requested_batch or [raw.id]).values_list("content", flat=True))
        words = set(re.findall(r"[\w-]{4,}", " ".join(target_texts).casefold())) - {
            "сегодня", "завтра", "получили", "сделали", "добрый", "утром", "объект", "оплата", "работа", "принято", "спасибо", "хорошо", "понятно"}
        search = Q(pk__in=[])
        for word in sorted(words, key=lambda value: (-len(value), value))[:12]:
            search |= Q(content__icontains=word)
        named = list(qs.filter(search).values_list("id", flat=True)[:24])
        quoted_ids = set()
        for target in source_scope(raw).filter(pk__in=requested_batch or [raw.id]):
            data = target.raw_payload if isinstance(target.raw_payload, dict) else {}
            for container in [data, data.get("_data", {}), data.get("replyTo", {})]:
                if isinstance(container, dict):
                    for key in ("quotedMessageId", "quotedStanzaID", "stanzaId"):
                        if isinstance(container.get(key), str):
                            quoted_ids.add(container[key])
        quoted = list(qs.filter(message_id__in=quoted_ids).values_list("id", flat=True))
        reply_context = set(preceding) | set(quoted)
        required = set(requested_batch) | linked | reply_context
        qs = qs.filter(pk__in=set(neighbors + named) | required).annotate(
            evidence_priority=Case(When(pk__in=requested_batch, then=Value(0)), When(pk__in=reply_context, then=Value(1)), When(pk__in=linked, then=Value(2)), default=Value(3), output_field=IntegerField())
        ).order_by("evidence_priority", "distance", "id")
    source = source_metadata(raw)
    source_timezone = source["timezone"]
    analysis_time = timezone.now().astimezone(source_zone(raw)).isoformat()

    def ordered(items):
        if not include_following:
            return list(reversed(items))
        return sorted(items, key=lambda item: (item.get("order_time") or item["timestamp"] or item["received_at"], item["raw_message_id"]))

    def payload(nearest):
        return extraction_payload(
            cfg,
            endpoint,
            raw.content if target_content is None else target_content,
            source["sender"],
            ordered(nearest),
            known_projects,
            source["sent_at"],
            source_timezone,
            source_metadata=source, current_time=analysis_time, target_message_id=raw.id,
            known_threads=themes, batch_message_ids=requested_batch,
        )

    incremental = getattr(cfg, "autonomous_enabled", False)
    if incremental:
        qs = qs[:max(1, cfg.autonomous_context_messages)]
    max_input = (
        cfg.context_window_tokens
        - cfg.max_completion_tokens
        - cfg.context_safety_tokens
    )
    if incremental:
        max_input = min(max_input, max(1, cfg.autonomous_input_tokens))
    if bounded and cfg.analysis_input_token_limit:
        max_input = min(max_input, cfg.analysis_input_token_limit)
    fixed = counter.count_payload(payload([]))
    if fixed > max_input:
        raise ProviderUnavailable("context_fixed_input_too_large")
    nearest, available, empty = [], 0, 0
    estimated, full, preflight_probes = fixed, False, 0
    available_first, available_last = None, None

    def fit_boundary(items):
        # Find the largest complete suffix. Only the next, oldest item may be partial.
        low, high = 0, len(items)
        probes = 0
        while low < high and (
            not counter.remote or probes < MAX_REMOTE_BOUNDARY_PROBES
        ):
            mid = (low + high + 1) // 2
            if counter.count_payload(payload(items[:mid])) <= max_input:
                low = mid
            else:
                high = mid - 1
            probes += 1
        accepted = items[:low]
        if low == len(items):
            return accepted
        boundary = items[low]
        content = boundary["content"]

        def partial(start):
            return {
                **boundary,
                "content": content[start:],
                "partial": True,
                "omission_reason": "token_budget",
                "original_characters": len(content),
                "included_character_range": [start, len(content)],
            }

        if counter.remote:
            # Character slicing is Unicode-safe. A capped binary search bounds
            # external count requests even for an exceptionally large message.
            lo, hi, found, probes = 1, len(content) - 1, None, 0
            while lo <= hi and probes < MAX_REMOTE_BOUNDARY_PROBES:
                mid = (lo + hi) // 2
                if (
                    counter.count_payload(payload(accepted + [partial(mid)]))
                    <= max_input
                ):
                    found, hi = mid, mid - 1
                else:
                    lo = mid + 1
                probes += 1
            if found is not None:
                accepted.append(partial(found))
        else:
            starts = [offset for offset in counter.offsets(content) if offset > 0]
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
    for message in qs.select_related("config").iterator(chunk_size=256):
        row = {field: getattr(message, field) for field in fields}
        original = source_metadata(message)
        text = plain_text(row["content"])
        if not text:
            empty += 1
            continue
        available += 1
        when = getattr(message, time_field).isoformat()
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
            "sender_name": plain_text(original["sender"]),
            "timestamp": original["sent_at"],
            "source_metadata": original,
            "received_at": row["received_at"].isoformat(),
            "partial": False,
            **({"order_time": message.order_time.isoformat()} if include_following else {}),
        }
        previous_estimate = estimated
        nearest.append(item)
        estimated += counter.count_text(canonical_json(item)) + 4
        if estimated > max_input:
            if counter.remote and preflight_probes >= MAX_REMOTE_PREFLIGHT_PROBES:
                nearest = fit_boundary(nearest)
                full = True
            else:
                actual = counter.count_payload(payload(nearest))
                if counter.remote:
                    preflight_probes += 1
                if actual > max_input:
                    if bounded:
                        # A clipped report is not evidence and should not consume
                        # the whole budget for an unrelated short target. Keep
                        # complete originals, then try the next relevant source.
                        nearest.pop()
                        estimated = previous_estimate
                        if item["raw_message_id"] in requested_batch:
                            raise ProviderUnavailable("context_batch_too_large")
                    else:
                        nearest = fit_boundary(nearest)
                        full = True
                else:
                    estimated = actual
    if counter.count_payload(payload(nearest)) > max_input:
        if bounded:
            while counter.count_payload(payload(nearest)) > max_input:
                optional = [index for index, item in enumerate(nearest) if item["raw_message_id"] not in requested_batch]
                if not optional:
                    raise ProviderUnavailable("context_batch_too_large")
                nearest.pop(optional[-1])
        else:
            nearest = fit_boundary(nearest)
    if requested_batch:
        included_ids = {raw.id} | {item["raw_message_id"] for item in nearest if not item["partial"]}
        if bounded and not set(requested_batch).issubset(included_ids):
            raise ProviderUnavailable("context_batch_too_large")
        requested_batch = [pk for pk in requested_batch if pk in included_ids]
    request = payload(nearest)
    input_tokens = counter.count_payload(request)
    if input_tokens > max_input:
        raise ProviderUnavailable("context_preflight_overflow")
    context = ordered(nearest)
    partial_count = sum(item["partial"] for item in context)
    included = len(context)
    metadata = {
        "schema_version": 1,
        "batch_message_ids": requested_batch,
        "known_threads": themes,
        "input_serialization": "target-last-v1",
        "policy_version": "history-packets-v1" if bounded else POLICY,
        "source": "chat_history",
        "model": cfg.chat_model_name,
        "provider": endpoint["tag"],
        "api_format": endpoint["api_format"],
        "effective_provider_url": endpoint["effective_provider_url"],
        "provider_window_tokens": endpoint["context_length"],
        "token_counter": counter.strategy,
        "token_count_requests": counter.request_count,
        "tokenizer_id": None if counter.remote else cfg.tokenizer_id,
        "tokenizer_revision": None if counter.remote else cfg.tokenizer_revision,
        "snapshot_max_id": snapshot_id,
        "time_basis": time_field,
        "target_source_metadata": source,
        "analysis_time": analysis_time,
        "includes_following_messages": include_following,
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
        "history_complete_in_request": not incremental and included == available and partial_count == 0,
        "incremental_context": incremental,
        "omission_reason": "token_budget"
        if included < available or partial_count
        else "",
        "available_period": [available_first, available_last],
        "included_period": [context[0][time_field], context[-1][time_field]]
        if context
        else [None, None],
        "source_period": [context[0]["timestamp"], context[-1]["timestamp"]] if context else [None, None],
        "external_import_completeness": "unknown",
        "relevance_selected": bounded,
        "omitted_reply_context_ids": sorted(reply_context - {item["raw_message_id"] for item in context}) if bounded and include_following else [],
    }
    return context, metadata, request
