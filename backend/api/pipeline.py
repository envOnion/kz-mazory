"""Durable inbox processing: immutable sources, schema-checked proposals, no business writes."""

import hashlib
import json
import re
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from .models import (
    RawMessage,
    MessageProcessingTrace,
    FactCandidate,
    FactEvidence,
    Project,
    UserProfile,
    AISettings,
    OutboxEvent,
)
from .facts import FactSchema, json_value
from .deduplication import normalize_deal_name
from .ai_service import AIService
from .providers import ProviderUnavailable
from .plain_text import clean_context


def extract_message(raw_id):
    raw = RawMessage.objects.select_related("config__team", "team", "project").get(
        pk=raw_id
    )
    team = raw.config.team if raw.config_id else raw.team
    if not team or not team.is_active:
        return
    if raw.source == "waha" and (not raw.config_id or not raw.config.is_active):
        return
    if raw.source == "attachment" and not raw.project_id:
        return
    if raw.processed:
        return
    # Context is restricted to this source chat and strictly earlier timestamps.
    context_qs = RawMessage.objects.filter(timestamp__lt=raw.timestamp)
    context_qs = (
        context_qs.filter(config=raw.config)
        if raw.config_id
        else context_qs.filter(source=raw.source, project_id=raw.project_id)
    )
    context = clean_context(
        context_qs.order_by("-timestamp").values(
            "id", "content", "sender_name", "timestamp"
        )[:12]
    )
    known = list(
        Project.objects.filter(team=team, is_verified=True, archived=False)
        .order_by("id")
        .values("id", "name", "company__name")[:100]
    )
    trace = MessageProcessingTrace.objects.create(
        raw_message=raw,
        attempt_no=raw.traces.count() + 1,
        whatsapp_message_id=raw.message_id,
        whatsapp_chat_id=raw.chat_id,
        whatsapp_sender_phone=raw.sender_phone,
        whatsapp_sender_name=raw.sender_name,
        whatsapp_timestamp=raw.timestamp if raw.sent_at_known else None,
        whatsapp_content=raw.content,
        earlier_messages_context=json_value(context),
        earlier_messages_count=len(context),
        model_version=AISettings.get_active().chat_model_name,
        prompt_version="facts-v1",
        status="warning",
    )
    try:
        result = AIService.analyze_message_with_context(
            raw.content,
            raw.sender_name,
            json_value(context),
            known,
            raw.timestamp.isoformat() if raw.sent_at_known else None,
            raw.config.snapshot.get("timezone", "Asia/Almaty")
            if raw.config_id
            else "Asia/Almaty",
        )
        schema = FactSchema(data=result["facts"], many=True)
        schema.is_valid(raise_exception=True)
        facts = schema.validated_data
        for fact in facts:
            if fact["fact_type"] == "payment" and fact["payment_kind"] == "increment":
                if re.search(
                    r"не\s+оплат|оплатим|төленбеді|төлейміз", fact["evidence"], re.I
                ):
                    raise ProviderUnavailable("payment_evidence_contradiction")
                if re.search(
                    r"всего\s+оплачено|итого\s+оплачено|накопительн",
                    fact["evidence"],
                    re.I,
                ):
                    fact["payment_kind"] = "cumulative"
                    fact["uncertainties"].append(
                        "Накопительный итог не является новым платежом."
                    )
            if fact["evidence"] not in raw.content:
                raise ProviderUnavailable("evidence_not_in_source")
            if not raw.sent_at_known and fact.get("deadline_at"):
                fact["deadline_at"] = None
                fact["deadline_precision"] = "unknown"
                fact["uncertainties"].append(
                    "Время исходного сообщения неизвестно: срок требует проверки."
                )
        with transaction.atomic():
            locked = RawMessage.objects.select_for_update().get(pk=raw_id)
            if locked.processed:
                trace.delete()
                return
            if RawMessage.objects.filter(
                source=raw.source,
                session_name=raw.session_name,
                message_id=raw.message_id,
                id__gt=raw.id,
            ).exists():
                locked.processing_state, locked.processed = "superseded", True
                locked.save(update_fields=["processing_state", "processed"])
                trace.status, trace.result_summary = (
                    "warning",
                    "Получена более новая редакция сообщения.",
                )
                trace.save(update_fields=["status", "result_summary"])
                return
            # A newer edit can supersede pending proposals, never erase accepted facts.
            older = RawMessage.objects.filter(
                source=raw.source,
                session_name=raw.session_name,
                message_id=raw.message_id,
            ).exclude(pk=raw.pk)
            FactCandidate.objects.filter(
                trace__raw_message__in=older, status="pending"
            ).update(status="superseded")
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
            for index, fact in enumerate(facts):
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
                candidate, _ = FactCandidate.objects.get_or_create(
                    source_key=f"message:{raw.id}:fact:{index}",
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
            locked.processed, locked.processing_state = (
                True,
                "needs_review" if facts else "no_facts",
            )
            locked.save(update_fields=["processed", "processing_state"])
            trace.ai_extracted_facts = json_value({"facts": facts})
            trace.pipeline_action = "proposed_facts" if facts else "non_commercial"
            trace.status, trace.result_summary = (
                "success",
                f"Предложено фактов: {len(facts)}",
            )
            trace.save()
            OutboxEvent.objects.get_or_create(
                deduplication_key=f"index:{raw.id}",
                defaults={"event_type": "index_message", "payload": {"raw_id": raw.id}},
            )
    except (ProviderUnavailable, ValidationError) as exc:
        trace.status, trace.pipeline_action = "error", "error"
        trace.error_code = (
            str(exc)[:64] if isinstance(exc, ProviderUnavailable) else "invalid_schema"
        )
        trace.save(update_fields=["status", "pipeline_action", "error_code"])
        RawMessage.objects.filter(pk=raw_id).update(processing_state="failed")
        raise ProviderUnavailable(trace.error_code) from None
