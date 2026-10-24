"""Durable live-message buffers; no provider is called before a batch is due."""

import copy
import hashlib
from collections import defaultdict
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .dialogue_threads import analysis_position, lock_source, source_key
from .models import AISettings, OutboxEvent, RawMessage, SourceWorkItem

POLICY = "live-50-or-5h-v1"
BUFFER_EVENT = "buffer_message"
BATCH_SIZE = 50
PROCESSING_BATCH_SIZE = 10
MAX_WAIT = timedelta(hours=5)
EXCLUDED_STATES = ("deleted", "superseded", "deduplication_ambiguous", "clarification_reply", "export_staged", "offtopic")


def processing_batch_size():
    configured = AISettings.get_active().analysis_target_message_limit
    return max(1, min(PROCESSING_BATCH_SIZE, configured))


def work_version(cfg, entity=False):
    return ("entity-stages-v1:" + hashlib.sha256(cfg.autonomous_policy_version.encode()).hexdigest()[:16]
            if entity else cfg.autonomous_policy_version)


@transaction.atomic
def enqueue(raw):
    """The unique member event prevents webhook/monitor/recovery duplicates."""
    if raw.processed or not raw.content.strip() or raw.processing_state in EXCLUDED_STATES:
        return None
    lock_source(raw)
    key = source_key(raw)
    pending = OutboxEvent.objects.filter(event_type=BUFFER_EVENT, analysis_source_key=key, state="pending")
    first = pending.order_by("next_attempt_at", "id").first()
    event, created = OutboxEvent.objects.get_or_create(
        deduplication_key=f"extract:{raw.id}",
        defaults={"event_type": BUFFER_EVENT, "analysis_source_key": key,
                  "analysis_position": analysis_position(raw),
                  "next_attempt_at": first.next_attempt_at if first else timezone.now() + MAX_WAIT,
                  "payload": {"raw_id": raw.id, "priority": "live", "live_batch_policy": POLICY}},
    )
    if created:
        cfg = AISettings.get_active()
        SourceWorkItem.objects.get_or_create(raw_message=raw,
            processing_version=work_version(cfg, cfg.entity_pipeline_enabled))
        RawMessage.objects.filter(pk=raw.pk, processed=False).update(processing_state="queued")
        _release(key, force=False)
    return event


def _release(key, *, force):
    """Caller holds the source lock. Claimed extraction roots are immutable."""
    now = timezone.now()
    target_limit = processing_batch_size()
    roots = []
    while True:
        members = list(OutboxEvent.objects.select_for_update().filter(
            event_type=BUFFER_EVENT, analysis_source_key=key, state="pending",
        ).order_by("analysis_position", "id")[:BATCH_SIZE])
        if not members:
            break
        raws = RawMessage.objects.select_related("config__team", "team", "project").in_bulk(
            [member.payload["raw_id"] for member in members])
        invalid = [member.pk for member in members if member.payload["raw_id"] not in raws
                   or raws[member.payload["raw_id"]].processed
                   or raws[member.payload["raw_id"]].processing_state in EXCLUDED_STATES
                   or source_key(raws[member.payload["raw_id"]]) != key]
        if invalid:
            OutboxEvent.objects.filter(pk__in=invalid).update(state="cancelled", error_code="buffer_source_unavailable")
            continue
        if len(members) < BATCH_SIZE and not (force and min(m.next_attempt_at for m in members) <= now):
            break
        batch = [raws[member.payload["raw_id"]] for member in members]
        from .batch_screening import enqueue as screen
        digest = hashlib.sha256(",".join(str(member.pk) for member in members).encode()).hexdigest()
        root = screen(batch, f"live:{digest}", downstream={"live_batch_policy": POLICY})
        for member in members:
            member.state = "done"
            member.payload = {**member.payload, "screening_event_id": root.id}
        roots.append(root.id)
        OutboxEvent.objects.bulk_update(members, ["state", "payload"])
    return roots



@transaction.atomic
def enqueue_extraction(rows, operation, extras=None):
    """Only a completed batch screen may fan out into extraction operations."""
    cfg = AISettings.get_active()
    allowed = {"history_run_id", "requested_by_id", "reanalyze", "commitment_refresh",
               "error_recovery_id", "trace_id", "trace_ids", "live_batch_policy", "analysis_policy",
               "screening_event_id", "priority"}
    extras = {key: value for key, value in (extras or {}).items() if key in allowed}
    ids = [row.pk for row in rows]
    traces = [entry for entry in extras.get("trace_ids", []) if entry["raw_id"] in ids]
    if traces:
        extras["trace_ids"] = traces
        extras["trace_id"] = next(entry["trace_id"] for entry in traces if entry["raw_id"] == ids[-1])
    elif len(ids) != 1:
        extras.pop("trace_id", None)
        extras.pop("trace_ids", None)
    payload = {"priority": "live", "routing_bypass": True, "analysis_policy": "history-packets-v1",
               "processing_version": work_version(cfg, cfg.entity_pipeline_enabled),
               "request_limit": max(16, len(ids) * 4),
               **({"entity_policy": "entity-stages-v1"} if cfg.entity_pipeline_enabled else {}),
               **extras, "raw_id": ids[-1], "batch_ids": ids}
    event, _ = OutboxEvent.objects.get_or_create(deduplication_key=operation, defaults={
        "event_type": "extract_message", "analysis_source_key": source_key(rows[0]),
        "analysis_position": analysis_position(rows[0]), "payload": payload})
    if "root_event_id" not in event.payload:
        event.payload = {**event.payload, "root_event_id": event.pk}
        event.save(update_fields=["payload"])
    return event

def release_due(limit=100):
    """Run before dispatch, including after a worker/Redis restart."""
    keys = list(OutboxEvent.objects.filter(event_type=BUFFER_EVENT, state="pending",
                next_attempt_at__lte=timezone.now()).order_by().values_list("analysis_source_key", flat=True).distinct()[:limit])
    for key in keys:
        with transaction.atomic():
            member = OutboxEvent.objects.filter(event_type=BUFFER_EVENT, state="pending", analysis_source_key=key).first()
            if member is None:
                continue
            raw = RawMessage.objects.select_related("config__team", "team", "project").filter(pk=member.payload["raw_id"]).first()
            if raw is None:
                OutboxEvent.objects.filter(pk=member.pk, state="pending").update(state="cancelled", error_code="buffer_source_unavailable")
                continue
            lock_source(raw)
            _release(key, force=True)


def enqueue_recovery(traces, operation, user):
    """Recovery also screens complete 50-message groups before extraction."""
    from .batch_screening import enqueue as screen
    groups = defaultdict(list)
    for trace in traces:
        groups[source_key(trace.raw_message)].append(trace)
    for key, group in sorted(groups.items()):
        group.sort(key=lambda trace: analysis_position(trace.raw_message))
        for offset in range(0, len(group), BATCH_SIZE):
            batch = group[offset:offset+BATCH_SIZE]
            digest = hashlib.sha256(','.join(str(trace.pk) for trace in batch).encode()).hexdigest()
            for trace in batch:
                trace.context_metadata.update(full_history_policy=None, live_batch_policy=POLICY)
                trace.save(update_fields=['context_metadata'])
            screen([trace.raw_message for trace in batch], f"recovery:{operation.pk}:{digest}", downstream={
                "raw_id": batch[-1].raw_message_id, "trace_id": batch[-1].pk,
                "trace_ids": [{"raw_id": trace.raw_message_id, "trace_id": trace.pk} for trace in batch],
                "requested_by_id": user.pk, "error_recovery_id": operation.pk,
                "live_batch_policy": POLICY, "analysis_policy": "history-packets-v1"})


def synchronize_failure(event):
    """Every target exposes the same retry/terminal outcome of its shared call."""
    if event.payload.get('live_batch_policy') != POLICY or event.state not in ('pending', 'failed', 'cancelled'):
        return
    from .models import MessageProcessingTrace
    from django.db.models import Max
    for entry in event.payload.get('trace_ids', []):
        trace = MessageProcessingTrace.objects.get(pk=entry['trace_id'])
        if trace.status == 'success':
            continue
        trace.status = 'error' if event.state == 'failed' else 'warning'
        trace.error_code = event.error_code
        if event.state == 'failed':
            trace.context_metadata['request_state'] = 'failed'
        trace.result_summary = ('Обработка не завершена. Новые факты не сохранены.' if event.state == 'failed'
                                else 'Пачка ожидает повторной обработки.' if event.state == 'pending'
                                else 'Попытка остановлена; история сохранена.')
        trace.save(update_fields=['status', 'error_code', 'context_metadata', 'result_summary'])
        latest = trace.raw_message.traces.aggregate(last=Max('id'))['last']
        if latest == trace.pk and event.state == 'failed':
            RawMessage.objects.filter(pk=trace.raw_message_id, processed=False).update(processing_state='failed')


@transaction.atomic
def prepare(payload):
    """Reserve a trace for every target only when the real batch starts."""
    if payload.get("trace_ids"):
        return payload
    from .ai_service import usage_event_id
    from .processing_attempts import reserve_attempt
    rows = RawMessage.objects.select_related("config__team", "team", "project").in_bulk(payload["batch_ids"])
    raw = rows[payload["raw_id"]]
    lock_source(raw)
    cfg = AISettings.get_active()
    traces = []
    for pk in payload["batch_ids"]:
        trace = reserve_attempt(rows[pk], f"live-batch:{usage_event_id.get()}:target:{pk}", cfg=cfg)
        trace.context_metadata.update(full_history_policy=None)
        if payload.get("live_batch_policy"):
            trace.context_metadata["live_batch_policy"] = POLICY
        if payload.get("history_run_id"):
            from .models import WhatsAppHistoryRun, HistoryAnalysisItem
            from .processing_attempts import ANALYSIS_CONFIG_FIELDS
            run = WhatsAppHistoryRun.objects.get(pk=payload["history_run_id"])
            trace.context_metadata["analysis_limits"].update({
                name: run.settings_snapshot.get(name, getattr(cfg, name))
                for name in ("analysis_input_token_limit", "analysis_output_token_limit", *ANALYSIS_CONFIG_FIELDS)})
            trace.context_metadata["full_history_policy"] = run.settings_snapshot.get("full_history_policy")
            if run.settings_snapshot.get("replace_unsent"):
                trace.context_metadata.update(replace_unsent=True, snapshot_max_id=run.settings_snapshot["snapshot_max_id"])
            HistoryAnalysisItem.objects.filter(run=run, raw_message_id=pk).update(trace=trace)
        trace.context_metadata["analysis_limits"]["analysis_target_message_limit"] = len(payload["batch_ids"])
        trace.save(update_fields=["context_metadata"])
        traces.append({"raw_id": pk, "trace_id": trace.id})
    owner = OutboxEvent.objects.select_for_update().get(pk=usage_event_id.get())
    owner.payload = {**owner.payload, "trace_ids": traces,
                     "trace_id": next(row["trace_id"] for row in traces if row["raw_id"] == raw.id)}
    owner.save(update_fields=["payload"])
    return owner.payload


def split(event):
    """Transfer BOTH halves to new immutable attempts; never drop targets."""
    if event.payload.get("live_batch_policy") != POLICY or len(event.payload.get("batch_ids", [])) < 2:
        return False
    from .models import AISettings, MessageProcessingTrace
    from .processing_attempts import reserve_attempt
    ids = event.payload["batch_ids"]
    rows = RawMessage.objects.select_related("config__team", "team", "project").in_bulk(ids)
    lock_source(rows[event.payload["raw_id"]])
    previous = MessageProcessingTrace.objects.get(pk=event.payload["trace_id"])
    active_cfg = AISettings.get_active()
    for group in (ids[:len(ids) // 2], ids[len(ids) // 2:]):
        operation = f"live-split:{event.id}:{group[0]}:{group[-1]}"
        traces = []
        for pk in group:
            trace = reserve_attempt(rows[pk], f"{operation}:target:{pk}")
            trace.context_metadata.update({name: copy.deepcopy(previous.context_metadata[name]) for name in (
                "analysis_limits", "snapshot_max_id", "context_window", "live_batch_policy") if name in previous.context_metadata})
            # A split is a fresh attempt. Honor the current admin output budget
            # instead of carrying a smaller reserve from the rejected parent.
            limits = trace.context_metadata.get("analysis_limits", {})
            limits["analysis_output_token_limit"] = active_cfg.analysis_output_token_limit
            limits["max_completion_tokens"] = active_cfg.max_completion_tokens
            limits["analysis_target_message_limit"] = len(group)
            trace.context_metadata["analysis_limits"] = limits
            trace.context_metadata.update(full_history_policy=None, parent_trace_id=previous.id, repair_reason=event.error_code)
            if "context_window" in trace.context_metadata:
                trace.context_metadata["context_window"].update(anchor_id=group[-1], target_ids=group)
            trace.save(update_fields=["context_metadata"])
            traces.append({"raw_id": pk, "trace_id": trace.id})
        payload = {**event.payload, "raw_id": group[-1], "batch_ids": group,
                   "trace_id": traces[-1]["trace_id"], "trace_ids": traces}
        payload.pop("claim_generation", None)
        payload.pop("schema_repaired", None)
        OutboxEvent.objects.get_or_create(deduplication_key=operation, defaults={
            "event_type": "extract_message", "analysis_source_key": event.analysis_source_key,
            "analysis_position": analysis_position(rows[group[0]]), "payload": payload})
    event.state, event.lease_until = "failed", None
    event.save(update_fields=["state", "lease_until"])
    return True
