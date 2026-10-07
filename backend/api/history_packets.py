"""Long originals are segmented without publishing incomplete business facts."""

import copy
import json
from django.db import transaction
from .models import HistoryAnalysisItem, MessageProcessingTrace, OutboxEvent, ProviderUsage, RawMessage
from .processing_attempts import reserve_attempt
from .providers import ProviderUnavailable


def prepare_segments(event, *, output_overflow=False):
    if event.payload.get("segment_ranges"):
        return False
    from .context_tokens import gemma_counter
    from .models import AISettings
    cfg = AISettings.get_active()
    # Only the installed, verified local profile is available without a remote probe.
    if cfg.chat_model_name != "gemma4:e4b":
        return False
    raw = RawMessage.objects.get(pk=event.payload["raw_id"])
    original = raw.traces.get(pk=event.payload["trace_id"])
    limit = max(512, original.context_metadata.get("analysis_limits", {}).get("analysis_input_token_limit", 16384) - 6000)
    counter = gemma_counter()
    if output_overflow:
        original_tokens = counter.count_text(json.dumps(raw.content, ensure_ascii=False))
        if original_tokens < 512:
            return False
        limit = min(limit, max(256, original_tokens // 2))
    ranges, start = [], 0
    while start < len(raw.content) and len(ranges) < 16:
        low, high = start + 1, len(raw.content)
        end = start
        while low <= high:
            mid = (low + high) // 2
            if counter.count_text(json.dumps(raw.content[start:mid], ensure_ascii=False)) <= limit:
                end, low = mid, mid + 1
            else:
                high = mid - 1
        if end <= start:
            return False
        if end < len(raw.content):
            boundary = raw.content.rfind("\n", start + (end-start)//2, end)
            if boundary > start:
                end = boundary + 1
        ranges.append([start, end])
        if end == len(raw.content):
            break
        start = max(start + 1, end - min(200, (end-start)//4))
    if not ranges or ranges[-1][1] != len(raw.content):
        return False
    traces = []
    for index, bounds in enumerate(ranges):
        trace = reserve_attempt(raw, f"{event.deduplication_key}:segment:{index}")
        trace.context_metadata.update(analysis_policy="history-packets-v1", analysis_limits=original.context_metadata.get("analysis_limits", {}), segment_range=bounds, parent_trace_id=original.id)
        for field in ("replace_unsent", "snapshot_max_id"):
            if field in original.context_metadata:trace.context_metadata[field]=original.context_metadata[field]
        trace.save(update_fields=["context_metadata"])
        traces.append(trace.id)
    event.payload = {**event.payload, "segment_ranges": ranges, "segment_trace_ids": traces,
                     "segment_cursor": 0, "trace_id": traces[0], "trace_ids": [{"raw_id":raw.id,"trace_id":traces[0]}], "schema_repaired":False}
    event.state, event.lease_until, event.attempt_count = "pending", None, 0
    event.error_code = ""
    event.save()
    HistoryAnalysisItem.objects.filter(outbox_event=event).update(trace_id=traces[-1], state="queued")
    return True


def merge_segments(traces):
    result = {"threads": [], "facts": []}
    identities, fact_keys = {}, set()
    for trace in traces:
        segment = trace.context_metadata["segment_result"]
        keys = {}
        for thread in segment["threads"]:
            identity = (thread.get("thread_id"), thread["topic"].casefold())
            if identity not in identities:
                saved = copy.deepcopy(thread)
                saved["key"] = f"segment_theme_{len(identities)}"
                identities[identity] = saved
                result["threads"].append(saved)
            saved = identities[identity]
            keys[thread["key"]] = saved["key"]
            if thread.get("state") == "ready":
                saved.update(state="ready", completion_reason=thread.get("completion_reason", ""))
            links = {link["raw_message_id"]:link for link in saved["messages"]}
            for link in thread["messages"]:
                if link["raw_message_id"] not in links or link["thought_state"] == "final":
                    links[link["raw_message_id"]] = copy.deepcopy(link)
            saved["messages"] = list(links.values())
        for thread in segment["threads"]:
            saved = next(row for row in result["threads"] if row["key"] == keys[thread["key"]])
            saved["parent_key"] = keys.get(thread.get("parent_key"))
        for fact in segment["facts"]:
            saved = {**fact, "thread_key":keys[fact["thread_key"]]}
            key = json.dumps(saved, ensure_ascii=False, sort_keys=True)
            if key not in fact_keys:
                fact_keys.add(key); result["facts"].append(saved)
    return result


def extract_packet(payload):
    from .pipeline import extract_message
    event_id = payload.get("root_event_id")
    if event_id and ProviderUsage.objects.filter(outbox_event__payload__root_event_id=event_id, operation="chat").count() >= 16:
        raise ProviderUnavailable("analysis_request_limit")
    common = dict(raw_id=payload["raw_id"], trace_id=payload["trace_id"], batch_ids=payload.get("batch_ids"), trace_ids=payload.get("trace_ids"))
    if not payload.get("segment_ranges"):
        return extract_message(**common)
    cursor = payload["segment_cursor"]
    trace_id = payload["segment_trace_ids"][cursor]
    common.update(trace_id=trace_id, trace_ids=[{"raw_id":payload["raw_id"],"trace_id":trace_id}])
    value = extract_message(**common, segment_range=payload["segment_ranges"][cursor], defer_result=True)
    if cursor + 1 < len(payload["segment_ranges"]):
        from .ai_service import usage_event_id
        with transaction.atomic():
            event = OutboxEvent.objects.select_for_update().get(pk=usage_event_id.get())
            if event.payload.get("history_cancelled"):
                raise ProviderUnavailable("history_run_cancelled")
            event.payload = {**event.payload, "segment_cursor":cursor+1,
                             "trace_id":payload["segment_trace_ids"][cursor+1], "schema_repaired":False}
            event.save(update_fields=["payload"])
        return {"history_continuation": True}
    traces = [MessageProcessingTrace.objects.get(pk=pk) for pk in payload["segment_trace_ids"]]
    final = traces[-1]
    final.context_metadata["segment_trace_ids"] = payload["segment_trace_ids"]
    final.save(update_fields=["context_metadata"])
    result = merge_segments(traces)
    return extract_message(**common, segment_range=payload["segment_ranges"][cursor], result_override=(result, value[1], {"segment_count":len(traces)}))
