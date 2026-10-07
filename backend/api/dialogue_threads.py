"""Versioned themes. Only a ready thought with literal sources may publish facts."""

import hashlib
import json
import re
from datetime import datetime, timezone as datetime_timezone
from django.db.models import Q
from rest_framework import serializers
from .models import (
    DialogueThread,
    ThreadMessage,
    ThreadRevision,
    FactCandidate,
    RawMessage,
    Team,
    WhatsAppConfig,
    Commitment,
)
from .providers import ProviderUnavailable


def lock_source(raw):
    """Serialize theme publication and review before locking their mutable rows."""
    if raw.config_id:
        WhatsAppConfig.objects.select_for_update().get(pk=raw.config_id)
    else:
        Team.objects.select_for_update().get(pk=raw.team_id)


def lock_candidate_source(candidate):
    raw = candidate.trace.raw_message
    if raw is not None:
        lock_source(raw)
    elif candidate.thread_revision_id and candidate.thread_revision.thread.config_id:
        WhatsAppConfig.objects.select_for_update().get(
            pk=candidate.thread_revision.thread.config_id
        )
    else:
        Team.objects.select_for_update().get(pk=candidate.team_id)


def source_key(raw):
    team_id = raw.config.team_id if raw.config_id else raw.team_id
    from .whatsapp_identity import WHATSAPP_SOURCES
    family = "waha" if raw.config_id and raw.source in WHATSAPP_SOURCES else raw.source
    return hashlib.sha256(
        json.dumps(
            [
                team_id,
                raw.config_id,
                family,
                raw.session_name,
                raw.chat_id,
                raw.project_id if not raw.config_id else None,
            ]
        ).encode()
    ).hexdigest()


def analysis_position(raw):
    """Import IDs may run backwards; source order follows the original date."""
    at = raw.timestamp if raw.sent_at_known else raw.received_at
    delta = at.astimezone(datetime_timezone.utc) - datetime(1970, 1, 1, tzinfo=datetime_timezone.utc)
    return max(0, delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds)


def context_threads(raw):
    qs = DialogueThread.objects.filter(source_key=source_key(raw)).exclude(
        state="superseded"
    )
    # Recent themes plus named older themes. Summary is a retrieval hint, never evidence.
    words = [
        word for word in re.findall(r"\w+", raw.content.casefold()) if len(word) >= 4
    ][:8]
    named = Q(pk__in=[])
    for word in words:
        named |= Q(topic__icontains=word)
    preferred = list(qs.filter(named).order_by("-updated_at")[:10])
    ids = {item.id for item in preferred}
    items = preferred + list(
        qs.exclude(pk__in=ids).order_by("-updated_at")[: 20 - len(preferred)]
    )
    return [
        {
            "id": item.id,
            "version": item.version,
            "topic": item.topic,
            "state": item.state,
            "parent_id": item.parent_id,
            "project_id": item.project_id,
            "summary": item.summary[:600],
            "commitments": [{**task, "deadline_at": task["deadline_at"].isoformat() if task["deadline_at"] else None} for task in Commitment.objects.filter(candidate__thread_revision__thread_id=item.id, status__in=["pending", "overdue"], is_verified=True).values("id", "version", "commitment_text", "responsible_name", "source_message_id", "deadline_at")[:10]],
            "message_ids": list(
                item.message_links.order_by("-raw_message_id").values_list(
                    "raw_message_id", flat=True
                )[:40]
            ),
        }
        for item in items
    ]


class LinkSchema(serializers.Serializer):
    raw_message_id = serializers.IntegerField(min_value=1)
    thought_state = serializers.ChoiceField(
        choices=["intermediate", "final", "unknown"]
    )
    relation = serializers.ChoiceField(
        choices=["discusses", "answers", "clarifies", "cancels", "fulfills"],
        default="discusses",
    )
    rationale = serializers.CharField(max_length=1000, allow_blank=True, default="")


class ThemeSchema(serializers.Serializer):
    key = serializers.CharField(max_length=64)
    thread_id = serializers.IntegerField(min_value=1, allow_null=True, default=None)
    parent_key = serializers.CharField(
        max_length=64, allow_null=True, allow_blank=True, default=None
    )
    topic = serializers.CharField(max_length=255)
    summary = serializers.CharField(max_length=2000, allow_blank=True, default="")
    state = serializers.ChoiceField(choices=["open", "ready", "unknown"])
    completion_reason = serializers.CharField(
        max_length=1000, allow_blank=True, default=""
    )
    messages = LinkSchema(many=True, allow_empty=False)


def normalize_themes(rows):
    """Fixed wire slots prevent repeated keys; saved legacy arrays stay valid."""
    if isinstance(rows, dict):
        if "t0" not in rows or any(key not in {f"t{i}" for i in range(16)} or not isinstance(value,dict) or "key" in value for key,value in rows.items()):
            raise ProviderUnavailable("thread_classification_invalid")
        return [{**value,"key":key} for key,value in rows.items()]
    return rows


def prepare_themes(result, raw, trace):
    from .message_context import source_scope

    rows = normalize_themes(result.get("threads"))
    result["threads"] = rows
    if rows is None:
        if result.get("facts"):
            raise ProviderUnavailable("thread_classification_missing")
        rows = []
    if not isinstance(rows, list):
        raise ProviderUnavailable("thread_classification_invalid")
    classification = result.get("target_classification")
    pairs = [(classification,raw.id)] if classification is not None else []
    if "target_classifications" in result:
        targets = trace.context_metadata.get("batch_message_ids") or [raw.id]
        multiple = result["target_classifications"]
        if not isinstance(multiple,dict) or set(multiple) != {f"c{i}" for i in range(len(targets))} or pairs:
            raise ProviderUnavailable("target_classification_invalid")
        pairs = [(multiple[f"c{i}"],pk) for i,pk in enumerate(targets)]
    for classification, target_id in pairs:
        # A dedicated required wire object pins the target even when the model
        # classifies only nearby originals in threads. The model, not the server,
        # supplies its theme and thought state; no inferred classification.
        if not isinstance(classification, dict) or type(classification.get("raw_message_id")) is not int or classification["raw_message_id"] != target_id or not isinstance(classification.get("thread_key"), str):
            raise ProviderUnavailable("target_classification_invalid")
        selected = [row for row in rows if isinstance(row, dict) and row.get("key") == classification["thread_key"]]
        schema = LinkSchema(data=classification)
        if len(selected) != 1 or not schema.is_valid() or not isinstance(selected[0].get("messages"), list):
            raise ProviderUnavailable("target_classification_invalid")
        existing = [link for link in selected[0]["messages"] if isinstance(link, dict) and link.get("raw_message_id") == target_id]
        if existing and any(link.get("thought_state") != schema.validated_data["thought_state"] for link in existing):
            raise ProviderUnavailable("target_classification_conflict")
        if not existing:
            selected[0]["messages"].append(schema.validated_data)
    if not rows:
        rows = [
            {
                "key": "unresolved",
                "topic": "Тема требует уточнения",
                "state": "unknown",
                "messages": [{"raw_message_id": raw.id, "thought_state": "unknown"}],
            }
        ]
    # DRF integer coercion is inappropriate for source identities supplied by AI.
    for row in rows:
        if not isinstance(row, dict) or (
            row.get("thread_id") is not None and type(row["thread_id"]) is not int
        ):
            raise ProviderUnavailable("thread_classification_invalid")
        if not isinstance(row.get("messages"), list) or any(
            not isinstance(link, dict) or type(link.get("raw_message_id")) is not int
            for link in row["messages"]
        ):
            raise ProviderUnavailable("thread_classification_invalid")
    schema = ThemeSchema(data=rows, many=True)
    schema.is_valid(raise_exception=True)
    themes = schema.validated_data
    keyed = {theme["key"]: theme for theme in themes}
    if len(keyed) != len(themes):
        raise ProviderUnavailable("thread_keys_duplicate")
    allowed = {raw.id} | {
        item["raw_message_id"]
        for item in trace.earlier_messages_context
        if not item.get("partial")
    }
    ids = {link["raw_message_id"] for theme in themes for link in theme["messages"]}
    valid_ids = set(
        source_scope(raw)
        .filter(id__in=ids, id__lte=trace.context_metadata["snapshot_max_id"])
        .exclude(processing_state__in=["deleted", "superseded"])
        .values_list("id", flat=True)
    )
    diagnostics = {"reference_count":len(ids), "unoffered_reference_count":len(ids - allowed),
                   "unavailable_reference_count":len(ids - valid_ids),
                   "missing_target_count":len(({raw.id} | set(trace.context_metadata.get("batch_message_ids", []))) - ids)}
    if not ids.issubset(allowed) or ids != valid_ids:
        raise ProviderUnavailable("thread_sources_unavailable", diagnostics=diagnostics)
    if diagnostics["missing_target_count"]:
        raise ProviderUnavailable("batch_classification_missing", diagnostics=diagnostics)
    offered = {item["id"] for item in trace.context_metadata.get("known_threads", [])}
    for theme in themes:
        if theme["thread_id"] is not None and theme["thread_id"] not in offered:
            raise ProviderUnavailable("thread_source_conflict")
        member_ids = [link["raw_message_id"] for link in theme["messages"]]
        if len(set(member_ids)) != len(member_ids):
            raise ProviderUnavailable("thread_sources_duplicate")
        if theme["state"] == "ready" and (
            not theme["completion_reason"]
            or not any(link["thought_state"] == "final" for link in theme["messages"])
        ):
            raise ProviderUnavailable("thread_completion_unproven")
        chain, parent = {theme["key"]}, theme["parent_key"]
        while parent:
            if parent in chain or parent not in keyed:
                raise ProviderUnavailable("thread_parent_invalid")
            chain.add(parent)
            parent = keyed[parent]["parent_key"]
    for fact in result["facts"]:
        if isinstance(fact, dict) and not fact.get("thread_key"):
            anchor = fact.get("evidence_message_id") or fact.get("promise_message_id")
            compatible = [key for key, theme in keyed.items() if type(anchor) is int and any(link["raw_message_id"] == anchor for link in theme["messages"])]
            if len(compatible) == 1:
                fact["thread_key"] = compatible[0]
                trace.context_metadata.setdefault("schema_repairs", []).append("unique_evidence_thread")
        if not isinstance(fact, dict) or fact.get("thread_key") not in keyed:
            raise ProviderUnavailable("fact_thread_missing")
    return keyed


def ready_facts(result, themes):
    selected = []
    for fact in result["facts"]:
        theme = themes[fact["thread_key"]]
        if theme["state"] not in ("ready", "open"):
            continue
        if theme["state"] == "open":
            fact["in_progress"] = True
        member_ids = {link["raw_message_id"] for link in theme["messages"]}
        references = fact.get("evidence_messages", [])
        anchor = fact.get("evidence_message_id") or fact.get("promise_message_id")
        if type(anchor) is not int or anchor not in member_ids:
            raise ProviderUnavailable("fact_thread_evidence_missing")
        if not isinstance(references, list) or any(
            not isinstance(ref, dict)
            or type(ref.get("raw_message_id")) is not int
            or ref.get("raw_message_id") not in member_ids
            for ref in references
        ):
            raise ProviderUnavailable("fact_thread_evidence_conflict")
        if fact.get("fact_type") == "commitment" and not references:
            raise ProviderUnavailable("fact_thread_evidence_missing")
        if fact.get("fact_type") == "commitment" and (
            type(fact.get("promise_message_id")) is not int
            or fact["promise_message_id"] not in member_ids
        ):
            raise ProviderUnavailable("commitment_promise_evidence_unavailable")
        selected.append(fact)
    return {"facts": selected}


def persist_themes(themes, raw, trace, facts):
    """Called in the pipeline transaction; commit only the observed generation."""
    team_id = raw.config.team_id if raw.config_id else raw.team_id
    lock_source(raw)
    expected = {
        item["id"]: item["version"]
        for item in trace.context_metadata.get("known_threads", [])
    }
    key = source_key(raw)
    persisted = {}
    pending = dict(themes)
    while pending:
        for theme_key, theme in list(pending.items()):
            parent_key = theme["parent_key"]
            if parent_key and parent_key not in persisted:
                continue
            parent = persisted[parent_key].thread if parent_key else None
            thread = (
                DialogueThread.objects.select_for_update()
                .filter(pk=theme["thread_id"], source_key=key)
                .first()
                if theme["thread_id"]
                else None
            )
            if theme["thread_id"] and (
                not thread or thread.version != expected.get(thread.id)
            ):
                raise ProviderUnavailable("thread_revision_conflict")
            if not thread:
                first = min(link["raw_message_id"] for link in theme["messages"])
                origin = RawMessage.objects.get(pk=first)
                identity = hashlib.sha256(
                    f"{key}:{origin.message_id}:{theme['topic'].casefold()}:{parent.id if parent else ''}".encode()
                ).hexdigest()
                thread, created = DialogueThread.objects.get_or_create(
                    identity=identity,
                    defaults={
                        "team_id": team_id,
                        "config_id": raw.config_id,
                        "source_key": key,
                        "topic": theme["topic"],
                    },
                )
                if (
                    not created
                    and thread.snapshot_max_id
                    > trace.context_metadata["snapshot_max_id"]
                ):
                    raise ProviderUnavailable("thread_revision_conflict")
            if parent:
                ancestor = parent
                while ancestor:
                    if ancestor.pk == thread.pk:
                        raise ProviderUnavailable("thread_parent_invalid")
                    ancestor = ancestor.parent
            theme_facts = [
                fact for fact in facts if fact.get("thread_key") == theme_key
            ]
            latest = thread.revisions.order_by("-version").first()
            if (
                latest
                and thread.topic == theme["topic"]
                and thread.summary == theme["summary"]
                and thread.state == theme["state"]
                and latest.message_snapshot == theme["messages"]
                and latest.extraction == theme_facts
            ):
                persisted[theme_key] = latest
                del pending[theme_key]
                continue
            old_state = thread.state
            thread.parent, thread.topic, thread.summary, thread.state = (
                parent,
                theme["topic"],
                theme["summary"],
                theme["state"],
            )
            thread.version += 1
            thread.snapshot_max_id = trace.context_metadata["snapshot_max_id"]
            thread.save()
            if old_state != thread.state:
                from .notifications import notify_thread_subscribers

                notify_thread_subscribers(thread, old_state, thread.state)
            for link in theme["messages"]:
                ThreadMessage.objects.update_or_create(
                    thread=thread,
                    raw_message_id=link["raw_message_id"],
                    defaults={k: v for k, v in link.items() if k != "raw_message_id"},
                )
            revision = ThreadRevision.objects.create(
                thread=thread,
                version=thread.version,
                state=theme["state"],
                topic=theme["topic"],
                summary=theme["summary"],
                message_snapshot=[dict(link) for link in theme["messages"]],
                extraction=theme_facts,
                completion_reason=theme["completion_reason"],
            )
            # A ready revision is a replacement snapshot for this thought, not a new task.
            old_candidates = FactCandidate.objects.filter(
                thread_revision__thread=thread, status="pending"
            )
            if trace.context_metadata.get("replace_unsent"):
                old_candidates = old_candidates.filter(trace__raw_message_id__in=trace.context_metadata.get("batch_message_ids") or [raw.id])
            old_candidates.update(status="superseded")
            persisted[theme_key] = revision
            del pending[theme_key]
    # Reassign observed messages; retain unseen history and immutable old revisions.
    classified = {
        link["raw_message_id"]
        for theme in themes.values()
        for link in theme["messages"]
    }
    for message_id in classified:
        assigned = [
            persisted[k].thread_id
            for k, theme in themes.items()
            if any(link["raw_message_id"] == message_id for link in theme["messages"])
        ]
        ThreadMessage.objects.filter(
            thread__source_key=key, raw_message_id=message_id
        ).exclude(thread_id__in=assigned).delete()
    empty = DialogueThread.objects.filter(
        source_key=key, message_links__isnull=True
    ).exclude(state="superseded")
    FactCandidate.objects.filter(
        thread_revision__thread__in=empty, status="pending"
    ).update(status="superseded")
    empty.update(state="superseded")
    return persisted
