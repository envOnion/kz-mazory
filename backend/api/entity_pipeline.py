"""Resumable background coordinator for small, separately validated AI steps.

The original extraction event remains the reporting/lease owner. Each paid step
has its own immutable OutboxEvent checkpoint and usage. One new call is allowed
per worker invocation; completed steps survive a crash or a later failure.
"""

import copy
import hashlib
import json
import time

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .ai_service import AIService, outbox_claim, usage_event_id
from .context_tokens import canonical_json, context_runtime, payload_hash
from .entity_stages import (POLICY, Observations, Comparison, OBSERVE_PROMPT, FACT_PROMPT,
                            KIND_PROMPT, COMPARE_PROMPT, fact_result_serializer, schema)
from .facts import fact_identity, json_value, normalize_fact_fields
from .message_context import source_scope
from .message_time import source_metadata
from .models import AISettings, MessageProcessingTrace, OutboxEvent, ProviderUsage, RawMessage
from .providers import ProviderUnavailable


class YieldStage(Exception):
    """A paid result is durable; release this worker before doing more work."""


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _owner():
    event = OutboxEvent.objects.select_for_update().get(pk=usage_event_id.get())
    if (event.state != "processing" or event.payload.get("claim_generation") != outbox_claim.get()
            or event.payload.get("history_cancelled")):
        raise ProviderUnavailable("analysis_claim_expired")
    return event


def _request(cfg, prompt, inputs, validator, name):
    # Quote IDs let the model cite immutable original lines without copying,
    # spelling correction or repeating a large literal enum in the schema.
    native = copy.deepcopy(json_value(inputs))
    catalog, ids = [], {}
    def quote_id(text, source_id):
        key = (source_id, text)
        if key not in ids:
            ids[key] = len(catalog) + 1
            catalog.append({"quote_id": ids[key], "raw_message_id": source_id, "text": text})
        return ids[key]
    def project(node):
        if isinstance(node, list):
            for item in node:
                project(item)
        elif isinstance(node, dict):
            if "content" in node and type(node.get("raw_message_id")) is int and isinstance(node["content"], str):
                content = node.pop("content")
                node["quote_ids"] = [quote_id(line, node["raw_message_id"]) for line in content.splitlines() if line.strip()]
            if isinstance(node.get("quote"), str) and type(node.get("raw_message_id")) is int:
                node["quote"] = quote_id(node["quote"], node["raw_message_id"])
            if isinstance(node.get("evidence"), str) and type(node.get("evidence_message_id")) is int:
                node["evidence"] = quote_id(node["evidence"], node["evidence_message_id"])
            for value in list(node.values()):
                project(value)
    project(native)
    native["source_quotes"] = catalog
    shape = schema(validator)
    def quote_fields(node):
        if isinstance(node, dict):
            properties = node.get("properties", {})
            for field in ("evidence", "quote"):
                if field in properties:
                    properties[field] = {"type": "integer", "minimum": 1}
            for value in node.values():
                quote_fields(value)
        elif isinstance(node, list):
            for value in node:
                quote_fields(value)
    quote_fields(shape)
    prompt += "\nВ полях evidence и quote возвращай целочисленный quote_id из source_quotes, а не строку. Не переписывай цитаты. ID должен принадлежать тому же raw_message_id."
    return {"model": cfg.chat_model_name, "max_tokens": cfg.max_completion_tokens,
            "messages": [{"role": "system", "content": prompt},
                         {"role": "user", "content": canonical_json(native)}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "mazory_entity_" + name, "strict": True, "schema": shape}}}


def _restore_quotes(value, request):
    inputs = json.loads(request["messages"][-1]["content"])
    catalog = {row["quote_id"]: row for row in inputs["source_quotes"]}
    def restore(node):
        if isinstance(node, list):
            return [restore(row) for row in node]
        if isinstance(node, dict):
            value = {key: restore(item) for key, item in node.items()}
            for field in ("quote", "evidence"):
                if field in value:
                    selected = catalog.get(value[field]) if type(value[field]) is int else None
                    expected = value.get("raw_message_id") if field == "quote" else value.get("evidence_message_id")
                    if selected is None or selected["raw_message_id"] != expected:
                        raise ProviderUnavailable("entity_quote_reference_invalid")
                    value[field] = selected["text"]
            return value
        return node
    return restore(value)


def _metrics(cfg, request, counter):
    from .minimax import enabled, request as minimax_request
    wire = minimax_request(request, cfg.max_completion_tokens) if enabled(cfg) else request
    return {"wire_bytes": len(canonical_json(wire).encode()),
            "system_bytes": sum(len(row["content"].encode()) for row in wire.get("messages", []) if row["role"] == "system"),
            "schema_bytes": len(canonical_json(wire.get("tools", wire.get("response_format", {}))).encode()),
            "input_tokens_estimate": counter.count_payload(request), "counter": counter.strategy}


def _call(root, cfg, key, prompt, inputs, validator, name, validate_result=None):
    request = _request(cfg, prompt, inputs, validator, name)
    fingerprint = _digest({"policy": POLICY, "config": root.payload["entity_config"], "request": request})
    counter, _ = context_runtime(cfg)
    measured = _metrics(cfg, request, counter)
    available = min(cfg.entity_input_token_limit,
                    cfg.analysis_input_token_limit or cfg.entity_input_token_limit,
                    cfg.context_window_tokens - cfg.max_completion_tokens - cfg.context_safety_tokens)
    if measured["input_tokens_estimate"] > available:
        raise ProviderUnavailable("entity_stage_input_too_large", diagnostics={"stage": key, **measured, "budget": available})
    with transaction.atomic():
        owner = _owner()
        step, _ = OutboxEvent.objects.select_for_update().get_or_create(
            deduplication_key=f"entity:{POLICY}:{root.pk}:{key}:{fingerprint}",
            defaults={"event_type": "entity_checkpoint", "state": "checkpoint", "payload": {
                "root_event_id": root.pk, "stage": key, "policy": POLICY, "fingerprint": fingerprint,
                "input_refs": inputs, "request_sha256": payload_hash(request), "metrics": measured}},
        )
        if step.state == "done":
            saved = _normalize_result(step.payload["result"], name)
            value = _validated(validator, saved)
            if validate_result:
                validate_result(value)
            return value
        operation_id = owner.payload.get("entity_operation_root_id") or owner.pk
        operation = owner if operation_id == owner.pk else OutboxEvent.objects.select_for_update().get(pk=operation_id)
        attempts = owner.payload.get("entity_call_count", 0)
        operation_attempts = operation.payload.get("entity_call_count", 0)
        call_limit = min(cfg.entity_max_calls, operation.payload.get("entity_call_limit", cfg.entity_max_calls))
        if step.attempt_count >= cfg.entity_stage_attempts or operation_attempts >= call_limit:
            raise ProviderUnavailable("entity_stage_budget_exhausted", diagnostics={"stage": key, "calls": attempts})
        repair = step.payload.get("diagnostics", {}).get("schema_errors", [])
        if repair:
            request["messages"][0]["content"] += "\nПредыдущая попытка отклонена валидатором. Исправь только формат нового полного ответа по схеме: " + canonical_json(repair[:8])
        step.attempt_count += 1
        step.error_code = ""
        step.payload = {**step.payload, "claim_generation": outbox_claim.get()}
        step.save(update_fields=["attempt_count", "error_code", "payload"])
        owner.payload = {**owner.payload, "entity_call_count": attempts + 1, "entity_current_stage": key}
        owner.save(update_fields=["payload"])
        if operation.pk != owner.pk:
            operation.payload = {**operation.payload, "entity_call_count": operation_attempts + 1}
            operation.save(update_fields=["payload"])
    token = usage_event_id.set(step.pk)
    started = time.monotonic()
    value, usage, diagnostics = None, {}, {}
    try:
        actual = _metrics(cfg, request, counter)
        if actual["input_tokens_estimate"] > available:
            raise ProviderUnavailable("entity_stage_input_too_large")
        claimed = OutboxEvent.objects.filter(pk=step.pk, payload__claim_generation=outbox_claim.get()).update(
            payload={**step.payload, "metrics": actual, "request_sha256": payload_hash(request)})
        if not claimed:
            raise ProviderUnavailable("analysis_claim_expired")
        value, usage, diagnostics = AIService._analyze_payload_once(request, config=cfg)
        value = _restore_quotes(value, request)
        value = _validated(validator, _normalize_result(value, name))
        _validate_refs(value, root.payload["entity_sources"])
        if validate_result:
            validate_result(value)
        with transaction.atomic():
            # Restore the root context before verifying its claim.
            usage_event_id.reset(token)
            token = None
            _owner()
            step = OutboxEvent.objects.select_for_update().get(pk=step.pk)
            step.payload = {**step.payload, "result": value, "usage": usage, "diagnostics": diagnostics,
                            "result_sha256": _digest(value), "duration_ms": round((time.monotonic() - started) * 1000)}
            step.payload["attempt_usage"] = [*step.payload.get("attempt_usage", []), {"usage": usage, "reasoning_tokens": diagnostics.get("reasoning_tokens")}]
            step.state, step.error_code = "done", ""
            step.save(update_fields=["payload", "state", "error_code"])
    except Exception as exc:
        with transaction.atomic():
            owner = OutboxEvent.objects.select_for_update().get(pk=root.pk)
            failed = OutboxEvent.objects.select_for_update().get(pk=step.pk)
            if (owner.state != "processing" or owner.payload.get("claim_generation") != outbox_claim.get()
                    or failed.payload.get("claim_generation") != outbox_claim.get() or failed.state == "done"):
                raise
            if isinstance(exc, ProviderUnavailable):
                diagnostics = {**diagnostics, **exc.diagnostics}
            failed.payload = {**failed.payload, "rejected_result": json_value(value), "usage": usage,
                              "diagnostics": diagnostics, "duration_ms": round((time.monotonic() - started) * 1000)}
            failed.payload["attempt_usage"] = [*failed.payload.get("attempt_usage", []), {"usage": usage, "reasoning_tokens": diagnostics.get("reasoning_tokens")}]
            failed.error_code = str(exc)[:64] if isinstance(exc, ProviderUnavailable) else type(exc).__name__
            admission_wait = isinstance(exc, ProviderUnavailable) and (str(exc) in {"ai_daily_budget_exhausted", "ai_minute_limit_exhausted"}
                or str(exc) in {"provider_circuit_open", "provider_in_flight_budget"} and exc.diagnostics.get("request_admission_denied"))
            if admission_wait:
                failed.attempt_count = max(0, failed.attempt_count - 1)
                # Lock in the same order as admission: this coordinator, then
                # the original operation whose budget its follow-ups share.
                operation_id = owner.payload.get("entity_operation_root_id") or owner.pk
                budgets = [owner]
                if operation_id != owner.pk:
                    budgets.append(OutboxEvent.objects.select_for_update().get(pk=operation_id))
                for budget_owner in budgets:
                    budget_owner.payload["entity_call_count"] = max(0, budget_owner.payload.get("entity_call_count", 1) - 1)
                    budget_owner.save(update_fields=["payload"])
            failed.save(update_fields=["payload", "error_code", "attempt_count"])
        raise
    finally:
        if token is not None:
            usage_event_id.reset(token)
    raise YieldStage()


def _validated(validator, value):
    checked = type(validator)(data=value)
    try:
        checked.is_valid(raise_exception=True)
    except ValidationError as exc:
        raise ProviderUnavailable("invalid_extraction_schema", diagnostics={
            "schema_errors": [json_value(exc.detail)]}) from exc
    return json_value(checked.validated_data)


def _normalize_result(value, name):
    if name == "fact" and isinstance(value, dict) and isinstance(value.get("fact"), dict):
        return {**value, "fact": normalize_fact_fields([value["fact"]])[0]}
    return value


def retry_failure(event, exc):
    """Retry this phase only; legacy batch splitting cannot reopen paid stages."""
    if event.payload.get("entity_policy") != POLICY:
        return False
    from datetime import timedelta
    from .ai_retries import record_attempt
    cfg = AISettings.get_active()
    code = str(exc)[:64] if isinstance(exc, ProviderUnavailable) else type(exc).__name__
    permanent = code in {"entity_configuration_changed", "entity_source_changed", "entity_atomic_source_too_large",
        "entity_stage_input_too_large", "entity_stage_budget_exhausted", "entity_projection_changed", "reanalysis_access_revoked",
        "context_source_unavailable", "analysis_claim_expired", "entity_candidate_changed"}
    last = OutboxEvent.objects.filter(event_type="entity_checkpoint", payload__root_event_id=event.pk).order_by("-id").first()
    exhausted = last is not None and last.state != "done" and last.attempt_count >= cfg.entity_stage_attempts
    terminal = permanent or exhausted or event.attempt_count >= cfg.entity_stage_attempts
    next_at = timezone.now() + timedelta(seconds=max(5, getattr(exc, "retry_after", None) or 15))
    OutboxEvent.objects.filter(pk=event.pk, state="processing", payload__claim_generation=event.payload["claim_generation"]).update(
        state="failed" if terminal else "pending", error_code=code, lease_until=None, next_attempt_at=next_at)
    record_attempt(event, "failed" if terminal else "pending", code, None if terminal else next_at)
    return True


def _validate_refs(value, sources):
    originals = {}
    for row in sources:
        originals.setdefault(row["raw_message_id"], []).append(row["content"])
    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            if "quote" in node and "raw_message_id" in node:
                if (type(node["raw_message_id"]) is not int or node["raw_message_id"] not in originals
                        or not isinstance(node["quote"], str) or not node["quote"].strip()
                        or not any(node["quote"] in content for content in originals[node["raw_message_id"]])):
                    raise ProviderUnavailable("evidence_not_in_source")
            for item in node.values():
                visit(item)
    visit(value)


def _source(row):
    meta = source_metadata(row)
    return {"raw_message_id": row.pk, "content": row.content, "sender": meta["sender"],
            "sent_at": meta["sent_at"], "timezone": meta["timezone"]}


def _initialize(payload, cfg):
    from .ai_connection_checks import config_fingerprint
    raw = RawMessage.objects.select_related("config__team", "team", "project").get(pk=payload["raw_id"])
    targets = payload.get("batch_ids") or [raw.pk]
    scope = source_scope(raw).exclude(processing_state__in=["deleted", "superseded", "clarification_reply"])
    originals = list(scope.filter(pk__in=targets).select_related("config").order_by("timestamp", "id"))
    if {row.pk for row in originals} != set(targets):
        raise ProviderUnavailable("context_source_unavailable")
    sources = [_source(row) for row in originals]
    # Nearby originals are retrieval context only. They are never independent targets.
    previous = list(scope.filter(timestamp__lte=originals[0].timestamp).exclude(pk__in=targets)
                    .select_related("config").order_by("-timestamp", "-id")[:2])
    quoted_ids = set()
    for row in originals:
        data = row.raw_payload if isinstance(row.raw_payload, dict) else {}
        for container in (data, data.get("_data", {}), data.get("replyTo", {})):
            if isinstance(container, dict):
                quoted_ids.update(container[key] for key in ("quotedMessageId", "quotedStanzaID", "stanzaId") if isinstance(container.get(key), str))
    quoted = list(scope.filter(message_id__in=quoted_ids).exclude(pk__in=targets).select_related("config").order_by("id")[:10])
    contextual = list({row.pk: row for row in [*quoted, *reversed(previous)]}.values())
    sources.extend(_source(row) for row in contextual)
    counter, _ = context_runtime(cfg)
    maximum = min(cfg.entity_input_token_limit, cfg.analysis_input_token_limit or cfg.entity_input_token_limit,
                  cfg.context_window_tokens - cfg.max_completion_tokens - cfg.context_safety_tokens)
    # Packet size is measured after adding system/schema/native tool wrapping.
    packets, current = [], []
    for source in sources[:len(originals)]:
        if not source["content"]:
            # An empty original is still a target. It must receive an explicit
            # unknown/no-fact classification rather than disappear from coverage.
            candidate = current + [{**source, "range": [0, 0]}]
            inputs = {"target_ids": [row["raw_message_id"] for row in candidate], "sources": candidate}
            if current and (len(candidate) > min(10, cfg.analysis_target_message_limit)
                    or counter.count_payload(_request(cfg, OBSERVE_PROMPT, inputs, Observations(), "observe")) > maximum):
                packets.append(current)
                current = []
            current.append({**source, "range": [0, 0]})
            continue
        start = 0
        while start < len(source["content"]):
            low, high, end = start + 1, len(source["content"]), start
            while low <= high:
                mid = (low + high) // 2
                piece = {**source, "content": source["content"][start:mid], "range": [start, mid]}
                candidate = current + [piece]
                inputs = {"target_ids": list(dict.fromkeys(row["raw_message_id"] for row in candidate)), "sources": candidate}
                count = counter.count_payload(_request(cfg, OBSERVE_PROMPT, inputs, Observations(), "observe"))
                if len(candidate) <= min(10, cfg.analysis_target_message_limit) and count <= maximum:
                    end, low = mid, mid + 1
                else:
                    high = mid - 1
            if end == start:
                if current:
                    packets.append(current)
                    current = []
                    continue
                raise ProviderUnavailable("entity_atomic_source_too_large")
            if end < len(source["content"]):
                boundary = source["content"].rfind("\n", start + (end - start) // 2, end)
                if boundary > start:
                    end = boundary + 1
            current.append({**source, "content": source["content"][start:end], "range": [start, end]})
            start = end
            if start < len(source["content"]):
                packets.append(current)
                current = []
    if current:
        packets.append(current)
    if not packets or len(packets) > cfg.entity_max_calls:
        raise ProviderUnavailable("entity_stage_budget_exhausted")
    with transaction.atomic():
        owner = _owner()
        if "entity_sources" not in owner.payload:
            owner.payload = {**owner.payload, "entity_policy": POLICY, "entity_sources": sources,
                             "entity_packets": packets, "entity_config": config_fingerprint(cfg, "chat"),
                             "entity_context_ids": [row.pk for row in contextual],
                             "entity_config_id": cfg.pk, "entity_call_count": 0,
                             "entity_call_limit": cfg.entity_max_calls,
                             "entity_source_hash": _digest(sources)}
            owner.save(update_fields=["payload"])
        return owner


def _validate_snapshot(root, raw, cfg):
    from .ai_connection_checks import config_fingerprint
    if (cfg.pk != root.payload["entity_config_id"]
            or config_fingerprint(cfg, "chat") != root.payload["entity_config"]):
        raise ProviderUnavailable("entity_configuration_changed")
    sources = root.payload["entity_sources"]
    current = {row.pk: row for row in source_scope(raw).filter(pk__in=[row["raw_message_id"] for row in sources])
               .exclude(processing_state__in=["deleted", "superseded", "clarification_reply"]).select_related("config")}
    if any(row["raw_message_id"] not in current or _digest(row) != _digest(_source(current[row["raw_message_id"]])) for row in sources):
        raise ProviderUnavailable("entity_source_changed")
    if root.payload.get("requested_by_id"):
        from django.contrib.auth.models import User
        from .processing_attempts import require_reanalysis
        user = User.objects.get(pk=root.payload["requested_by_id"])
        for pk in root.payload.get("batch_ids") or [raw.pk]:
            require_reanalysis(user, current[pk])


def process(payload):
    cfg = AIService._config()
    raw = RawMessage.objects.select_related("config__team", "team", "project").get(pk=payload["raw_id"])
    root = OutboxEvent.objects.get(pk=usage_event_id.get())
    if root.payload.get("entity_published"):
        return
    root = root if "entity_sources" in root.payload else _initialize(payload, cfg)
    _validate_snapshot(root, raw, cfg)
    try:
        return _advance(root, raw, cfg)
    except YieldStage:
        _progress(root.pk)
        return {"history_continuation": True}
    except Exception:
        _progress(root.pk)
        raise


def _advance(root, raw, cfg):
    originals = root.payload["entity_sources"]
    observations, classifications = [], []
    for index, packet in enumerate(root.payload["entity_packets"]):
        ids = list(dict.fromkeys(row["raw_message_id"] for row in packet))
        # Optional preceding sources are included only when the complete request fits.
        sources = list(packet)
        counter, _ = context_runtime(cfg)
        for context in [row for row in originals if row["raw_message_id"] in root.payload.get("entity_context_ids", [])]:
            candidate = {"target_ids": ids, "sources": sources + [context]}
            if counter.count_payload(_request(cfg, OBSERVE_PROMPT, candidate, Observations(), "observe")) <= min(cfg.entity_input_token_limit, cfg.analysis_input_token_limit or cfg.entity_input_token_limit):
                sources.append(context)
        def validate_observations(value):
            if (sorted(row["raw_message_id"] for row in value["classifications"]) != sorted(ids)
                    or any(row["raw_message_id"] not in ids for row in value["observations"])):
                raise ProviderUnavailable("entity_target_coverage_invalid")
            _validate_refs(value, sources)
        value = _call(root, cfg, f"observe-{index}", OBSERVE_PROMPT,
                      {"target_ids": ids, "sources": sources}, Observations(), "observe", validate_observations)
        value = _contextualize(root, raw, cfg, index, packet, value)
        originals = root.payload["entity_sources"]
        observations.extend(value["observations"])
        classifications.extend(value["classifications"])
    facts, resolutions, threads = [], [], []
    known_threads = {}
    seen = set()
    seen_facts = set()
    observation_sources = {row.pk: row for row in source_scope(raw).filter(pk__in=[row["raw_message_id"] for row in observations])}
    observations.sort(key=lambda row: (observation_sources[row["raw_message_id"]].timestamp
        if observation_sources[row["raw_message_id"]].sent_at_known else observation_sources[row["raw_message_id"]].received_at,
        row["raw_message_id"]))
    for index, observation in enumerate(observations):
        identity = _digest(observation)
        if identity in seen:
            continue
        seen.add(identity)
        refs = observation["evidence_messages"]
        # Include only evidenced spans and metadata, never the complete chat.
        by_id = {row["raw_message_id"]: row for row in originals}
        selected = [{**by_id[ref["raw_message_id"]], "content": ref["quote"]} for ref in refs]
        validator = fact_result_serializer(observation["fact_type"])
        def validate_fact(response):
            if response["state"] != "ready":
                return
            value = response["fact"]
            if value["fact_type"] != observation["fact_type"] or value.get("evidence_message_id") not in {row["raw_message_id"] for row in selected}:
                raise ProviderUnavailable("entity_fact_source_invalid")
            _validate_refs(value, selected)
            if not any(value["evidence"] in row["content"] for row in selected if row["raw_message_id"] == value["evidence_message_id"]):
                raise ProviderUnavailable("evidence_not_in_source")
        value = _call(root, cfg, f"fact-{index}", FACT_PROMPT + KIND_PROMPT[observation["fact_type"]],
                      {"observation": observation, "sources": selected}, validator, "fact", validate_fact)
        if value["state"] == "no_fact":
            continue
        if value["state"] != "ready":
            for classification in classifications:
                if classification["raw_message_id"] == observation["raw_message_id"]:
                    classification.update(state="unknown", reason=value["reason"])
            continue
        value = value["fact"]
        identity = _digest([value["evidence_message_id"], value.get("promise_message_id"),
                            value.get("deal_contract_number"), value.get("deal_period"), fact_identity(value)])
        if identity in seen_facts:
            continue
        seen_facts.add(identity)

        def compare(stage_key, mention, candidates, coverage):
            # Resolver may fetch a specific old promise. Revalidate every added
            # source against the same team/chat and persist it before sending.
            offered_refs = mention.get("evidence_messages", []) + [ref for row in candidates for ref in row.get("evidence_messages", [])]
            extra_ids = {ref["raw_message_id"] for ref in offered_refs} - {row["raw_message_id"] for row in root.payload["entity_sources"]}
            if extra_ids:
                extras = list(source_scope(raw).filter(pk__in=extra_ids).exclude(processing_state__in=["deleted", "superseded", "clarification_reply"]).select_related("config"))
                if {row.pk for row in extras} != extra_ids:
                    raise ProviderUnavailable("context_source_unavailable")
                with transaction.atomic():
                    owner = _owner()
                    merged = {row["raw_message_id"]: row for row in owner.payload["entity_sources"]}
                    merged.update({row.pk: _source(row) for row in extras})
                    owner.payload = {**owner.payload, "entity_sources": list(merged.values())}
                    owner.save(update_fields=["payload"])
                    root.payload["entity_sources"] = owner.payload["entity_sources"]
            def validate_comparison(result):
                _validate_refs(result, root.payload["entity_sources"])
                allowed = {row["id"] for row in candidates}
                if (result["decision"] == "match" and (result["candidate_id"] not in allowed or not result["evidence_messages"])
                        or result["decision"] != "match" and result["candidate_id"] is not None):
                    raise ProviderUnavailable("entity_comparison_invalid")
            result = _call(root, cfg, f"resolve-{index}-{stage_key}", COMPARE_PROMPT,
                          {"mention": json_value(mention), "candidates": json_value(candidates), "coverage": coverage},
                          Comparison(), "compare", validate_comparison)
            return result

        from .entity_resolution import resolve
        primary = source_scope(raw).select_related("config__team", "team", "project").get(pk=value["evidence_message_id"])
        resolved = resolve(copy.deepcopy(value), primary, compare)
        value = resolved["fact"]
        extra_ids = {ref["raw_message_id"] for ref in value.get("evidence_messages", [])} - {row["raw_message_id"] for row in root.payload["entity_sources"]}
        if extra_ids:
            extras = list(source_scope(raw).filter(pk__in=extra_ids).exclude(processing_state__in=["deleted", "superseded", "clarification_reply"]).select_related("config"))
            if {row.pk for row in extras} != extra_ids:
                raise ProviderUnavailable("context_source_unavailable")
            root.payload["entity_sources"].extend(_source(row) for row in extras)
            with transaction.atomic():
                owner = _owner()
                owner.payload = {**owner.payload, "entity_sources": root.payload["entity_sources"]}
                owner.save(update_fields=["payload"])
        _validate_refs(value, root.payload["entity_sources"])
        if resolved["state"] == "duplicate":
            continue
        # Existing explicit source membership is stronger than topic similarity.
        from .models import DialogueThread, ThreadMessage
        links = ThreadMessage.objects.filter(raw_message_id__in=[ref["raw_message_id"] for ref in value.get("evidence_messages", [])],
                                              thread__source_key=root.analysis_source_key).values_list("thread_id", flat=True).distinct()
        candidates = list(DialogueThread.objects.filter(pk__in=links).exclude(state="superseded")[:2])
        topic = observation["topic"]
        thread = candidates[0] if len(candidates) == 1 else None
        if thread is None:
            import re
            from django.db.models import Q
            from .dialogue_threads import source_key
            search = Q(pk__in=[])
            for word in re.findall(r"\w+", topic)[:6]:
                if len(word) >= 4:
                    search |= Q(topic__icontains=word)
            topics = list(DialogueThread.objects.filter(source_key=source_key(raw)).filter(search)
                          .exclude(state="superseded").order_by("-updated_at", "id")[:6])
            if topics:
                offered = [{"id": row.pk, "version": row.version, "topic": row.topic, "summary": row.summary[:300]} for row in topics[:5]]
                routing = compare("topic", {"kind": "topic", "topic": topic, "summary": observation["summary"],
                    "evidence_messages": refs}, offered, {"scope": "source", "has_more": len(topics) > 5})
                if routing["decision"] == "match":
                    thread = next(row for row in topics if row.pk == routing["candidate_id"])
        if thread:
            known_threads[thread.pk] = {"id": thread.pk, "version": thread.version, "topic": thread.topic,
                                       "message_ids": list(ThreadMessage.objects.filter(thread=thread).values_list("raw_message_id", flat=True)[:10]), "commitments": []}
        key = f"e{len(threads)}"
        value["thread_key"] = key
        source_ids = {observation["raw_message_id"], value["evidence_message_id"], *[ref["raw_message_id"] for ref in value.get("evidence_messages", [])]}
        threads.append({"key": key, "thread_id": thread.pk if thread else None, "parent_key": None,
                        "topic": thread.topic if thread else topic, "summary": observation["summary"], "state": "ready",
                        "completion_reason": observation["summary"], "messages": [
                            {"raw_message_id": pk, "thought_state": "final", "relation": "discusses", "rationale": observation["summary"]} for pk in sorted(source_ids)]})
        facts.append(value)
        resolutions.append({**{k: v for k, v in resolved.items() if k != "fact"}, "evidence_message_id": value["evidence_message_id"], "thread_key": key})
    # Classifications also retain non-business and unresolved messages.
    for classification in classifications:
        pk = classification["raw_message_id"]
        if any(any(link["raw_message_id"] == pk for link in thread["messages"]) for thread in threads):
            continue
        threads.append({"key": f"e{len(threads)}", "thread_id": None, "parent_key": None,
                        "topic": classification["topic"], "summary": classification["reason"],
                        "state": classification["state"], "completion_reason": classification["reason"],
                        "messages": [{"raw_message_id": pk, "thought_state": "final" if classification["state"] == "ready" else "unknown",
                                      "relation": "discusses", "rationale": classification["reason"]}]})
    result = {"threads": threads, "facts": json_value(facts)}
    uncertain_ids = {row["raw_message_id"] for row in classifications if row["state"] == "unknown"}
    review_ids = set(uncertain_ids)
    for resolution in resolutions:
        if resolution["state"] == "review":
            review_ids.update(link["raw_message_id"] for thread in threads if thread["key"] == resolution["thread_key"] for link in thread["messages"])
    _publish(root, raw, result, resolutions, list(known_threads.values()), review_ids, uncertain_ids)
    return None


def _contextualize(root, raw, cfg, packet_index, packet, result):
    """Retrieve originals for an ambiguous target once, without reopening a chat."""
    import re
    from django.db.models import Q
    from .dialogue_threads import source_key
    from .models import DialogueThread, ThreadMessage
    result = copy.deepcopy(result)
    for classification in result["classifications"]:
        pk = classification["raw_message_id"]
        if classification["state"] != "unknown" or any(row["raw_message_id"] == pk for row in result["observations"]):
            continue
        target = next(row for row in packet if row["raw_message_id"] == pk)
        search = Q(pk__in=[])
        for word in re.findall(r"\w+", target["content"])[:8]:
            if len(word) >= 4:
                search |= Q(topic__icontains=word)
        topics = list(DialogueThread.objects.filter(source_key=source_key(raw)).exclude(state="superseded")
                      .filter(search).order_by("-updated_at", "id")[:6])
        if not topics:
            continue
        candidates = [{"id": row.pk, "topic": row.topic, "summary": row.summary[:300]} for row in topics[:5]]
        def validate_selection(answer):
            if (answer["decision"] == "match" and (answer["candidate_id"] not in {row["id"] for row in candidates} or not answer["evidence_messages"])
                    or answer["decision"] != "match" and answer["candidate_id"] is not None):
                raise ProviderUnavailable("entity_comparison_invalid")
            _validate_refs(answer, [target])
        choice = _call(root, cfg, f"context-topic-{packet_index}-{pk}", COMPARE_PROMPT,
                       {"mention": {"content": target["content"], "raw_message_id": pk}, "candidates": candidates,
                        "coverage": {"scope": "source", "has_more": len(topics) > 5}}, Comparison(), "compare", validate_selection)
        if choice["decision"] != "match":
            continue
        ids = list(ThreadMessage.objects.filter(thread_id=choice["candidate_id"]).exclude(raw_message_id=pk)
                   .order_by("-raw_message__timestamp", "-raw_message_id").values_list("raw_message_id", flat=True)[:3])
        extras = list(source_scope(raw).filter(pk__in=ids).exclude(processing_state__in=["deleted", "superseded", "clarification_reply"]).select_related("config"))
        selected = [target, *[_source(row) for row in extras]]
        counter, _ = context_runtime(cfg)
        inputs = {"target_ids": [pk], "sources": selected}
        if counter.count_payload(_request(cfg, OBSERVE_PROMPT, inputs, Observations(), "observe")) > min(cfg.entity_input_token_limit, cfg.analysis_input_token_limit or cfg.entity_input_token_limit):
            continue
        with transaction.atomic():
            owner = _owner()
            sources = {row["raw_message_id"]: row for row in owner.payload["entity_sources"]}
            sources.update({row.pk: _source(row) for row in extras})
            root.payload["entity_sources"] = list(sources.values())
            owner.payload = {**owner.payload, "entity_sources": root.payload["entity_sources"]}
            owner.save(update_fields=["payload"])
        def validate_observation(value):
            if [row["raw_message_id"] for row in value["classifications"]] != [pk] or any(row["raw_message_id"] != pk for row in value["observations"]):
                raise ProviderUnavailable("entity_target_coverage_invalid")
            _validate_refs(value, selected)
        expanded = _call(root, cfg, f"context-observe-{packet_index}-{pk}", OBSERVE_PROMPT,
                         inputs, Observations(), "observe", validate_observation)
        classification.update(expanded["classifications"][0])
        result["observations"].extend(expanded["observations"])
    return result


def _progress(root_id):
    root = OutboxEvent.objects.get(pk=root_id)
    steps = list(OutboxEvent.objects.filter(event_type="entity_checkpoint", payload__root_event_id=root_id).order_by("id"))
    usage_rows = ProviderUsage.objects.filter(outbox_event_id__in=[row.pk for row in steps], operation="chat").values("outbox_event_id").annotate(
        calls=Count("id"), input_known=Count("input_tokens"), output_known=Count("output_tokens"),
        input_tokens=Sum("input_tokens"), output_tokens=Sum("output_tokens"), duration=Sum("duration_ms"))
    actual_usage = {row["outbox_event_id"]: row for row in usage_rows}
    stages = []
    for step in steps:
        actual = actual_usage.get(step.pk, {})
        calls = actual.get("calls", 0)
        reasoning = [row["reasoning_tokens"] for row in step.payload.get("attempt_usage", []) if type(row.get("reasoning_tokens")) is int]
        stages.append({"stage": step.payload["stage"], "state": step.state, "attempts": step.attempt_count,
            "error": step.error_code, "metrics": step.payload["metrics"], "calls": calls,
            "usage": {"prompt_tokens": actual.get("input_tokens") if actual.get("input_known") == calls else None,
                      "completion_tokens": actual.get("output_tokens") if actual.get("output_known") == calls else None},
            "duration_ms": actual.get("duration"),
            "reasoning_tokens": sum(reasoning) if calls and len(reasoning) == calls else None})
    metrics = {"policy": POLICY, "current_stage": root.payload.get("entity_current_stage"),
               "state": root.state, "error": root.error_code, "calls": sum(row.get("calls", 0) for row in stages),
               "attempted_calls": root.payload.get("entity_call_count", 0),
               "completed_steps": sum(row.state == "done" for row in steps), "stages": stages}
    for entry in root.payload.get("trace_ids") or [{"trace_id": root.payload["trace_id"]}]:
        trace = MessageProcessingTrace.objects.get(pk=entry["trace_id"])
        if root.event_type == "entity_reconcile":
            trace.context_metadata.setdefault("entity_reconciliations", {})[str(root.payload["candidate_id"])] = metrics
        else:
            trace.context_metadata["entity_pipeline"] = metrics
            if root.state in {"pending", "enqueued", "processing"}:
                trace.result_summary = f"Этап {metrics['current_stage']}; сохранено шагов: {metrics['completed_steps']}, вызовов: {metrics['calls']}."
        trace.save(update_fields=["context_metadata", "result_summary"])


@transaction.atomic
def _publish(root, raw, result, resolutions, known_threads, review_ids, uncertain_ids):
    owner = _owner()
    if owner.payload.get("entity_published"):
        return
    from .dialogue_threads import lock_source
    lock_source(raw)
    list(source_scope(raw).filter(pk__in=[row["raw_message_id"] for row in owner.payload["entity_sources"]])
         .order_by("id").select_for_update().values_list("id", flat=True))
    _validate_snapshot(owner, raw, AIService._config())
    _progress(root.pk)
    originals = root.payload["entity_sources"]
    trace = MessageProcessingTrace.objects.get(pk=root.payload["trace_id"])
    cfg = AIService._config()
    context = [{**row, "partial": False} for row in originals if row["raw_message_id"] != raw.pk]
    inputs = {"content": raw.content, "target_message_id": raw.pk, "context": context,
              "known_projects": [], "known_threads": known_threads, "batch_message_ids": root.payload.get("batch_ids") or [raw.pk]}
    envelope = {"model": cfg.chat_model_name, "messages": [{"role": "user", "content": canonical_json({k:v for k,v in inputs.items() if k != 'context'})}]}
    trace.earlier_messages_context, trace.earlier_messages_count = context, len(context)
    trace.model_version = cfg.chat_model_name
    trace.context_metadata.update(entity_policy=POLICY, entity_resolutions=resolutions,
        entity_root_event_id=root.pk,
        request_state="not_sent", request_envelope=envelope, api_format=cfg.chat_api_format,
        provider_url=AIService.effective_chat_provider_url(cfg), effective_provider_url=AIService.effective_chat_provider_url(cfg),
        batch_message_ids=root.payload.get("batch_ids") or [raw.pk], known_threads=known_threads,
        evidence_ranges={str(row["raw_message_id"]): len(row["content"]) for row in originals},
        snapshot_max_id=max(row["raw_message_id"] for row in originals))
    from .live_batching import work_version
    trace.context_metadata["entity_processing_version"] = root.payload.get("processing_version") or work_version(cfg, True)
    trace.context_metadata["payload_sha256"] = payload_hash({**envelope, "messages": [{"role": "user", "content": canonical_json(inputs)}]})
    # Old packet checkpoints must never be interpreted as the new wire contract.
    trace.context_metadata.pop("context_window", None)
    trace.context_metadata["full_history_policy"] = None
    trace.context_metadata["analysis_policy"] = POLICY
    trace.save()
    from .pipeline import extract_message
    # The persistence/FactDecision path is shared; no legacy AI pass is invoked.
    extract_message.__wrapped__(raw.pk, trace_id=trace.pk, batch_ids=root.payload.get("batch_ids"),
        trace_ids=root.payload.get("trace_ids"), requested_by_id=root.payload.get("requested_by_id"),
        result_override=(result, {}, {"entity_policy": POLICY}))
    targets = set(root.payload.get("batch_ids") or [raw.pk])
    review_ids &= targets
    RawMessage.objects.filter(pk__in=review_ids).update(processing_state="needs_review")
    entries = root.payload.get("trace_ids") or [{"trace_id": trace.pk}]
    for member in MessageProcessingTrace.objects.filter(pk__in=[row["trace_id"] for row in entries]):
        if member.raw_message_id in review_ids:
            member.context_metadata["request_state"] = "needs_review"
            member.result_summary = "Этапы сохранены. Недостаточно доказательств для автоматического применения; требуется проверка."
        member.context_metadata["entity_outcome"] = "review" if member.raw_message_id in review_ids else "success"
        member.context_metadata["entity_source_uncertain"] = member.raw_message_id in uncertain_ids
        member.save(update_fields=["context_metadata", "result_summary"])
    owner.payload = {**owner.payload, "entity_published": True, "entity_current_stage": "persist_facts"}
    owner.save(update_fields=["payload"])
