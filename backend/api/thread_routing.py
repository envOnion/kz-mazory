"""LLM-owned topics and facts; version-checked LAYA membership through Q2."""

import copy
import hashlib
import json
import math
import time
from decimal import Decimal
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .dialogue_threads import analysis_position, lock_source, source_key
from .models import AISettings, DialogueThread, OutboxEvent, RawMessage, ThreadMessage, ThreadRoutingAttempt
from .providers import ProviderUnavailable, checked_base_url
from .decision_models import fingerprint

ANALYSIS_EVENTS = frozenset({"extract_message", "route_message", "extract_thread", "entity_reconcile", "classify_message_batch"})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def enabled(config):
    return bool(config and config.thread_routing_enabled and AISettings.get_active().decision_enabled)


def incoming_event_type(config):
    return "route_message" if enabled(config) else "extract_message"


def update_descriptor(thread, theme):
    """Only the validated LLM output supplies a descriptor, never classifier labels."""
    topic, summary = theme["topic"].strip(), theme["summary"].strip()
    prepared = theme["state"] in ("open", "ready") and len(summary) >= 12 and topic.casefold() not in {
        "тема требует уточнения", "общее обсуждение", "неизвестная тема", "unknown",
    }
    descriptor = {"topic": topic, "summary": summary, "evidence_message_ids": sorted({link["raw_message_id"] for link in theme["messages"]})}
    if thread.routing_descriptor != descriptor:
        thread.routing_descriptor_version += 1
        thread.routing_descriptor = descriptor
    thread.routing_state = "prepared" if prepared else "needs_context"
    thread.save(update_fields=["routing_descriptor", "routing_descriptor_version", "routing_state"])


def snapshot(raw):
    from .context_window import current_window
    from .dialogue_threads import context_threads
    qs = DialogueThread.objects.filter(source_key=source_key(raw), routing_state="prepared").exclude(state="superseded")
    if current_window() is not None:
        qs = qs.filter(pk__in=[row['id'] for row in context_threads(raw)])
    return [{"id": t.id, "version": t.routing_descriptor_version, "topic": t.topic, "summary": "" if current_window() is not None else t.routing_descriptor.get("summary", "")}
            for t in qs.order_by("id")[:201]]


def unsuitable(raw):
    text = raw.content.strip()
    if len(text) < 18 or text.casefold() in {"принято", "ок", "хорошо", "понял", "да", "спасибо"}:
        return "reply_requires_context"
    if text.startswith(("http://", "https://")):
        return "link_requires_context"
    if len(text.encode()) > 1500:
        return "routing_input_too_large"
    # Multi-section reports may belong to several themes; let the LLM split them.
    if text.count("\n") >= 4:
        return "multiple_subjects_possible"
    return ""


def predictions(raw, candidates, cfg=None):
    from .decision_models import predictions as predict
    return predict(raw, candidates, cfg or AISettings.get_active())


def schedule_extraction(thread, *, flush=False, exclude_event_id=None):
    """Caller holds the source lock. Pending work coalesces; claimed snapshots do not."""
    cfg = AISettings.get_active()
    if thread.routing_generation <= thread.last_extracted_routing_generation:
        return None
    active = OutboxEvent.objects.filter(event_type="extract_thread", payload__thread_id=thread.id, state__in=["pending", "enqueued", "processing"])
    if exclude_event_id:
        active = active.exclude(pk=exclude_event_id)
    current = active.order_by("id").first()
    pending = list(thread.message_links.filter(raw_message__processed=False).order_by("raw_message__timestamp", "raw_message_id"))
    if not pending:
        return current
    now = timezone.now()
    first_dirty = min(link.created_at or now for link in pending)
    due = now if flush or len(pending) >= cfg.thread_extract_batch_size else min(now + timedelta(seconds=cfg.thread_extract_idle_seconds), first_dirty + timedelta(seconds=cfg.thread_extract_max_wait_seconds))
    batch = pending[:min(cfg.thread_extract_batch_size,cfg.analysis_target_message_limit)]
    batch_ids = [link.raw_message_id for link in batch]
    payload = {"thread_id": thread.id, "raw_id": batch[-1].raw_message_id, 'batch_ids':batch_ids,"generation": thread.routing_generation, "policy_version": cfg.routing_policy_version}
    if current:
        if current.state == "pending":
            current.payload = payload
            current.next_attempt_at = due
            current.save(update_fields=["payload", "next_attempt_at"])
        return current
    return OutboxEvent.objects.get_or_create(
        deduplication_key=f"thread-extract:{thread.id}:{thread.routing_generation}:{digest(batch_ids)}:{cfg.routing_policy_version}",
        defaults={"event_type": "extract_thread", "payload": payload, "analysis_source_key": thread.source_key,
                  "analysis_position": analysis_position(pending[0].raw_message), "next_attempt_at": due},
    )[0]


def _bootstrap(payload, attempt, reason):
    from .tasks import extract_message
    attempt.outcome,attempt.reason_code = 'llm_pending',reason
    attempt.save(update_fields=['outcome','reason_code'])
    result = extract_message({**payload, "routing_bypass": True})
    if isinstance(result, dict) and result.get("history_continuation"):
        attempt.outcome, attempt.reason_code = "llm_pending", reason
        attempt.save(update_fields=["outcome", "reason_code"])
        return result
    raw = RawMessage.objects.get(pk=payload["raw_id"])
    attempt.outcome, attempt.reason_code = "llm", reason
    attempt.save(update_fields=["outcome", "reason_code"])
    ThreadMessage.objects.filter(raw_message=raw).update(assignment_method="llm", routing_attempt=attempt)
    return result


def route_message(payload):
    from .ai_service import usage_event_id
    from .message_context import source_scope
    started = time.monotonic()
    if not usage_event_id.get():
        raise ProviderUnavailable("routing_requires_worker")
    raw = RawMessage.objects.select_related("config__team", "team", "project").get(pk=payload["raw_id"])
    source_scope(raw)
    cfg = AISettings.get_active()
    if raw.processed or raw.processing_state in ("deleted", "superseded", "clarification_reply"):
        return
    candidates = snapshot(raw)
    input_hash = digest([raw.content, raw.sender_name, candidates, cfg.routing_policy_version,str(cfg.laya_match_threshold),str(cfg.laya_match_margin),fingerprint(cfg)])
    attempt, _ = ThreadRoutingAttempt.objects.get_or_create(operation_key=f"route:{usage_event_id.get()}:{raw.id}:{raw.source_revision}:{input_hash}", defaults={
        "raw_message": raw, "source_revision": raw.source_revision, "input_hash": input_hash,
        "candidate_snapshot": candidates, "policy_version": cfg.routing_policy_version,
    })
    if attempt.outcome in ("laya", "decision"):
        return
    if payload.get("full_history_state") or payload.get("trace_id") and attempt.outcome == "llm_pending":
        return _bootstrap(payload, attempt, attempt.reason_code)
    reason = ("routing_disabled" if not enabled(raw.config) else "") or unsuitable(raw) or ("no_prepared_topic" if not candidates else "too_many_topics" if len(candidates) > 200 else "")
    if not reason and cfg.thread_routing_mode != "shadow" and not cfg.decision_quality_confirmed:
        reason = "quality_not_validated"
    if reason:
        return _bootstrap(payload, attempt, reason)
    try:
        scores, revision = predictions(raw, candidates, cfg)
    except (ProviderUnavailable, requests.RequestException, ValueError, TypeError, KeyError) as exc:
        return _bootstrap(payload, attempt, str(exc) if isinstance(exc, ProviderUnavailable) else "decision_unavailable")
    attempt.scores, attempt.model_revision = scores, revision
    attempt.latency_ms = round((time.monotonic() - started) * 1000)
    attempt.save(update_fields=["scores", "model_revision", "latency_ms"])
    best = scores[0]
    margin = best["probability"] - (scores[1]["probability"] if len(scores) > 1 else 0)
    if cfg.thread_routing_mode == "shadow":
        return _bootstrap(payload, attempt, "shadow")
    ambiguous = best["probability"] < float(cfg.laya_match_threshold) or margin < float(cfg.laya_match_margin)
    stale = False
    with transaction.atomic():
        lock_source(raw)
        current_raw = RawMessage.objects.select_for_update().get(pk=raw.id)
        current = snapshot(raw)
        current_cfg = AISettings.get_active()
        policy_changed = fingerprint(current_cfg) != fingerprint(cfg)
        if current_raw.source_revision != raw.source_revision or current_raw.content != raw.content or current_raw.sender_name!=raw.sender_name or current != candidates or policy_changed or not current_raw.config.thread_routing_enabled:
            stale = True
        elif not ambiguous and not current_raw.processed:
            thread = DialogueThread.objects.select_for_update().get(pk=best["thread_id"], source_key=source_key(raw))
            link, created = ThreadMessage.objects.get_or_create(thread=thread, raw_message=raw, defaults={"thought_state": "unknown", "relation": "discusses", "rationale": "Предварительная принадлежность; бизнес-смысл проверит LLM.", "assignment_method": "decision", "routing_attempt": attempt})
            if created or link.routing_attempt_id != attempt.id:
                if not created:
                    link.assignment_method, link.routing_attempt = "decision", attempt
                    link.save(update_fields=["assignment_method", "routing_attempt"])
                thread.routing_generation += 1
                thread.save(update_fields=["routing_generation"])
            attempt.selected_thread, attempt.outcome = thread, "decision"
            attempt.save(update_fields=["selected_thread", "outcome"])
            RawMessage.objects.filter(pk=raw.id).update(processing_state="routed")
            schedule_extraction(thread)
    if stale:
        return _bootstrap(payload, attempt, "stale_snapshot")
    if ambiguous:
        return _bootstrap(payload, attempt, "ambiguous_membership")


def build_thread_context(raw, cfg, known_projects, maximum, state):
    """Originals of the selected thought remain evidence; summaries never replace them."""
    from .context_tokens import context_runtime, extraction_payload, payload_hash
    from .message_time import source_metadata, source_zone
    from .message_context import source_scope
    thread = DialogueThread.objects.get(pk=state["routing_thread_id"], source_key=source_key(raw))
    ids = list(thread.message_links.values_list("raw_message_id", flat=True))
    from .context_window import current_window
    window = current_window()
    all_inside = window is None or set(ids) <= set(window['source_ids'])
    if window is not None:
        ids = [pk for pk in ids if pk in window['source_ids']]
    rows = list(source_scope(raw).filter(pk__in=ids, id__lte=maximum).exclude(processing_state__in=["deleted", "superseded", "clarification_reply"]).order_by("timestamp", "id"))
    context = [{"raw_message_id": item.id, "content": item.content, "sender_name": item.sender_name, "timestamp": item.timestamp.isoformat(),
                "received_at": item.received_at.isoformat(), "source_metadata": source_metadata(item), "partial": False}
               for item in rows if item.pk != raw.pk]
    pending_ids = [item.pk for item in rows if not item.processed and item.pk in state['batch_ids']]
    descriptor = {"id": thread.id, "version": thread.version, "topic": thread.topic, "summary": thread.summary, "state": thread.state,
                  "parent_id": thread.parent_id, "project_id": thread.project_id, "message_ids": ids, "commitments": []}
    if window is not None:
        descriptor['summary'] = ''
    if not all_inside:
        descriptor.update(topic='', summary='', state='unknown', parent_id=None, project_id=None)
    prepared = copy.copy(cfg)
    prepared.analysis_policy = "history-packets-v1"
    counter, endpoint = context_runtime(prepared)
    source = source_metadata(raw)
    source_text = ' '.join(item.content for item in rows).casefold()
    known_projects = [item for item in known_projects if item['name'].casefold() in source_text or item.get('id')==thread.project_id][:20]
    request = extraction_payload(prepared, endpoint, raw.content, source["sender"], context, known_projects, source["sent_at"], source["timezone"],
        source_metadata=source, current_time=timezone.now().astimezone(source_zone(raw)).isoformat(), target_message_id=raw.id,
        known_threads=[descriptor], batch_message_ids=pending_ids)
    tokens = counter.count_payload(request)
    limit = min(cfg.analysis_input_token_limit or cfg.context_window_tokens, cfg.context_window_tokens - cfg.max_completion_tokens - cfg.context_safety_tokens)
    if tokens > limit:
        raise ProviderUnavailable("thread_context_too_large")
    metadata = {"known_threads": [descriptor], "snapshot_max_id": maximum, "batch_message_ids": pending_ids,
                "policy_version": "thread-routed-context-v1", "full_history_policy": "thread-routed-context-v1", "input_serialization": "target-last-v1",
                "source": "thread_originals", "input_tokens_preflight": tokens, "input_tokens_actual": None, "payload_sha256": payload_hash(request),
                "schema_version": 1, "model": cfg.chat_model_name, "provider": endpoint["tag"], "api_format": endpoint["api_format"],
                "effective_provider_url": endpoint["effective_provider_url"], "token_counter": counter.strategy,
                "target_source_metadata": source, "available_messages_count": len(rows) - 1, "included_messages_count": len(context),
                "fully_included_messages_count": len(context), "partial_messages_count": 0, "omitted_messages_count": 0,
                "history_complete_in_request": False, "omission_reason": "selected_thread", "request_state": "prepared",
                "evidence_ranges": {str(item.id): len(item.content) for item in rows},
                "routing_thread_id": thread.id, "routing_generation": state["routing_generation"], "routing_descriptor_version": thread.routing_descriptor_version}
    return context, metadata, request


def extract_thread(payload):
    from .ai_service import usage_event_id
    from .processing_attempts import reserve_attempt
    from .pipeline import extract_message
    if not usage_event_id.get():
        raise ProviderUnavailable("routing_requires_worker")
    thread = DialogueThread.objects.select_related("config__team").get(pk=payload["thread_id"])
    if payload.get("full_history_state") or payload.get("full_context_fallback"):
        from .tasks import extract_message as full_extract
        result = full_extract({**payload, "routing_bypass": True})
        if isinstance(result, dict) and result.get("history_continuation"):
            return {**result, "full_context_fallback": True}
        thread.refresh_from_db()
        with transaction.atomic():
            lock_source(RawMessage.objects.get(pk=payload['raw_id']))
            if not thread.message_links.filter(raw_message__processed=False).exists():
                thread.last_extracted_routing_generation = payload['generation']
                thread.save(update_fields=['last_extracted_routing_generation'])
            schedule_extraction(thread,exclude_event_id=usage_event_id.get())
        return result
    pending = list(thread.message_links.filter(raw_message__processed=False).select_related("raw_message__config__team").order_by("raw_message__timestamp", "raw_message_id"))
    if not pending:
        # A full-context LLM fallback may have extracted this dirty range already.
        first = thread.message_links.select_related("raw_message__config__team").first()
        if first:
            with transaction.atomic():
                lock_source(first.raw_message)
                thread.refresh_from_db()
                if not thread.message_links.filter(raw_message__processed=False).exists():
                    thread.last_extracted_routing_generation = thread.routing_generation
                    thread.save(update_fields=["last_extracted_routing_generation"])
        return
    if thread.routing_generation <= thread.last_extracted_routing_generation:
        return
    generation = thread.routing_generation
    cfg = AISettings.get_active()
    from .context_window import LIMIT
    pending = pending[:min(LIMIT + 1, cfg.thread_extract_batch_size,cfg.analysis_target_message_limit)]
    raw = pending[-1].raw_message
    from .message_context import history_queryset, source_scope
    from django.db.models import Max
    maximum = source_scope(raw).aggregate(last=Max('id'))['last'] or raw.id
    preceding, _ = history_queryset(raw, maximum)
    allowed = {raw.id, *preceding.values_list('id', flat=True)}
    pending = [link for link in pending if link.raw_message_id in allowed]
    with transaction.atomic():
        lock_source(raw)
        trace = raw.traces.filter(pk=payload['trace_id']).first() if payload.get('trace_id') else None
        if trace and (trace.context_metadata.get('routing_descriptor_version',thread.routing_descriptor_version)!=thread.routing_descriptor_version or trace.context_metadata.get('routing_generation',generation)!=generation):
            trace = None
        if trace is None:
            trace = reserve_attempt(raw, f"thread-extract:{usage_event_id.get()}:{generation}:{thread.routing_descriptor_version}")
        trace.context_metadata.update(full_history_policy="thread-routed-context-v1", analysis_policy="history-packets-v1")
        trace.save(update_fields=["context_metadata"])
        owner = OutboxEvent.objects.select_for_update().get(pk=usage_event_id.get())
        owner.payload = {**owner.payload, 'trace_id':trace.id,'raw_id':raw.id,'generation':generation,'batch_ids':[link.raw_message_id for link in pending]}
        owner.save(update_fields=['payload'])
    try:
        extract_message(raw.id, trace_id=trace.id, batch_ids=[link.raw_message_id for link in pending], full_state={"routing_thread_id": thread.id, "routing_generation": generation,'batch_ids':[link.raw_message_id for link in pending]})
    except ProviderUnavailable as exc:
        if str(exc) != "thread_context_too_large":
            raise
        from .tasks import extract_message as full_extract
        fallback = {**payload, "raw_id": raw.id, "generation": generation, "full_context_fallback": True,
                    "routing_bypass": True, "batch_ids": [link.raw_message_id for link in pending]}
        OutboxEvent.objects.filter(pk=usage_event_id.get()).update(payload=fallback)
        result = full_extract(fallback)
        if isinstance(result, dict) and result.get("history_continuation"):
            return {**result, "full_context_fallback": True}
        thread.refresh_from_db()
        with transaction.atomic():
            lock_source(raw)
            if not thread.message_links.filter(raw_message__processed=False).exists():
                thread.last_extracted_routing_generation = generation
                thread.save(update_fields=['last_extracted_routing_generation'])
            schedule_extraction(thread,exclude_event_id=usage_event_id.get())
        return result
    trace.refresh_from_db()
    if trace.status != "success":
        raise ProviderUnavailable(trace.error_code or "thread_extraction_failed")
    with transaction.atomic():
        lock_source(raw)
        thread.refresh_from_db()
        RawMessage.objects.filter(pk__in=[link.raw_message_id for link in pending]).update(processed=True, processing_state="analyzed")
        if not thread.message_links.filter(raw_message__processed=False).exists():
            thread.last_extracted_routing_generation = max(thread.last_extracted_routing_generation, generation)
            thread.save(update_fields=["last_extracted_routing_generation"])
        schedule_extraction(thread, exclude_event_id=usage_event_id.get())
