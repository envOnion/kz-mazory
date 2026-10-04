"""Versioned themes. Only a ready thought with literal sources may publish facts."""

import hashlib
import json
import re
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
    return hashlib.sha256(
        json.dumps(
            [
                team_id,
                raw.config_id,
                raw.source,
                raw.session_name,
                raw.chat_id,
                raw.project_id if not raw.config_id else None,
            ]
        ).encode()
    ).hexdigest()


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


def prepare_themes(result, raw, trace):
    from .message_context import source_scope

    rows = result.get("threads")
    if rows is None:
        if result.get("facts"):
            raise ProviderUnavailable("thread_classification_missing")
        rows = []
    if not isinstance(rows, list):
        raise ProviderUnavailable("thread_classification_invalid")
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
    if not ids.issubset(allowed) or ids != valid_ids or raw.id not in ids:
        raise ProviderUnavailable("thread_sources_unavailable")
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
            thread.parent, thread.topic, thread.summary, thread.state = (
                parent,
                theme["topic"],
                theme["summary"],
                theme["state"],
            )
            thread.version += 1
            thread.snapshot_max_id = trace.context_metadata["snapshot_max_id"]
            thread.save()
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
            FactCandidate.objects.filter(
                thread_revision__thread=thread, status="pending"
            ).update(status="superseded")
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
