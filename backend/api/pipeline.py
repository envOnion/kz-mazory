"""Durable extraction attempts: bounded full history, immutable audit, reviewed facts."""

import copy
import json
import re
import uuid

from django.contrib.auth.models import User
from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from .ai_service import AIService, usage_event_id
from .context_tokens import canonical_json, extraction_input, payload_hash
from .deduplication import normalize_deal_name
from .facts import FactSchema, fact_identity, json_value, same_commitment_origin
from .message_context import build_context, source_scope
from .message_time import source_time
from .models import (
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


def _facts(result, raw, diagnostics=None, snapshot_id=None):
    # A missing optional value and an explicit null both mean unknown. Required
    # values, enums and evidence still go through the full serializer validation.
    fields = FactSchema().fields
    normalized = [
        {
            key: value
            for key, value in item.items()
            if not (
                value is None
                and key in fields
                and not fields[key].required
                and not fields[key].allow_null
            )
        }
        if isinstance(item, dict)
        else item
        for item in result["facts"]
    ]
    schema = FactSchema(data=normalized, many=True)
    schema.is_valid(raise_exception=True)
    accepted_facts = []
    for index, fact in enumerate(schema.validated_data):
        if fact["fact_type"] == "payment" and fact["payment_kind"] == "increment":
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
        quote = _source_quote(fact["evidence"], raw.content)
        if quote != fact["evidence"] and diagnostics is not None:
            diagnostics.setdefault("source_whitespace_restored", []).append(index)
        fact["evidence"] = quote
        if fact["fact_type"] == "commitment":
            from .commitment_evidence import validate_commitment
            if not validate_commitment(fact, raw, snapshot_id or raw.id, _source_quote):
                continue
        accepted_facts.append(fact)
    return accepted_facts



def _restore_request(trace):
    try:
        payload = copy.deepcopy(trace.context_metadata["request_envelope"])
        user_message = _extraction_user_message(payload)
        fixed = json.loads(user_message["content"])
        if not isinstance(fixed, dict):
            raise TypeError()
        fixed["context"] = trace.earlier_messages_context
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


def extract_message(raw_id, trace_id=None, requested_by_id=None, commitment_refresh=False):
    explicit = trace_id is not None
    with transaction.atomic():
        raw = (
            RawMessage.objects.select_for_update(of=("self",))
            .select_related("config__team", "team", "project")
            .get(pk=raw_id)
        )
        if raw.processed and not explicit:
            return
        if explicit:
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
        if commitment_refresh and trace.context_metadata.get("request_state") == "not_sent":
            from django.db.models import Max
            trace.context_metadata["snapshot_max_id"] = source_scope(raw).aggregate(last=Max("id"))["last"] or raw.id
        if requested_by_id:
            try:
                require_reanalysis(User.objects.get(pk=requested_by_id), raw)
            except (User.DoesNotExist, PermissionDenied) as exc:
                raise ProviderUnavailable("reanalysis_access_revoked") from exc
        cfg = AIService._config()
        api_format = getattr(cfg, "chat_api_format", "openai_compatible")
        effective_provider_url = AIService.effective_chat_provider_url(cfg)
        if trace.context_metadata.get("request_state") == "sent":
            raise ProviderUnavailable("context_request_uncertain")
        if "request_envelope" in trace.context_metadata:
            snapshot_format = trace.context_metadata.get(
                "api_format", "openai_compatible"
            )
            snapshot_url = trace.context_metadata.get(
                "effective_provider_url", trace.context_metadata.get("provider_url")
            )
            if (
                cfg.chat_model_name != trace.model_version
                or api_format != snapshot_format
                or not isinstance(snapshot_url, str)
                or effective_provider_url.rstrip("/") != snapshot_url.rstrip("/")
            ):
                raise ProviderUnavailable("context_configuration_changed")
            payload = _restore_request(trace)
        else:
            team = raw.config.team if raw.config_id else raw.team
            known = json_value(
                list(
                    Project.objects.filter(team=team, is_verified=True, archived=False)
                    .order_by("id")
                    .values("id", "name", "company__name")[:100]
                )
            )
            context, metadata, payload = build_context(
                raw, cfg, known, trace.context_metadata["snapshot_max_id"], include_following=True
            )
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
            )
            metadata.setdefault("snapshot_max_id", trace.context_metadata["snapshot_max_id"])
            trace.earlier_messages_context, trace.earlier_messages_count = (
                context,
                len(context),
            )
            trace.context_metadata, trace.model_version = metadata, cfg.chat_model_name
        trace.context_metadata["request_state"] = "sent"
        trace.error_code = ""
        trace.result_summary = "Запрос с сохранённой историей отправлен в AI."
        trace.save()
        result, usage, diagnostics = AIService.analyze_payload(
            payload,
            trace.context_metadata.get("effective_provider_url")
            or trace.context_metadata.get("provider_url"),
            expected_api_format=api_format,
        )
        trace.context_metadata["request_state"] = "responded"
        trace.context_metadata["response_diagnostics"] = diagnostics
        value = (
            usage.get("prompt_tokens", usage.get("input_tokens"))
            if isinstance(usage, dict)
            else None
        )
        trace.context_metadata["input_tokens_actual"] = (
            value if type(value) is int and value >= 0 else None
        )
        facts = _facts(result, raw, diagnostics, trace.context_metadata["snapshot_max_id"])
        from .commitment_resolution import resolve_remaining
        resolved = resolve_remaining(facts, raw, cfg, trace)
        facts = _facts({"facts": json_value(resolved)}, raw, diagnostics, trace.context_metadata["snapshot_max_id"])
        with transaction.atomic():
            locked = (
                RawMessage.objects.select_for_update(of=("self",))
                .select_related("config__team", "team", "project")
                .get(pk=raw_id)
            )
            source_scope(locked)
            if requested_by_id:
                require_reanalysis(User.objects.get(pk=requested_by_id), locked)
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
            origin = RawMessage.objects.filter(
                source=raw.source,
                session_name=raw.session_name,
                message_id=raw.message_id,
            )
            previous = FactCandidate.objects.filter(
                trace__raw_message__in=origin
            ).exclude(trace=trace)
            accepted = list(previous.filter(status="approved"))
            accepted_ids = {fact_identity(item.proposed_changes) for item in accepted}
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
            proposed, duplicates = 0, []
            for index, fact in enumerate(facts):
                approved_obligation = next((item for item in accepted if item.fact_type == "commitment" and same_commitment_origin(item.proposed_changes, fact) and hasattr(item, "accepted_commitment")), None) if fact["fact_type"] == "commitment" else None
                fulfillment_update = bool(approved_obligation and fact.get("commitment_status") == "fulfilled" and approved_obligation.accepted_commitment.status not in ("fulfilled", "cancelled"))
                if approved_obligation and fulfillment_update:
                    fact["commitment_id"] = approved_obligation.accepted_commitment.id
                    fact["base_commitment_version"] = approved_obligation.accepted_commitment.version
                if fact_identity(fact) in accepted_ids and not fulfillment_update:
                    duplicates.append(index)
                    continue
                if approved_obligation and not fulfillment_update:
                    duplicates.append(index)
                    continue
                matches = Project.objects.filter(
                    team=team,
                    archived=False,
                    normalized_name=normalize_deal_name(fact["object_name"]),
                )
                project = (
                    matches.first()
                    if fact["object_name"] and matches.count() == 1
                    else None
                )
                if any(item.fact_type == fact["fact_type"] for item in accepted):
                    fact["uncertainties"].append(
                        "Есть ранее подтверждённый факт из этого сообщения. Проверьте, является ли предложение корректировкой."
                    )
                if fact["fact_type"] == "commitment" and raw.config_id and source_time(raw)[2] == "export_header":
                    # Transport sender is the person who imported the chat, not the author.
                    authors = UserProfile.objects.filter(full_name=fact.get("responsible_name", ""), user__memberships__team=team, user__memberships__status="active").distinct()
                    sender = authors.first() if authors.count() == 1 else None
                candidate, created = FactCandidate.objects.get_or_create(
                    source_key=f"message:{raw.id}:attempt:{trace.attempt_no}:fact:{index}",
                    defaults={
                        "trace": trace,
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
                references = fact.get("evidence_messages") or [{"raw_message_id": raw.id, "quote": fact["evidence"], "role": "source"}]
                for reference in references:
                    FactEvidence.objects.get_or_create(
                        candidate=candidate,
                        raw_message_id=reference["raw_message_id"],
                        quote=reference["quote"],
                        field_name=reference["role"],
                    )
                if created and candidate.fact_type == "project":
                    from .tasks import enqueue_crm_match

                    enqueue_crm_match(candidate.id)
                proposed += 1
            previous.filter(status="pending").update(status="superseded")
            locked.processed, locked.processing_state = (
                True,
                "needs_review" if proposed else "no_facts",
            )
            locked.save(update_fields=["processed", "processing_state"])
            trace.ai_extracted_facts = json_value({"facts": facts})
            trace.context_metadata["already_approved_fact_indices"] = duplicates
            trace.pipeline_action = "proposed_facts" if proposed else "non_commercial"
            trace.status, trace.result_summary = (
                "success",
                f"Предложено фактов: {proposed}. Уже подтверждено ранее: {len(duplicates)}.",
            )
            trace.save()
            OutboxEvent.objects.get_or_create(
                deduplication_key=f"index:{raw.id}",
                defaults={"event_type": "index_message", "payload": {"raw_id": raw.id}},
            )
        if not explicit:
            from .commitment_refresh import schedule_commitment_refresh
            schedule_commitment_refresh(raw)
    except (
        ProviderUnavailable,
        ValidationError,
        PermissionDenied,
        User.DoesNotExist,
    ) as exc:
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
        if isinstance(exc, ProviderUnavailable):
            diagnostics.update(exc.diagnostics)
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
