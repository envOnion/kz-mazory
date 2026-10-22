"""Durable extraction attempts: bounded full history, immutable audit, reviewed facts."""

import copy
import json
import re
import uuid
import hashlib
import time
import logging

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from .ai_service import AIService, usage_event_id, outbox_claim
from .context_tokens import canonical_json, extraction_input, payload_hash
from .deduplication import normalize_deal_name
from .facts import FactSchema, fact_identity, json_value, same_commitment_origin, normalize_fact_fields, artifact_subject
from .message_context import build_context, source_scope
from .message_time import source_time
from .models import (
    AISettings,
    AuditEvent,
    FactCandidate,
    FactEvidence,
    OutboxEvent,
    Project,
    ProviderUsage,
    RawMessage,
    UserProfile,
)
from .processing_attempts import require_reanalysis, reserve_attempt
from .providers import ProviderUnavailable
from .context_window import bounded_operation, current_window

logger = logging.getLogger(__name__)


def _source_quote(quote, content):
    if quote in content:
        return quote
    # Models often fold newlines/NBSP into spaces. Match literal text separated
    # only by whitespace, then store the exact original substring as evidence.
    pattern = re.compile(r"\s+".join(re.escape(part) for part in quote.split()))
    match = pattern.search(content)
    if not match or pattern.search(content, match.start() + 1):
        raise ProviderUnavailable(
            "evidence_not_in_source",
            diagnostics={"evidence_match": "ambiguous" if match else "not_found"},
        )
    return match.group()


def _source_content_for_trace(content, trace=None):
    """Use the same source projection as the full-history request."""
    if trace is not None and trace.context_metadata.get("full_history"):
        from .full_history import CANONICAL_EVIDENCE_POLICY
        from .qwen_profile import MODEL
        if (trace.context_metadata.get("evidence_text_policy") == CANONICAL_EVIDENCE_POLICY
                and trace.model_version != MODEL):
            from .plain_text import plain_text
            return plain_text(content)
    return content


def _source_quote_matcher(trace=None):
    if trace is None or not trace.context_metadata.get("full_history"):
        return _source_quote
    from .qwen_profile import MODEL
    if trace.model_version == MODEL:
        return _source_quote
    return lambda quote, content: _source_quote(quote, _source_content_for_trace(content, trace))


def _facts(result, raw, diagnostics=None, snapshot_id=None, threaded=False, target_ids=None, trace=None):
    # A missing optional value and an explicit null both mean unknown. Required
    # values, enums and evidence still go through the full serializer validation.
    normalized = normalize_fact_fields(result['facts'])
    schema = FactSchema(data=normalized, many=True)
    schema.is_valid(raise_exception=True)
    quote_match = _source_quote_matcher(trace)
    accepted_facts = []
    for index, fact in enumerate(schema.validated_data):
        if fact['fact_type']=='project' and artifact_subject(fact['object_name']):
            if diagnostics is not None:
                diagnostics.setdefault('excluded_artifact_subjects', []).append(index)
            continue
        if fact['fact_type']=='project' and (re.search(r'\S\s*;\s*\S', fact['object_name']) or re.match(r'^(?:запрос|просьба|напоминан\w*)\b', fact['current_action'], re.I)):
            if diagnostics is not None:
                diagnostics.setdefault('excluded_request_or_object_list', []).append(index)
            continue
        if target_ids and fact.get('evidence_message_id') not in target_ids and not (fact['fact_type']=='commitment' and any(ref['raw_message_id'] in target_ids for ref in fact['evidence_messages'])):
            if diagnostics is not None:
                diagnostics.setdefault('excluded_context_facts', []).append(index)
            continue
        if fact['fact_type']=='project' and (re.search(r"^(?:общий\s+итог|(?:все\s+)?действующие\s+проекты|все\s+проекты|проекты$|портфель|таблица|отч[её]т)", fact['object_name'].strip(), re.I) or (not fact['object_name'].strip() and re.search(r"таблиц|портфел|общий\s+итог", fact['current_action'], re.I))):
            if diagnostics is not None:
                diagnostics.setdefault('excluded_aggregate_subjects', []).append(index)
            continue
        if fact['fact_type']=='commitment':
            promise_text = ' '.join(ref['quote'] for ref in fact['evidence_messages'] if ref['role']=='promise') or fact['evidence']
            completed = re.search(r"подписан|выставил|отправил|поступил|получил|завершил|перенесл|согласован", promise_text, re.I)
            future = re.search(r"завтра|сегодня\s+\w*(?:чу|лю|ю)|обещ|будет|скину|отправлю|подготовлю|сделаю|закончу|вышлю|предоставлю|планирую", promise_text, re.I)
            expectation = fact['commitment_status']=='pending' and (fact['assignment_kind']=='reported_promise' or bool(re.search(r"должен\s+вернуться", promise_text, re.I))) and not re.search(r"обещ|пообещ|сказал.*(?:сдела|отправ|подготов)|уәде", promise_text, re.I)
            if (completed and not future and not fact.get('commitment_id')) or expectation:
                if diagnostics is not None:
                    diagnostics.setdefault('excluded_unproven_promises', []).append(index)
                continue
        if fact['fact_type']=='project' and re.search(r"(?:оплат|средств|деньг).*(?:поступ|получ)|(?:поступ|получ).*(?:оплат|средств|деньг)", fact['current_action'], re.I) and not re.search(r"договор|контракт|\bКП\b|мощност|оборудован|МВт", fact['current_action'], re.I):
            if diagnostics is not None:
                diagnostics.setdefault('excluded_financial_project_duplicates', []).append(index)
            continue
        if fact["fact_type"] == "payment" and fact["payment_kind"] == "increment":
            from .focused_extraction import INVOICE_ISSUED, RECEIPT_REPORTED
            if INVOICE_ISSUED.search(fact['evidence']) and not RECEIPT_REPORTED.search(fact['evidence']):
                if diagnostics is not None:
                    diagnostics.setdefault('excluded_invoice_as_receipt', []).append(index)
                continue
            if re.search(
                r"не\s+оплат|оплатим|төленбеді|төлейміз",
                fact["evidence"],
                re.IGNORECASE,
            ):
                raise ProviderUnavailable("payment_evidence_contradiction")
            if re.search(
                r"всего\s+оплачено|итого\s+оплачено|накопительн",
                fact["evidence"],
                re.IGNORECASE,
            ):
                fact["payment_kind"] = "cumulative"
                fact["uncertainties"].append(
                    "Накопительный итог не является новым платежом."
                )
        evidence_raw = raw
        if threaded:
            evidence_raw = source_scope(raw).get(pk=fact["evidence_message_id"], id__lte=snapshot_id)
        quote = quote_match(fact["evidence"], evidence_raw.content)
        if quote != fact["evidence"] and diagnostics is not None:
            diagnostics.setdefault("source_whitespace_restored", []).append(index)
        fact["evidence"] = quote
        if fact['fact_type'] in ('project','payment') and not any(ref['raw_message_id']==fact['evidence_message_id'] and ref['quote']==quote for ref in fact['evidence_messages']):
            # The separately supplied primary quote has just passed the same
            # source-scope and literal checks. Retain it in the evidence ledger.
            fact['evidence_messages'].append({'raw_message_id':fact['evidence_message_id'],'quote':quote,'role':'identity'})
            if diagnostics is not None:
                diagnostics.setdefault('primary_quote_retained',[]).append(index)
        if fact["fact_type"] == "commitment":
            from .commitment_evidence import validate_commitment
            promise_raw = source_scope(raw).filter(pk=fact.get("promise_message_id"), id__lte=snapshot_id).first() if threaded else raw
            if not promise_raw:
                raise ProviderUnavailable("commitment_promise_evidence_unavailable")
            if not validate_commitment(fact, promise_raw, snapshot_id or raw.id, quote_match, threaded=threaded, body_text=_source_content_for_trace(promise_raw.content, trace)):
                continue
        def is_receipt_ack(message):
            text = _source_content_for_trace(message.content, trace)[:120]
            return bool(re.search(r"\b(?:принято|принял|подтверждаю|верно)\b", text, re.I)) and not re.search(r"\b(?:нов\w+|ещ[её]|втор\w+|дополнительн\w+)\b", text, re.I)
        duplicate = next((previous for previous in accepted_facts if previous['fact_type']==fact['fact_type'] and (
            previous['thread_key']==fact['thread_key'] and same_commitment_origin(previous,fact) if fact['fact_type']=='commitment' else
            (fact_identity(previous)==fact_identity(fact) or (
                fact['fact_type']=='payment' and previous['thread_key']==fact['thread_key']
                and (not previous['object_name'] or not fact['object_name'])
                and bool(previous['company_name'])
                and normalize_deal_name(previous['company_name'])==normalize_deal_name(fact['company_name'])
                and fact_identity({**previous,'object_name':''})==fact_identity({**fact,'object_name':''})
            )) and (previous['evidence_message_id']==fact['evidence_message_id'] or
                (fact['fact_type']=='payment' and (is_receipt_ack(evidence_raw) or is_receipt_ack(source_scope(raw).get(pk=previous['evidence_message_id'])))))
        )),None)
        if duplicate is not None:
            if fact['fact_type']=='commitment' and fact.get('commitment_status') in ('fulfilled','cancelled'):
                accepted_facts.remove(duplicate)
            elif fact['fact_type']=='payment' and not is_receipt_ack(evidence_raw) and duplicate['evidence_message_id']!=fact['evidence_message_id']:
                accepted_facts.remove(duplicate)
            else:
                continue
        accepted_facts.append(fact)
    return accepted_facts



def _preflight_thread_completion(result, raw, trace):
    """Reject only completion that focused recovery cannot repair."""
    from .dialogue_threads import normalize_themes, prepare_themes

    if not isinstance(result, dict):
        return
    try:
        themes = normalize_themes(copy.deepcopy(result.get('threads')))
    except ProviderUnavailable:
        return
    if not isinstance(themes, list) or not any(
        isinstance(theme, dict) and theme.get('state') == 'ready'
        and isinstance(theme.get('messages'), list)
        and (not theme.get('completion_reason') or not any(
            isinstance(link, dict) and link.get('thought_state') == 'final'
            for link in theme['messages']))
        for theme in themes
    ):
        return
    work = copy.copy(trace)
    work.context_metadata = copy.deepcopy(trace.context_metadata)
    try:
        prepare_themes(copy.deepcopy(result), raw, work)
    except ProviderUnavailable as exc:
        if str(exc) == 'thread_completion_unproven':
            raise
    except ValidationError:
        pass


def _main_response_cache(trace, payload):
    """Reuse only a native-success response from this immutable attempt."""
    cache = trace.context_metadata.get('main_response_cache')
    if cache is None:
        return None
    if (not isinstance(cache, dict) or cache.get('version') != 'native-main-v1'
            or cache.get('trace_id') != trace.pk
            or cache.get('payload_sha256') != payload_hash(payload)):
        raise ProviderUnavailable('context_snapshot_mismatch')
    value = cache.get('value')
    if (not isinstance(value, list) or len(value) != 3
            or not all(isinstance(item, dict) for item in value)
            or not isinstance(value[0].get('facts'), list)
            or cache.get('response_sha256') != hashlib.sha256(canonical_json(value).encode()).hexdigest()):
        raise ProviderUnavailable('context_snapshot_mismatch')
    return tuple(copy.deepcopy(value))


def _restore_request(trace, cfg=None):
    try:
        payload = copy.deepcopy(trace.context_metadata["request_envelope"])
        user_message = _extraction_user_message(payload)
        fixed = json.loads(user_message["content"])
        if not isinstance(fixed, dict):
            raise TypeError()
        target_ids = {row["raw_message_id"] for row in fixed.get("target_messages", [])}
        fixed["context"] = [row for row in trace.earlier_messages_context if row["raw_message_id"] not in target_ids]
        serialize = (
            extraction_input
            if trace.context_metadata.get("input_serialization") == "target-last-v1"
            else canonical_json
        )
        user_message["content"] = serialize(fixed)
        expected_hash = trace.context_metadata["payload_sha256"]
    except (KeyError, TypeError, ValueError):
        raise ProviderUnavailable("context_snapshot_mismatch") from None
    if payload_hash(payload) != expected_hash:
        raise ProviderUnavailable("context_snapshot_mismatch")
    from .free_fallback import check_frozen
    check_frozen(payload, trace.context_metadata, trace.model_version, cfg)
    return payload


def _extraction_user_message(payload):
    try:
        messages = payload["messages"]
        matches = [
            message
            for message in messages
            if isinstance(message, dict)
            and message.get("role") == "user"
            and isinstance(message.get("content"), str)
        ]
    except (KeyError, TypeError):
        matches = []
    if len(matches) != 1:
        raise ProviderUnavailable("context_snapshot_mismatch")
    return matches[0]


@bounded_operation
def extract_message(raw_id, trace_id=None, requested_by_id=None, commitment_refresh=False, batch_ids=None, reanalyze=False, trace_ids=None, segment_range=None, defer_result=False, result_override=None, full_state=None):
    preparation_started = time.monotonic()
    explicit = trace_id is not None or reanalyze
    with transaction.atomic():
        raw = (
            RawMessage.objects.select_for_update(of=("self",))
            .select_related("config__team", "team", "project")
            .get(pk=raw_id)
        )
        if raw.processing_state == 'clarification_reply' or raw.processed and not explicit:
            return
        if trace_id is not None:
            trace = raw.traces.get(pk=trace_id)
        else:
            operation = (
                f"outbox:{usage_event_id.get()}"
                if usage_event_id.get()
                else f"direct:{uuid.uuid4()}"
            )
            trace = reserve_attempt(raw, operation)
        if trace.status == "success":
            return
    try:
        source_scope(raw)
        if trace.context_metadata.get("replace_unsent"):
            from .replay_replacement import guard,LABELS
            code=next((reason for member in source_scope(raw).filter(pk__in=batch_ids or [raw.id]) if (reason:=guard(member))),None)
            if code:
                if len(batch_ids or [raw.id])>1:raise ProviderUnavailable(code)
                from .models import HistoryAnalysisItem
                trace.result_summary=LABELS[code];trace.error_code=code;trace.status="warning";trace.save()
                HistoryAnalysisItem.objects.filter(trace=trace).update(state="succeeded" if code=="skipped_crm_delivered" else "blocked",disposition=code if code=="skipped_crm_delivered" else "blocked",reason_code=code,reason_description=LABELS[code],updated_at=timezone.now())
                return
        if commitment_refresh and trace.context_metadata.get("request_state") == "not_sent":
            from django.db.models import Max
            trace.context_metadata["snapshot_max_id"] = source_scope(raw).aggregate(last=Max("id"))["last"] or raw.id
        if requested_by_id:
            try:
                require_reanalysis(User.objects.get(pk=requested_by_id), raw)
            except (User.DoesNotExist, PermissionDenied) as exc:
                raise ProviderUnavailable("reanalysis_access_revoked") from exc
        cfg = AIService._config()
        from .free_fallback import with_policy, policy_metadata, models as fallback_models
        cfg = with_policy(cfg, trace.context_metadata)
        packet = trace.context_metadata.get("analysis_policy") == "history-packets-v1"
        target_traces = {raw.id: trace}
        if packet:
            from .models import MessageProcessingTrace
            target_traces.update({entry["raw_id"]: MessageProcessingTrace.objects.get(pk=entry["trace_id"], raw_message_id=entry["raw_id"]) for entry in (trace_ids or []) if entry["raw_id"] != raw.id})
            cfg = copy.copy(cfg)
            limits = trace.context_metadata.get("analysis_limits", {})
            cfg.analysis_policy = "history-packets-v1"
            cfg.live_batch_policy = trace.context_metadata.get('live_batch_policy')
            cfg.full_history_policy = trace.context_metadata.get('full_history_policy')
            cfg.analysis_chunk_input_limit = trace.context_metadata.get('analysis_chunk_input_limit')
            if cfg.full_history_policy == 'thread-context-v2':
                from .processing_attempts import ANALYSIS_CONFIG_FIELDS
                transport_fields = {'chat_model_name', 'chat_api_format', 'chat_provider_url',
                                    'context_window_tokens', 'tokenizer_id', 'tokenizer_revision'}
                changed = [name for name in ANALYSIS_CONFIG_FIELDS if name in limits
                           and name not in transport_fields and limits[name] != getattr(cfg, name)]
                if changed:
                    raise ProviderUnavailable('context_configuration_changed', diagnostics={'changed_fields':changed})
            cfg.analysis_repair_reason = trace.context_metadata.get("repair_reason")
            cfg.analysis_input_token_limit = limits.get("analysis_input_token_limit", cfg.analysis_input_token_limit)
            cfg.max_completion_tokens = min(cfg.max_completion_tokens, limits.get("analysis_output_token_limit", cfg.analysis_output_token_limit))
            if cfg.full_history_policy == 'thread-context-v2' and not defer_result and result_override is None:
                raise ProviderUnavailable('analysis_claim_expired')
        from .free_fallback import local_runtime, runtime_metadata
        cfg = local_runtime(cfg)
        api_format = getattr(cfg, "chat_api_format", "openai_compatible")
        effective_provider_url = AIService.effective_chat_provider_url(cfg)
        from .qwen_profile import enabled as qwen_enabled, attach_schema, PROFILE
        if trace.context_metadata.get("request_state") == "sent":
            raise ProviderUnavailable("context_request_uncertain")
        if "request_envelope" in trace.context_metadata:
            snapshot_format = trace.context_metadata.get(
                "api_format", "openai_compatible"
            )
            snapshot_url = trace.context_metadata.get(
                "effective_provider_url", trace.context_metadata.get("provider_url")
            )
            if not isinstance(snapshot_url, str):
                raise ProviderUnavailable("context_snapshot_mismatch")
            route_changed = (
                cfg.chat_model_name != trace.model_version
                or api_format != snapshot_format
                or effective_provider_url.rstrip("/") != snapshot_url.rstrip("/")
            )
            if route_changed:
                if trace.context_metadata.get("request_state") == "sent":
                    raise ProviderUnavailable("context_request_uncertain")
                source_hash = trace.context_metadata.get("full_history", {}).get("source_hash")
                payload_sha256 = trace.context_metadata.get("payload_sha256")
                if (not isinstance(source_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", source_hash)
                        or not isinstance(payload_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", payload_sha256)):
                    raise ProviderUnavailable("context_snapshot_mismatch")
                old = AISettings.objects.filter(
                    chat_model_name=trace.model_version,
                    chat_api_format=snapshot_format,
                    chat_provider_url=snapshot_url,
                ).order_by("pk").first()
                transition = {
                    "version": 1,
                    "from_config_id": old.pk if old else None,
                    "to_primary_config_id": cfg.pk,
                    "source_hash": source_hash,
                    "request_sha256": payload_sha256,
                }
                if trace.context_metadata.get("provider_route_transition") != transition:
                    AuditEvent.objects.create(
                        target_type="MessageProcessingTrace", target_id=trace.pk,
                        action="ai.provider_route_transition", before_after=transition,
                    )
                    trace.context_metadata["provider_route_transition"] = transition
                    trace.save(update_fields=["context_metadata"])
            elif qwen_enabled(cfg) and trace.context_metadata.get('provider_profile') != PROFILE:
                raise ProviderUnavailable("context_configuration_changed")
            if 'free_fallback_policy' in trace.context_metadata and cfg.chat_model_name == "openrouter/free":
                from .context_tokens import context_runtime
                _, endpoint = context_runtime(cfg)
                fallback_models(cfg, endpoint)
            payload = _restore_request(trace, cfg)
        else:
            team = raw.config.team if raw.config_id else raw.team
            known = json_value(
                list(
                    Project.objects.filter(team=team, archived=False).filter(
                        Q(is_verified=True) | Q(identity_confirmed=True)
                    )
                    .order_by("id")
                    .values("id", "name", "company__name")[:100]
                )
            )
            # Live arrivals are individual targets. Explicit imported packets
            # supply their own targets within the same bounded source window.
            if current_window() is not None:
                trace.context_metadata['context_window'] = copy.deepcopy(current_window())
            context, metadata, payload = build_context(
                raw, cfg, known, trace.context_metadata["snapshot_max_id"], include_following=True, full_state=full_state, **({"batch_ids": batch_ids} if batch_ids else {}), **({"target_content":raw.content[segment_range[0]:segment_range[1]]} if segment_range else {})
            )
            metadata.update(policy_metadata(trace.context_metadata))
            metadata.update(runtime_metadata(cfg))
            if '_ollama_num_ctx' in payload or qwen_enabled(cfg):
                from .extraction_schema import extraction_schema, WIRE_VERSION
                user = _extraction_user_message(payload)
                inputs = json.loads(user['content'])
                if trace.context_metadata.get('required_thread_keys'):
                    inputs['required_thread_keys'] = trace.context_metadata['required_thread_keys']
                    metadata['required_thread_keys'] = inputs['required_thread_keys']
                if qwen_enabled(cfg):
                    payload = attach_schema(payload, extraction_schema(inputs), inputs)
                    metadata['provider_profile'] = PROFILE
                else:
                    payload['_ollama_format'] = extraction_schema(inputs)
                metadata['payload_sha256'] = payload_hash(payload)
                metadata['wire_schema_version'] = WIRE_VERSION
            envelope = copy.deepcopy(payload)
            user_message = _extraction_user_message(envelope)
            fixed = json.loads(user_message["content"])
            fixed.pop("context")
            user_message["content"] = canonical_json(fixed)
            metadata.update(
                request_envelope=envelope,
                provider_url=effective_provider_url,
                effective_provider_url=effective_provider_url,
                api_format=api_format,
                context_window=copy.deepcopy(current_window()),
            )
            if packet:
                metadata.update(analysis_policy="history-packets-v1", analysis_limits=limits,
                                full_history_policy=cfg.full_history_policy,
                                live_batch_policy=cfg.live_batch_policy,
                                retry=trace.context_metadata.get("retry", {}), repair_reason=cfg.analysis_repair_reason,
                                output_retry_generation=trace.context_metadata.get('output_retry_generation', 0))
            metadata.setdefault("snapshot_max_id", trace.context_metadata["snapshot_max_id"])
            if trace.context_metadata.get("replace_unsent"):
                metadata["replace_unsent"] = True
            trace.earlier_messages_context, trace.earlier_messages_count = (
                context,
                len(context),
            )
            trace.context_metadata, trace.model_version = metadata, cfg.chat_model_name
        if not trace.context_metadata.get('full_history', {}).get('ranges') and not trace.context_metadata.get('evidence_ranges'):
            offered = json.loads(_extraction_user_message(payload)['content'])
            sources = [{'raw_message_id': offered['target_message_id'], 'content': offered['content']}]
            sources.extend(offered.get('target_messages', []))
            sources.extend(item for item in offered.get('context', []) if not item.get('partial'))
            trace.context_metadata['evidence_ranges'] = {str(item['raw_message_id']): len(item['content']) for item in sources}
        cache_enabled = (getattr(cfg, 'full_history_policy', None) == 'thread-context-v2'
                         or bool(trace.context_metadata.get('live_batch_policy')))
        cached_response = _main_response_cache(trace, payload) if cache_enabled and result_override is None else None
        trace.context_metadata["request_state"] = "responded" if cached_response else "sent"
        trace.context_metadata.setdefault("timings_ms", {})["prepare"] = round((time.monotonic()-preparation_started)*1000)
        trace.error_code = ""
        trace.result_summary = ("Продолжается проверка сохранённого ответа AI." if cached_response
                                else "Запрос с сохранённой историей отправлен в AI.")
        trace.save()
        if packet:
            for member in target_traces.values():
                if member.pk != trace.pk:
                    member.context_metadata = {**copy.deepcopy(trace.context_metadata), "packet_anchor_id": raw.id}
                    member.earlier_messages_context, member.earlier_messages_count = context if "context" in locals() else trace.earlier_messages_context, trace.earlier_messages_count
                    member.model_version = trace.model_version
                    member.save()
        provider_started = time.monotonic()
        result, usage, diagnostics = result_override or cached_response or AIService.analyze_payload(
            payload,
            trace.context_metadata.get("effective_provider_url")
            or trace.context_metadata.get("provider_url"),
            expected_api_format=api_format,
        )
        if trace.context_metadata.get("provider_route_transition"):
            diagnostics["provider_route_transition"] = trace.context_metadata["provider_route_transition"]
        trace.context_metadata["request_state"] = "responded"
        trace.context_metadata["timings_ms"]["provider"] = round((time.monotonic()-provider_started)*1000)
        if cache_enabled and result_override is None and cached_response is None:
            value = copy.deepcopy([result, usage, diagnostics])
            trace.context_metadata['main_response_cache'] = {
                'version': 'native-main-v1', 'trace_id': trace.pk,
                'payload_sha256': payload_hash(payload), 'value': value,
                'response_sha256': hashlib.sha256(canonical_json(value).encode()).hexdigest(),
                'provider_duration_ms': trace.context_metadata['timings_ms']['provider'],
            }
        if cached_response is not None:
            diagnostics['main_response_cache_reused'] = True
        validation_started = time.monotonic()
        trace.context_metadata["response_diagnostics"] = diagnostics
        trace.context_metadata['provider_result'] = copy.deepcopy(result)
        trace.save(update_fields=['context_metadata'])
        from .full_history import restore_continuity_topics
        result, restored_topics = restore_continuity_topics(result, json.loads(_extraction_user_message(payload)['content']))
        if restored_topics:
            diagnostics.setdefault('schema_repairs', []).append('unchanged_prior_topic_restored')
            diagnostics['restored_topic_keys'] = restored_topics
        value = (
            usage.get("prompt_tokens", usage.get("input_tokens"))
            if isinstance(usage, dict)
            else None
        )
        trace.context_metadata["input_tokens_actual"] = (
            value if type(value) is int and value >= 0 else None
        )
        from .dialogue_threads import prepare_themes, ready_facts, persist_themes, lock_source
        if cache_enabled and result_override is None:
            _preflight_thread_completion(result, raw, trace)
        from .gpt_sol import enabled as sol_enabled, normalize_grounded_fields, POLICY as SOL_POLICY
        from .focused_extraction import recover
        defer_focused = (defer_result and sol_enabled(cfg)
                         and trace.context_metadata.get('full_history_policy') == 'thread-context-v2'
                         and json.loads(_extraction_user_message(payload)['content']).get('analysis_transport') == SOL_POLICY)
        trace.context_metadata['focused_recovery_deferred'] = defer_focused
        if not defer_focused:
            recover(result, raw, trace, cfg)
        # Bounded packets also carry evidence_ranges. Validate their literal
        # sources and complete source links after any focused extraction.
        from .full_history import validate_evidence
        validate_evidence(result, raw, trace)
        if sol_enabled(cfg):
            normalize_grounded_fields(result, diagnostics)
        themes = prepare_themes(result, raw, trace)
        targets = set(trace.context_metadata.get('batch_message_ids') or [raw.id])
        facts = _facts(ready_facts(result, themes), raw, diagnostics, trace.context_metadata["snapshot_max_id"], threaded=True, target_ids=targets, trace=trace)
        if not defer_result:
            from .focused_extraction import reconcile_promises
            facts = reconcile_promises(facts,raw,trace,cfg)
            facts = _facts({'facts':json_value(facts)},raw,diagnostics,trace.context_metadata['snapshot_max_id'],threaded=True,target_ids=targets,trace=trace)
        if defer_result:
            trace.context_metadata.update(request_state="segment_validated", segment_result=result,
                                          segment_usage=usage, segment_diagnostics=diagnostics, segment_range=segment_range)
            trace.ai_extracted_facts = json_value({"facts":facts})
            if trace.context_metadata.get('full_history'):
                progress = trace.context_metadata['full_history']
                progress['validated_read'] = progress['read']
                trace.result_summary = f"Прочитано {progress['read']}/{progress['total']}. Промежуточный результат сохранён; публикация после итоговой сверки."
            trace.save()
            return (result, usage, diagnostics)
        from .commitment_resolution import resolve_remaining
        resolved = resolve_remaining(facts, raw, cfg, trace)
        facts = _facts({"facts": json_value(resolved)}, raw, diagnostics, trace.context_metadata["snapshot_max_id"], threaded=True, target_ids=targets, trace=trace)
        if packet:
            # Only add closing sources after their quote, source and chronology
            # have passed the same commitment validation as the main request.
            for fact in facts:
                if fact["fact_type"] == "commitment" and fact["commitment_status"] == "fulfilled":
                    theme = themes[fact["thread_key"]]
                    linked = {row["raw_message_id"] for row in theme["messages"]}
                    for ref in fact["evidence_messages"]:
                        if ref["role"] == "fulfillment" and ref["raw_message_id"] not in linked:
                            theme["messages"].append({"raw_message_id":ref["raw_message_id"], "thought_state":"final", "relation":"fulfills", "rationale":"Проверенное подтверждение выполнения обязательства."})
                            linked.add(ref["raw_message_id"])
        trace.context_metadata["timings_ms"]["validate"] = round((time.monotonic()-validation_started)*1000)
        persistence_started = time.monotonic()
        with transaction.atomic():
            if usage_event_id.get() and outbox_claim.get():
                owner = OutboxEvent.objects.select_for_update().get(pk=usage_event_id.get())
                if owner.state != "processing" or owner.payload.get("claim_generation") != outbox_claim.get() or owner.payload.get('history_cancelled'):
                    raise ProviderUnavailable("analysis_claim_expired")
            lock_source(raw)
            locked = (
                RawMessage.objects.select_for_update(of=("self",))
                .select_related("config__team", "team", "project")
                .get(pk=raw_id)
            )
            source_scope(locked)
            protected_ids = {locked.id, *(batch_ids or []), *[item['raw_message_id'] for item in trace.earlier_messages_context]}
            protected_ids.update(trace.context_metadata.get('batch_message_ids', []))
            protected_ids.update(link['raw_message_id'] for theme in themes.values() for link in theme['messages'])
            protected_ids.update(fact.get('evidence_message_id') for fact in facts)
            protected_ids.update(ref['raw_message_id'] for fact in facts for ref in fact.get('evidence_messages', []))
            if source_scope(locked).filter(pk__in=protected_ids, processing_state='clarification_reply').exists():
                trace.status, trace.result_summary = 'warning', 'Сообщение сохранено как ответ на уточнение; машинные факты не применены.'
                trace.context_metadata['request_state'] = 'superseded'
                trace.save()
                return
            if full_state and full_state.get("routing_thread_id"):
                from .models import DialogueThread
                routing_thread = DialogueThread.objects.select_for_update().get(pk=full_state["routing_thread_id"])
                if (routing_thread.routing_generation != full_state["routing_generation"] or
                    routing_thread.routing_descriptor_version != trace.context_metadata["routing_descriptor_version"]):
                    raise ProviderUnavailable("routing_snapshot_changed")
            if requested_by_id:
                require_reanalysis(User.objects.get(pk=requested_by_id), locked)
                for member in source_scope(locked).filter(pk__in=batch_ids or []):
                    require_reanalysis(User.objects.get(pk=requested_by_id), member)
            if RawMessage.objects.filter(
                source=raw.source,
                session_name=raw.session_name,
                message_id=raw.message_id,
                id__gt=raw.id,
            ).exists():
                trace.status, trace.result_summary = (
                    "warning",
                    "Получена более новая редакция сообщения.",
                )
                trace.context_metadata["request_state"] = "superseded"
                trace.save()
                if not explicit:
                    locked.processing_state, locked.processed = "superseded", True
                    locked.save(update_fields=["processing_state", "processed"])
                return
            if locked.traces.filter(attempt_no__gt=trace.attempt_no, status="success").exists():
                trace.status, trace.result_summary = "warning", "Более новая попытка уже обработана; её предложения сохранены."
                trace.context_metadata["request_state"] = "superseded"
                trace.save()
                return
            team = locked.config.team if locked.config_id else locked.team
            replacing=trace.context_metadata.get("replace_unsent",False)
            if replacing:
                from .replay_replacement import replace_unsent
                replace_unsent(batch_ids or [raw.id],trace)
            revisions = persist_themes(themes, locked, trace, json_value(facts))
            if full_state and full_state.get('routing_thread_id'):
                from .models import ThreadMessage, ThreadRoutingAttempt
                for target_id in targets:
                    assigned = [revisions[key].thread_id for key,theme in themes.items() if any(link['raw_message_id']==target_id for link in theme['messages'])]
                    moved = ThreadMessage.objects.filter(raw_message_id=target_id,assignment_method__in=['laya', 'decision']).exclude(thread_id__in=assigned)
                    attempt_ids = list(moved.values_list('routing_attempt_id',flat=True))
                    moved.delete()
                    ThreadRoutingAttempt.objects.filter(pk__in=attempt_ids).update(outcome='llm_corrected',reason_code='llm_reassigned')
            origin = RawMessage.objects.filter(
                source=raw.source,
                session_name=raw.session_name,
                message_id=raw.message_id,
            )
            previous = FactCandidate.objects.filter(
                Q(trace__raw_message__in=origin) | Q(thread_revision__thread_id__in=[revision.thread_id for revision in revisions.values()])
                | Q(team=team, fact_type='commitment', proposed_changes__promise_message_id__in=[fact['promise_message_id'] for fact in facts if fact['fact_type']=='commitment'])
            ).exclude(trace=trace)
            if replacing:previous=FactCandidate.objects.filter(trace__raw_message_id__in=batch_ids or [raw.id]).exclude(trace=trace)
            accepted = list(previous.filter(status="approved"))
            pending_candidates = list(previous.filter(status="pending"))
            sender = (
                UserProfile.objects.filter(
                    phone=raw.sender_phone,
                    user__memberships__team=team,
                    user__memberships__status="active",
                )
                .distinct()
                .first()
                if raw.sender_phone
                else None
            )
            proposed, duplicates, retained = 0, [], []
            for index, fact in enumerate(facts):
                revision = revisions[fact["thread_key"]]
                replaced_promises = []
                if fact['fact_type']=='commitment':
                    replaced_promises = [item.id for item in pending_candidates if item.fact_type=='commitment' and same_commitment_origin(item.proposed_changes,fact)]
                related_approved = [item for item in accepted if fact['fact_type']=='commitment' or not item.thread_revision_id or item.thread_revision.thread_id == revision.thread_id]
                approved_obligation = next((item for item in related_approved if item.fact_type == "commitment" and same_commitment_origin(item.proposed_changes, fact) and hasattr(item, "accepted_commitment")), None) if fact["fact_type"] == "commitment" else None
                fulfillment_update = bool(approved_obligation and (fact.get("commitment_status") in ("fulfilled", "cancelled") or fact.get("commitment_id")) and approved_obligation.accepted_commitment.status not in ("fulfilled", "cancelled"))
                if approved_obligation and fulfillment_update:
                    fact["commitment_id"] = approved_obligation.accepted_commitment.id
                    fact["base_commitment_version"] = approved_obligation.accepted_commitment.version
                if fact['fact_type'] != 'commitment' and any(fact_identity(item.proposed_changes) == fact_identity(fact) for item in related_approved) and not fulfillment_update:
                    duplicates.append(index)
                    continue
                if approved_obligation and not fulfillment_update:
                    duplicates.append(index)
                    continue
                matches = Project.objects.filter(
                    team=team,
                    archived=False,
                ).filter(Q(normalized_name=normalize_deal_name(fact["object_name"])) | Q(source="bitrix_crm", name__iexact=fact["object_name"]))
                project = (
                    matches.first()
                    if fact["object_name"] and matches.count() == 1
                    else None
                )
                if any(item.fact_type == fact["fact_type"] for item in accepted):
                    fact["uncertainties"].append(
                        "Есть ранее подтверждённый факт из этого сообщения. Проверьте, является ли предложение корректировкой."
                    )
                if fact["fact_type"] == "commitment":
                    promise_raw = RawMessage.objects.select_related("config").get(pk=fact["promise_message_id"])
                    profiles = UserProfile.objects.filter(user__memberships__team=team, user__memberships__status="active", user__is_active=True)
                    # A closing reply may come from the requester. Resolve the actor
                    # from the promise or the explicit assignment, never the target.
                    if fact["assignment_kind"] == "promise" and promise_raw.sender_phone:
                        authors = profiles.filter(phone=promise_raw.sender_phone).distinct()
                    elif promise_raw.source == "whatsapp_export":
                        authors = profiles.none()
                    else:
                        authors = profiles.filter(full_name=fact.get("responsible_name", "")).distinct()
                    sender = authors.first() if authors.count() == 1 else None
                identity = hashlib.sha256(f"{fact['evidence_message_id']}:{fact_identity(fact)}".encode()).hexdigest()
                candidate, created = FactCandidate.objects.get_or_create(
                    source_key=(f"replay:{trace.id}:" if replacing else "")+f"thread:{revision.thread_id}:revision:{revision.version}:fact:{identity}",
                    defaults={
                        "thread_revision": revisions[fact["thread_key"]],
                        "trace": target_traces.get(fact["evidence_message_id"], trace),
                        "project": project,
                        "team": team,
                        "manager": sender if fact["fact_type"] == "commitment" else sender or (project.manager if project else None),
                        "fact_type": fact["fact_type"],
                        "proposed_changes": json_value(fact),
                        "base_project_version": project.version if project else 0,
                        "confidence": fact["confidence"],
                        "uncertainties": fact["uncertainties"],
                    },
                )
                FactCandidate.objects.filter(pk__in=replaced_promises).exclude(pk=candidate.pk).update(status='superseded')
                references = fact.get("evidence_messages") or [{"raw_message_id": fact["evidence_message_id"], "quote": fact["evidence"], "role": "source"}]
                for reference in references:
                    FactEvidence.objects.get_or_create(
                        candidate=candidate,
                        raw_message_id=reference["raw_message_id"],
                        quote=reference["quote"],
                        field_name=reference["role"],
                    )
                if created and (candidate.fact_type == "project" or fact["object_name"] or fact["company_name"] or project):
                    from .tasks import enqueue_crm_match

                    enqueue_crm_match(candidate.id)
                from .autonomous import enqueue_decision
                enqueue_decision(candidate.id)
                if created and not cfg.autonomous_enabled and not trace.context_metadata.get("analysis_limits", {}).get("maintenance_quiet"):
                    from .notifications import notify_on_new_candidate

                    notify_on_new_candidate(candidate)
                proposed += 1
                retained.append(candidate.pk)
            replaced_origins = {raw.id, *(batch_ids or []), *(fact["evidence_message_id"] for fact in facts)}
            previous.filter(status="pending", trace__raw_message_id__in=replaced_origins).exclude(pk__in=retained).update(status="superseded")
            # All facts must exist before deciding whether their shared thread
            # identifies one project. A multi-object report has no shared one.
            from .facts import _link_thread_project
            for candidate in FactCandidate.objects.filter(pk__in=retained, project__isnull=False).select_related("project", "thread_revision"):
                _link_thread_project(candidate, candidate.project)
            locked.processed, locked.processing_state = (
                True,
                ("analyzed" if cfg.autonomous_enabled else "needs_review") if proposed else "no_facts",
            )
            locked.save(update_fields=["processed", "processing_state"])
            classified = {link["raw_message_id"] for theme in themes.values() for link in theme["messages"]}
            batch_covered = set(trace.context_metadata.get("batch_message_ids", [])) & classified
            if (cfg.autonomous_enabled or packet) and batch_covered:
                source_scope(locked).filter(pk__in=batch_covered).update(processed=True, processing_state="analyzed")
                from .models import SourceWorkItem
                SourceWorkItem.objects.bulk_create([SourceWorkItem(raw_message_id=pk, processing_version=cfg.autonomous_policy_version, state="done") for pk in batch_covered], ignore_conflicts=True)
                SourceWorkItem.objects.filter(raw_message_id__in=batch_covered, processing_version=cfg.autonomous_policy_version).update(state="done", error_code="", lease_until=None)
            trace.ai_extracted_facts = json_value({"facts": facts})
            trace.context_metadata["already_approved_fact_indices"] = duplicates
            trace.pipeline_action = "proposed_facts" if proposed else "non_commercial"
            trace.status, trace.result_summary = (
                "success",
                f"Предложено фактов: {proposed}. Уже подтверждено ранее: {len(duplicates)}.",
            )
            trace.context_metadata["timings_ms"]["persist"] = round((time.monotonic()-persistence_started)*1000)
            trace.save()
            if packet:
                from .models import HistoryAnalysisItem
                for message_id, member in target_traces.items():
                    local_facts = [fact for fact in facts if fact["evidence_message_id"] == message_id]
                    complete = any(theme["state"] == "ready" and any(link["raw_message_id"] == message_id for link in theme["messages"]) for theme in themes.values())
                    disposition = "facts" if local_facts else "no_facts" if complete else "insufficient_data"
                    reason = "Извлечены проверяемые кандидаты фактов." if local_facts else "Сообщение классифицировано; новых бизнес-фактов нет." if complete else "Мысль не определена; подтверждённых оснований для бизнес-факта недостаточно."
                    member.context_metadata = {**copy.deepcopy(trace.context_metadata), "packet_anchor_id": raw.id}
                    member.status, member.error_code = "success", ""
                    member.ai_extracted_facts = json_value({"facts": local_facts})
                    member.result_summary = reason
                    member.pipeline_action = "proposed_facts" if local_facts else "non_commercial"
                    member.save(update_fields=["context_metadata", "status", "error_code", "ai_extracted_facts", "result_summary", "pipeline_action"])
                    HistoryAnalysisItem.objects.filter(trace=member, outbox_event_id=usage_event_id.get()).update(state="succeeded", disposition=disposition, reason_code=disposition, reason_description=reason, updated_at=timezone.now())
            for indexed_id in {raw.id, *batch_covered}:
                OutboxEvent.objects.get_or_create(deduplication_key=f"index:{indexed_id}", defaults={"event_type": "index_message", "payload": {"raw_id": indexed_id}})
        if not explicit and not cfg.autonomous_enabled:
            from .commitment_refresh import schedule_commitment_refresh
            schedule_commitment_refresh(raw)
    except (
        ProviderUnavailable,
        ValidationError,
        PermissionDenied,
        User.DoesNotExist,
    ) as exc:
        if (isinstance(exc, ProviderUnavailable) and str(exc) == "thread_revision_conflict"
                and not trace.context_metadata.get("live_batch_policy") and not trace.historyanalysisitem_set.exists()):
            # Never reuse an immutable request containing obsolete thread versions.
            # Reserve a fresh snapshot and let the durable queue retry the analysis.
            with transaction.atomic():
                source = RawMessage.objects.select_for_update().get(pk=raw.id)
                key = f"thread-conflict:{trace.id}"
                replacement = reserve_attempt(source, key)
                OutboxEvent.objects.get_or_create(deduplication_key=key, defaults={
                    "event_type": "extract_message", "payload": {"raw_id": raw.id,
                    "trace_id": replacement.id, "requested_by_id": requested_by_id,
                    "commitment_refresh": True}})
                trace.status = "warning"
                trace.context_metadata["request_state"] = "superseded"
                trace.result_summary = "Тема изменилась во время анализа; запрошена новая версия."
                trace.save()
            return
        trace.status, trace.pipeline_action = "error", "error"
        trace.error_code = (
            str(exc)[:64]
            if isinstance(exc, ProviderUnavailable)
            else (
                "reanalysis_access_revoked"
                if isinstance(exc, (PermissionDenied, User.DoesNotExist))
                else "invalid_schema"
            )
        )
        if (
            trace.context_metadata.get("request_state") == "sent"
            and trace.error_code != "context_request_uncertain"
        ):
            trace.context_metadata["request_state"] = "failed"
        usage = (
            ProviderUsage.objects.filter(
                outbox_event_id=usage_event_id.get(),
                operation__in=("extraction", "chat"),
            )
            .order_by("-id")
            .first()
            if usage_event_id.get()
            else None
        )
        if usage:
            trace.context_metadata["input_tokens_actual"] = usage.input_tokens
        diagnostics = trace.context_metadata.setdefault("response_diagnostics", {})
        # Error provenance belongs to this error, never an earlier retry.
        current_error_diagnostics = dict(exc.diagnostics) if isinstance(exc, ProviderUnavailable) else {}
        for key in ('request_admission_denied', 'provider_error_type', 'provider_error_category',
                    'provider_http_status', 'provider_error_location', 'provider_error_body_format',
                    'provider_error_detail_shape', 'provider_error_message_hint'):
            diagnostics.pop(key, None)
        if isinstance(exc, ProviderUnavailable):
            diagnostics.update(current_error_diagnostics)
        elif isinstance(exc, ValidationError):
            diagnostics["field_errors"] = json_value(exc.get_full_details())
        trace.result_summary = (
            "Обработка не завершена. Новые факты не сохранены. "
            "Предыдущие результаты сохранены."
        )
        trace.save()
        RawMessage.objects.filter(pk=raw_id, processed=False).update(
            processing_state="failed"
        )
        raise ProviderUnavailable(
            trace.error_code,
            diagnostics=diagnostics,
            retry_after=exc.retry_after
            if isinstance(exc, ProviderUnavailable)
            else None,
        ) from None
    except Exception as exc:
        logger.exception('extraction_internal_error trace_id=%s',trace.id)
        trace.status,trace.error_code = 'error','pipeline_internal_error'
        trace.context_metadata['request_state'] = 'failed'
        trace.context_metadata['internal_error_type'] = type(exc).__name__
        trace.result_summary = 'Внутренняя ошибка обработки. Требуется повторный разбор; результат не подтверждён.'
        trace.save(update_fields=['status','error_code','context_metadata','result_summary'])
        RawMessage.objects.filter(pk=raw_id,processed=False).update(processing_state='failed')
        raise ProviderUnavailable('pipeline_internal_error') from None
