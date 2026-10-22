"""One immutable source window shared by every call of a message operation."""

import copy
import json
import inspect
from contextvars import ContextVar
from functools import wraps

from .providers import ProviderUnavailable

POLICY = "last-50-messages-v1"
LIMIT = 50
_window = ContextVar("message_context_window", default=None)


def current_window():
    return _window.get()


def bounded_operation(function):
    signature = inspect.signature(function)
    @wraps(function)
    def wrapped(*args, **kwargs):
        from django.db.models import Max
        from .models import MessageProcessingTrace, RawMessage
        from .message_context import history_queryset, source_scope

        arguments = signature.bind(*args, **kwargs).arguments
        payload = arguments.get('payload', arguments)
        # The versioned entity coordinator owns explicit, fingerprinted source
        # spans per stage and may retrieve one old promise by provenance. It
        # must not inherit the legacy monolithic last-50 request contract.
        if payload.get('entity_policy') == 'entity-stages-v1':
            return function(*args, **kwargs)
        raw_id = payload['raw_id']
        if current_window() is not None:
            if raw_id not in current_window()["source_ids"]:
                raise ProviderUnavailable("context_source_unavailable")
            return function(*args, **kwargs)
        raw = RawMessage.objects.select_related("config__team", "team").get(pk=raw_id)
        trace_id = payload.get("trace_id")
        trace = MessageProcessingTrace.objects.filter(pk=trace_id, raw_message_id=raw_id).first() if trace_id else None
        metadata = trace.context_metadata if trace else {}
        manifest = metadata.get("context_window")
        if manifest is None:
            if metadata.get("request_envelope") or payload.get("full_history_state"):
                raise ProviderUnavailable("context_configuration_changed")
            maximum = metadata.get("snapshot_max_id") or source_scope(raw).aggregate(last=Max("id"))["last"] or raw.id
            targets = list(dict.fromkeys(payload.get("batch_ids") or [raw.id]))
            following = bool(payload.get("requested_by_id") or payload.get("reanalyze") or payload.get("history_run_id") or payload.get("live_batch_policy"))
            qs, _ = history_queryset(raw, maximum, include_following=following, target_ids=targets)
            manifest = {"policy": POLICY, "anchor_id": raw.id, "snapshot_max_id": maximum,
                        "include_following": following, "source_ids": [raw.id, *qs.values_list("id", flat=True)],
                        "target_ids": targets}
        if (manifest.get("policy") != POLICY or manifest.get("anchor_id") != raw.id
                or raw.id not in manifest.get("source_ids", [])
                or type(manifest.get("include_following")) is not bool
                or any(type(pk) is not int for pk in manifest["source_ids"])
                or len(manifest["source_ids"]) > LIMIT + 1
                or len(set(manifest["source_ids"])) != len(manifest["source_ids"])):
            raise ProviderUnavailable("context_configuration_changed")
        targets = payload.get("batch_ids") or [raw.id]
        if not set(targets) <= set(manifest["source_ids"]):
            raise ProviderUnavailable("context_source_unavailable")
        # Revalidate a saved manifest against chat identity, revisions and time;
        # pinning its IDs avoids selecting a different window during a retry.
        history_queryset(raw, manifest['snapshot_max_id'],
                         include_following=manifest['include_following'],
                         target_ids=manifest['source_ids'])
        if trace and metadata.get("context_window") is None:
            trace.context_metadata["context_window"] = copy.deepcopy(manifest)
            trace.save(update_fields=["context_metadata"])
        token = _window.set(manifest)
        try:
            return function(*args, **kwargs)
        finally:
            _window.reset(token)
    return wrapped


def validate_payload(payload):
    """Auxiliary calls cannot add an original outside the operation's window."""
    window = current_window()
    if window is None:
        return
    try:
        value = json.loads(next(row["content"] for row in reversed(payload["messages"]) if row.get("role") == "user"))
    except (KeyError, StopIteration, TypeError, ValueError):
        raise ProviderUnavailable("context_snapshot_mismatch") from None
    allowed = set(window["source_ids"])
    id_fields = {"raw_message_id", "target_message_id", "evidence_message_id", "promise_message_id",
                 "deadline_message_id", "fulfillment_message_id"}
    list_fields = {"batch_message_ids", "read_source_ids", "message_ids"}
    def check(node):
        if isinstance(node, list):
            for item in node:
                check(item)
        elif isinstance(node, dict):
            for key, item in node.items():
                if key in id_fields and item is not None and (type(item) is not int or item not in allowed):
                    raise ProviderUnavailable("context_source_unavailable")
                if key in list_fields and isinstance(item, list) and not set(item) <= allowed:
                    raise ProviderUnavailable("context_source_unavailable")
                check(item)
    check(value)
    sources = {value.get("target_message_id")}
    sources.update(row["raw_message_id"] for row in value.get("context", []) + value.get("target_messages", []))
    if len(sources) > LIMIT + 1 or not sources <= allowed:
        raise ProviderUnavailable("context_source_unavailable")
