"""Resume unresolved observations after a related entity has become known.

This route never re-extracts a message. It reuses the validated candidate and
only re-runs identity steps when the local catalog generation has changed.
"""

import copy

from django.db import transaction

from .ai_service import AIService, usage_event_id
from .context_tokens import payload_hash
from .entity_stages import POLICY, Comparison, COMPARE_PROMPT
from .entity_pipeline import (_call, _digest, _initialize, _owner, _progress,
                              _source, _validate_refs, _validate_snapshot, _validated, YieldStage)
from .entity_resolution import catalog_fingerprint, resolve
from .facts import FactSchema, json_value, normalize_fact_fields
from .message_context import source_scope
from .models import (AISettings, AuditEvent, FactCandidate, FactDecision,
                     FactEvidence, MessageProcessingTrace, OutboxEvent)
from .providers import ProviderUnavailable


def enqueue_if_changed(candidate):
    cfg = AISettings.get_active()
    previous = candidate.materialization_snapshot.get("entity_resolution", {})
    if (not cfg.entity_pipeline_enabled or not cfg.autonomous_enabled or candidate.status != "pending"
            or previous.get("state") != "review" or not previous.get("catalog_fingerprint")
            or candidate.trace.model_version != cfg.chat_model_name):
        return False
    generation = catalog_fingerprint(candidate.team_id, candidate.proposed_changes,
        object_id=previous.get("object_id"), project_id=previous.get("project_id"))
    if generation == previous["catalog_fingerprint"]:
        return False
    raw = candidate.trace.raw_message
    if raw is None:
        return False
    from .dialogue_threads import source_key, analysis_position
    fingerprint = _digest([candidate.proposed_changes, candidate.team_id, candidate.trace_id])
    OutboxEvent.objects.get_or_create(
        deduplication_key=f"entity-reconcile:{POLICY}:{candidate.pk}:{generation}:{fingerprint}",
        defaults={"event_type": "entity_reconcile", "analysis_source_key": source_key(raw),
                  "analysis_position": analysis_position(raw), "payload": {
                      "candidate_id": candidate.pk, "raw_id": raw.pk, "trace_id": candidate.trace_id,
                      "entity_operation_root_id": candidate.trace.context_metadata.get("entity_root_event_id", previous.get("root_event_id")),
                      "entity_policy": POLICY, "candidate_fingerprint": fingerprint,
                      "catalog_generation": generation}},
    )
    return True


def _hydrate(root, raw, references):
    known = {row["raw_message_id"] for row in root.payload["entity_sources"]}
    ids = {row["raw_message_id"] for row in references} - known
    if not ids:
        return
    rows = list(source_scope(raw).filter(pk__in=ids).exclude(
        processing_state__in=["deleted", "superseded", "clarification_reply"]).select_related("config"))
    if {row.pk for row in rows} != ids:
        raise ProviderUnavailable("context_source_unavailable")
    with transaction.atomic():
        owner = _owner()
        sources = {row["raw_message_id"]: row for row in owner.payload["entity_sources"]}
        sources.update({row.pk: _source(row) for row in rows})
        root.payload["entity_sources"] = list(sources.values())
        owner.payload = {**owner.payload, "entity_sources": root.payload["entity_sources"]}
        owner.save(update_fields=["payload"])


def process(payload):
    root = OutboxEvent.objects.get(pk=usage_event_id.get())
    if root.payload.get("entity_candidate_published"):
        return
    candidate = FactCandidate.objects.select_related("trace__raw_message__config__team", "trace__raw_message__team").get(pk=payload["candidate_id"])
    if candidate.status != "pending":
        return
    fingerprint = _digest([candidate.proposed_changes, candidate.team_id, candidate.trace_id])
    if fingerprint != payload["candidate_fingerprint"]:
        raise ProviderUnavailable("entity_candidate_changed")
    raw = candidate.trace.raw_message
    cfg = AIService._config()
    if raw is None or candidate.trace.model_version != cfg.chat_model_name:
        raise ProviderUnavailable("entity_configuration_changed")
    root = root if "entity_sources" in root.payload else _initialize(payload, cfg)
    _hydrate(root, raw, candidate.proposed_changes.get("evidence_messages", []))
    _validate_snapshot(root, raw, cfg)

    def compare(key, mention, candidates, coverage):
        references = mention.get("evidence_messages", []) + [ref for row in candidates for ref in row.get("evidence_messages", [])]
        _hydrate(root, raw, references)
        def validate(value):
            _validate_refs(value, root.payload["entity_sources"])
            if (value["decision"] == "match" and (value["candidate_id"] not in {row["id"] for row in candidates}
                                                    or not value["evidence_messages"])
                    or value["decision"] != "match" and value["candidate_id"] is not None):
                raise ProviderUnavailable("entity_comparison_invalid")
        return _call(root, cfg, "reconcile-" + key, COMPARE_PROMPT,
                     {"mention": json_value(mention), "candidates": json_value(candidates), "coverage": coverage},
                     Comparison(), "compare", validate)

    try:
        primary = source_scope(raw).select_related("config__team", "team", "project").get(
            pk=candidate.proposed_changes.get("evidence_message_id", raw.pk))
        resolved = resolve(copy.deepcopy(candidate.proposed_changes), primary, compare)
        _hydrate(root, raw, resolved["fact"].get("evidence_messages", []))
        _validate_refs(resolved["fact"], root.payload["entity_sources"])
        fact = _validated(FactSchema(), normalize_fact_fields([resolved["fact"]])[0])
        _publish(root, candidate, resolved, fact, fingerprint)
        _progress(root.pk)
    except YieldStage:
        _progress(root.pk)
        return {"history_continuation": True}
    except Exception:
        _progress(root.pk)
        raise


@transaction.atomic
def _publish(root, candidate, resolved, fact, fingerprint):
    owner = _owner()
    from .dialogue_threads import lock_candidate_source
    lock_candidate_source(candidate)
    raw = candidate.trace.raw_message
    list(source_scope(raw).filter(pk__in=[row["raw_message_id"] for row in owner.payload["entity_sources"]])
         .order_by("id").select_for_update().values_list("id", flat=True))
    _validate_snapshot(owner, raw, AIService._config())
    locked = FactCandidate.objects.select_for_update().get(pk=candidate.pk)
    if locked.status != "pending":
        return
    if _digest([locked.proposed_changes, locked.team_id, locked.trace_id]) != fingerprint:
        raise ProviderUnavailable("entity_candidate_changed")
    before = locked.materialization_snapshot.get("entity_resolution", {})
    locked.proposed_changes = fact
    locked.project_id = resolved.get("project_id")
    locked.object_identity_id = resolved.get("object_id")
    locked.base_project_version = resolved.get("project_version") or 0
    locked.materialization_snapshot = {**locked.materialization_snapshot,
        "entity_resolution": {**{key: value for key, value in resolved.items() if key != "fact"}, "root_event_id": root.pk}}
    if resolved["state"] == "duplicate":
        locked.status = "superseded"
        FactDecision.objects.get_or_create(candidate=locked, policy_version=POLICY, input_fingerprint=payload_hash(fact),
            defaults={"outcome": "rejected", "reason_code": "duplicate_event",
                      "explanation": "Повторное сообщение об уже учтённом событии; новая запись не создаётся.",
                      "validation": {"entity_resolution": locked.materialization_snapshot["entity_resolution"]}})
    locked.save(update_fields=["proposed_changes", "project", "object_identity", "base_project_version", "materialization_snapshot", "status"])
    for ref in fact.get("evidence_messages", []):
        FactEvidence.objects.get_or_create(candidate=locked, raw_message_id=ref["raw_message_id"], quote=ref["quote"], field_name=ref["role"])
    AuditEvent.objects.create(target_type="FactCandidate", target_id=locked.pk, action="entity_resolution_updated",
        before_after={"before": before, "after": locked.materialization_snapshot["entity_resolution"]})
    if locked.status == "pending":
        from .autonomous import enqueue_decision
        enqueue_decision(locked.pk, trigger=f"entity-reconciled:{root.pk}")
    owner.payload = {**owner.payload, "entity_candidate_published": True, "entity_current_stage": "apply_identity"}
    owner.save(update_fields=["payload"])
    transaction.on_commit(lambda: refresh_outcome(locked.trace_id))


@transaction.atomic
def refresh_outcome(trace_id):
    """A later identity resolution can finish a previously held source."""
    trace = MessageProcessingTrace.objects.select_related("raw_message").get(pk=trace_id)
    metadata = trace.context_metadata
    raw = trace.raw_message
    if raw is not None:
        type(raw).objects.select_for_update().get(pk=raw.pk)
    if (raw is None or metadata.get("entity_policy") != POLICY or "entity_source_uncertain" not in metadata
            or not raw.processed or trace.status == "error" or raw.traces.filter(id__gt=trace.pk).exists()):
        return
    pending = trace.candidates.filter(status="pending").exists()
    review = metadata["entity_source_uncertain"] or pending
    metadata["entity_outcome"] = "review" if review else "success"
    metadata["request_state"] = "needs_review" if review else "responded"
    trace.result_summary = ("Извлечённые наблюдения сохранены; требуется проверка." if review
                            else "Поэтапный разбор завершён; решения по наблюдениям сохранены.")
    trace.save(update_fields=["context_metadata", "result_summary"])
    type(raw).objects.filter(pk=raw.pk).update(processing_state="needs_review" if review else "analyzed")
