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
from .facts import FactSchema, fact_identity, json_value
from .message_context import build_context, source_scope
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


def _facts(result, raw, diagnostics=None):
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
        if not raw.sent_at_known and fact.get("deadline_at"):
            fact["deadline_at"], fact["deadline_precision"] = None, "unknown"
            fact["uncertainties"].append(
                "Время исходного сообщения неизвестно: срок требует проверки."
            )
    return schema.validated_data


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


def extract_message(raw_id, trace_id=None, requested_by_id=None):
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
                raw, cfg, known, trace.context_metadata["snapshot_max_id"]
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
        facts = _facts(result, raw, diagnostics)
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
                if fact_identity(fact) in accepted_ids:
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
                candidate, _ = FactCandidate.objects.get_or_create(
                    source_key=f"message:{raw.id}:attempt:{trace.attempt_no}:fact:{index}",
                    defaults={
                        "trace": trace,
                        "project": project,
                        "team": team,
                        "manager": sender or (project.manager if project else None),
                        "fact_type": fact["fact_type"],
                        "proposed_changes": json_value(fact),
                        "base_project_version": project.version if project else 0,
                        "confidence": fact["confidence"],
                        "uncertainties": fact["uncertainties"],
                    },
                )
                FactEvidence.objects.get_or_create(
                    candidate=candidate,
                    raw_message=raw,
                    quote=fact["evidence"],
                    field_name="source",
                )
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
