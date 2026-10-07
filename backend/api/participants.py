"""Source-backed people directory. Discovery never grants application access."""

import re
import hashlib
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta

from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    Commitment, OutboxEvent, Participant, ParticipantIdentity, RawMessage,
    Team, TeamMembership, UserProfile, WhatsAppConfig,
)
from .phone_numbers import normalize_phone

DIRECTORY_VERSION = 2


def person_name(value):
    if not isinstance(value, str):
        return ""
    value = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", value).strip()
    if value.endswith(("@lid", "@c.us", "@s.whatsapp.net", "@g.us")):
        return ""
    return value[:255] if any(char.isalpha() for char in value) else ""


def phone_from_jid(value):
    """An opaque LID must never pass through phone normalization."""
    if not isinstance(value, str):
        return ""
    match = re.fullmatch(r"([0-9]+)(?::[0-9]+)?@(c\.us|s\.whatsapp\.net)", value)
    if not match:
        return ""
    try:
        return normalize_phone(match[1])
    except ValidationError:
        return ""


def canonical_jid(value):
    if not isinstance(value, str):
        return ""
    if re.fullmatch(r"[0-9]+@lid", value):
        return value
    phone = phone_from_jid(value)
    return f"{phone}@c.us" if phone else ""


@dataclass(frozen=True)
class Sender:
    jid: str = ""
    phone: str = ""
    name: str = ""


def message_sender(payload, self_jid=""):
    """WAHA history and webhooks expose different shapes of the same sender."""
    if not isinstance(payload, dict):
        return Sender()
    extra = payload.get("_data") or {}
    extra = extra if isinstance(extra, dict) else {}
    key = extra.get("key") or {}
    key = key if isinstance(key, dict) else {}
    jid = next((canonical_jid(value) for value in (
        payload.get("participant"), key.get("participant"), extra.get("participant"),
    ) if canonical_jid(value)), "")
    own = payload.get("fromMe") is True or key.get("fromMe") is True
    if not jid:
        jid = canonical_jid(payload.get("from")) or (canonical_jid(self_jid) if own else "")
    # participantAlt is an explicit alternate identifier of this message author.
    alternate = next((phone_from_jid(value) for value in (
        key.get("participantAlt"), payload.get("participantAlt"),
        payload.get("participantPn"), extra.get("participantPn"),
    ) if phone_from_jid(value)), "")
    name = next((person_name(value) for value in (
        payload.get("notifyName"), payload.get("pushName"), extra.get("pushName"),
    ) if person_name(value)), "")
    return Sender(jid, phone_from_jid(jid) or alternate, name)


def name_namespace(config):
    return f"team:{config.team_id}:source:{config.id}:{config.group_jid}:name"


def jid_namespace(config):
    return f"team:{config.team_id}:session:{config.session_name}:jid"


def _merge(target, other):
    if target.id == other.id:
        return target
    if target.user_profile_id and other.user_profile_id and target.user_profile_id != other.user_profile_id:
        return None
    if not target.user_profile_id and other.user_profile_id:
        target.user_profile_id = other.user_profile_id
        target.save(update_fields=["user_profile"])
    ParticipantIdentity.objects.filter(participant=other).update(participant=target)
    Commitment.objects.filter(participant=other).update(participant=target)
    other.delete()
    return target


def sync_user_name(profile):
    """Contact labels are display names; never infer a surname or overwrite one."""
    label = person_name(profile.full_name)
    if not label:
        return
    user = User.objects.select_for_update().get(pk=profile.user_id)
    if not user.first_name.strip() and not user.last_name.strip():
        user.first_name = label[:User._meta.get_field("first_name").max_length]
        user.save(update_fields=["first_name"])


@transaction.atomic
def register_sender(config, sender):
    """Serialize per team; unique username also serializes accounts across teams."""
    if not config.team_id or not (sender.jid or sender.phone or sender.name):
        return None
    Team.objects.select_for_update().get(pk=config.team_id)
    keys = []
    if sender.phone:
        keys.append((f"team:{config.team_id}:phone", sender.phone))
    if sender.jid:
        keys.append((jid_namespace(config), sender.jid))
    if not keys:
        keys.append((name_namespace(config), sender.name.casefold()))
    found = [ParticipantIdentity.objects.filter(namespace=ns, value=value).select_related("participant").first() for ns, value in keys]
    people = {identity.participant_id: identity.participant for identity in found if identity}
    participant = next(iter(people.values()), None)
    if participant is None:
        participant = Participant.objects.create(team_id=config.team_id, display_name=sender.name or sender.phone or sender.jid)
    for other in list(people.values()):
        if _merge(participant, other) is None:
            ParticipantIdentity.objects.filter(pk__in=[item.pk for item in found if item]).update(resolution_state="conflict")
            return participant
    for ns, value in keys:
        ParticipantIdentity.objects.get_or_create(namespace=ns, value=value, defaults={
            "participant": participant,
            "resolution_state": "resolved" if sender.phone else "phone_unknown" if sender.jid else "scoped_name",
        })
    if sender.phone:
        if participant.user_profile_id and participant.user_profile.phone and participant.user_profile.phone != sender.phone:
            participant.identities.update(resolution_state="conflict")
            return participant
        profiles = list(UserProfile.objects.filter(phone=sender.phone)[:2])
        user = User.objects.filter(username=sender.phone).first()
        if len(profiles) > 1 or (profiles and user and profiles[0].user_id != user.id):
            participant.identities.update(resolution_state="conflict")
            return participant
        if profiles:
            profile = profiles[0]
        else:
            user, _ = User.objects.get_or_create(username=sender.phone, defaults={"password": make_password(None)})
            # Protect account/profile updates across different importing teams.
            user = User.objects.select_for_update().get(pk=user.id)
            profile, _ = UserProfile.objects.get_or_create(user=user, defaults={"phone": sender.phone, "full_name": sender.name or sender.phone})
        if (profile.phone and profile.phone != sender.phone) or (participant.user_profile_id and participant.user_profile_id != profile.id):
            participant.identities.update(resolution_state="conflict")
            return participant
        fields = []
        if not profile.phone:
            profile.phone = sender.phone
            fields.append("phone")
        if not person_name(profile.full_name) and sender.name:
            profile.full_name = sender.name
            fields.append("full_name")
        if fields:
            profile.save(update_fields=fields)
        participant.user_profile = profile
        participant.identities.exclude(resolution_state="conflict").update(resolution_state="resolved")
    if not person_name(participant.display_name):
        participant.display_name = sender.name or sender.phone or sender.jid
    elif sender.name and not participant.user_profile_id:
        participant.display_name = sender.name
    participant.save(update_fields=["display_name", "user_profile"])
    if participant.user_profile_id:
        sync_user_name(participant.user_profile)
    if sender.name and sender.jid:
        # A transport-backed name remains scoped to that person, even if another
        # group member has the same display name.
        ParticipantIdentity.objects.get_or_create(
            namespace=jid_namespace(config) + f":{sender.jid}:name", value=sender.name.casefold(),
            defaults={"participant": participant, "resolution_state": "resolved" if sender.phone else "phone_unknown"},
        )
    return participant


@transaction.atomic
def enqueue_participants(config, actor=None, message_evidence=None):
    config = WhatsAppConfig.objects.select_for_update().get(pk=config.pk)
    if not config.is_active or not config.team_id:
        return None
    existing = OutboxEvent.objects.select_for_update().filter(event_type="whatsapp_participants", payload__config_id=config.id, payload__team_id=config.team_id, payload__session_name=config.session_name, payload__chat_id=config.group_jid, state__in=["pending", "enqueued"]).order_by("id").first()
    if existing:
        previous = existing.payload.get("message_evidence", [])
        incoming = message_evidence or []
        if len(previous) + len(incoming) <= 100:
            if incoming:
                existing.payload = {**existing.payload, "message_evidence": previous + incoming}
                existing.save(update_fields=["payload"])
            return existing
    return OutboxEvent.objects.create(event_type="whatsapp_participants", deduplication_key=f"participants:{uuid.uuid4().hex}", payload={
        "config_id": config.id, "team_id": config.team_id,
        "session_name": config.session_name, "chat_id": config.group_jid,
        "requested_by_id": actor.id if actor else None,
        "message_evidence": message_evidence or [],
    })


def schedule_participant_sync():
    """The ordinary dispatcher owns discovery, including chats with no new text."""
    cutoff = timezone.now() - timedelta(minutes=15)
    scheduled = 0
    configs = WhatsAppConfig.objects.filter(is_active=True, team__is_active=True).exclude(group_jid="")
    for config_id in configs.values_list("id", flat=True):
        with transaction.atomic():
            config = WhatsAppConfig.objects.select_for_update(of=("self",)).filter(pk=config_id, is_active=True, team__is_active=True).first()
            if not config or not config.group_jid:
                continue
            latest = OutboxEvent.objects.filter(event_type="whatsapp_participants", payload__config_id=config.id, payload__team_id=config.team_id, payload__session_name=config.session_name, payload__chat_id=config.group_jid).order_by("-id").first()
            if latest and (latest.state in ("pending", "enqueued", "processing") or (
                latest.created_at > cutoff and (latest.state != "done" or config.snapshot.get("participant_directory_version") == DIRECTORY_VERSION)
            )):
                continue
            enqueue_participants(config)
            scheduled += 1
    return scheduled


def scope_matches(config, payload):
    return config.is_active and config.team_id == payload["team_id"] and config.session_name == payload["session_name"] and config.group_jid == payload["chat_id"] and config.team.is_active


def bind_aliased_export(config, item, self_jid=""):
    """The existing transport alias proves this sender authored this TXT row."""
    payload = item["payload"]
    raw = RawMessage.objects.filter(pk=item["raw_id"], config=config, team_id=config.team_id, session_name=config.session_name, chat_id=config.group_jid, source="whatsapp_export").first()
    if not raw or not raw.transport_aliases.filter(namespace="waha", external_id=payload.get("id"), source_revision=hashlib.sha256(raw.content.encode()).hexdigest()).exists():
        return
    sender = message_sender(payload, self_jid)
    technical = ParticipantIdentity.objects.filter(namespace=jid_namespace(config), value=sender.jid).select_related("participant").first()
    named = ParticipantIdentity.objects.filter(namespace=name_namespace(config), value=person_name(raw.sender_name).casefold()).select_related("participant").first()
    if technical and named and _merge(technical.participant, named.participant):
        named.refresh_from_db()
        named.resolution_state = "resolved" if technical.participant.user_profile_id else "phone_unknown"
        named.save(update_fields=["resolution_state"])


def restore_saved(config, self_jid=""):
    """Rebuild authors, including media authors retained only in private TXT."""
    from .whatsapp_export_parser import MAX_BYTES, parse_export
    for upload in config.history_job.exports.filter(state="ready") if hasattr(config, "history_job") else []:
        if not scope_matches(config, upload.source_snapshot):
            continue
        with upload.file.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if hashlib.sha256(data).hexdigest() != upload.sha256:
            from .providers import ProviderUnavailable
            raise ProviderUnavailable("participants_export_changed")
        records, _ = parse_export(data, upload.timezone, upload.date_order)
        for name, phone in {(person_name(row.sender), row.phone) for row in records if row.sender}:
            normalized = ""
            if phone:
                try:
                    normalized = normalize_phone(phone)
                except ValidationError:
                    pass
            register_sender(config, Sender(phone=normalized, name=name))
    seen = set()
    query = RawMessage.objects.filter(config=config, team_id=config.team_id, session_name=config.session_name, chat_id=config.group_jid, source__in=["waha", "whatsapp_export"])
    for raw in query.iterator(chunk_size=500):
        if raw.source == "waha":
            sender = message_sender(raw.raw_payload.get("payload", {}), self_jid)
            phone_identity = ParticipantIdentity.objects.filter(namespace=jid_namespace(config), value=sender.jid, resolution_state="resolved", participant__user_profile__isnull=False).select_related("participant__user_profile").first() if sender.jid else None
            if phone_identity:
                sender = Sender(sender.jid, phone_identity.participant.user_profile.phone, sender.name or person_name(phone_identity.participant.display_name))
            # Correct derived columns while preserving the immutable payload.
            updates = {}
            if raw.sender_phone != sender.phone:
                updates["sender_phone"] = sender.phone
            if sender.name and raw.sender_name != sender.name:
                updates["sender_name"] = sender.name
            if updates:
                RawMessage.objects.filter(pk=raw.pk).update(**updates)
        else:
            phone = ""
            if raw.sender_phone:
                try:
                    phone = normalize_phone(raw.sender_phone)
                except ValidationError:
                    pass
            sender = Sender(phone=phone, name=person_name(raw.sender_name))
        if sender not in seen:
            register_sender(config, sender)
            seen.add(sender)


@transaction.atomic
def link_export_names(config, self_jid=""):
    """Require repeated unique source matches, never a display-name guess."""
    Team.objects.select_for_update().get(pk=config.team_id)
    slots = defaultdict(lambda: {"waha": [], "whatsapp_export": []})
    rows = RawMessage.objects.filter(config=config, team_id=config.team_id, session_name=config.session_name, chat_id=config.group_jid, source__in=["waha", "whatsapp_export"]).exclude(processing_state__in=["deleted", "superseded", "deduplication_ambiguous"])
    for raw in rows.iterator(chunk_size=500):
        if not raw.sent_at_known or len(raw.content.strip()) < 16:
            continue
        slot = (raw.timestamp.replace(second=0, microsecond=0), raw.content)
        value = message_sender(raw.raw_payload.get("payload", {}), self_jid).jid if raw.source == "waha" else person_name(raw.sender_name).casefold()
        if value:
            slots[slot][raw.source].append(value)
    votes = Counter()
    names = defaultdict(set)
    for slot in slots.values():
        if len(slot["waha"]) == len(slot["whatsapp_export"]) == 1:
            name, jid = slot["whatsapp_export"][0], slot["waha"][0]
            votes[(name, jid)] += 1
            names[name].add(jid)
    for (name, jid), count in votes.items():
        if count < 2 or len(names[name]) != 1:
            continue
        named = ParticipantIdentity.objects.filter(namespace=name_namespace(config), value=name).select_related("participant").first()
        technical = ParticipantIdentity.objects.filter(namespace=jid_namespace(config), value=jid).select_related("participant__user_profile").first()
        if not named or not technical or named.participant_id == technical.participant_id:
            continue
        label = named.participant.display_name
        target = _merge(technical.participant, named.participant)
        if target:
            named.refresh_from_db()
            named.resolution_state = "resolved" if target.user_profile_id else "phone_unknown"
            named.save(update_fields=["resolution_state"])
            target.display_name = label
            target.save(update_fields=["display_name"])
            if target.user_profile_id and not person_name(target.user_profile.full_name):
                target.user_profile.full_name = label
                target.user_profile.save(update_fields=["full_name"])


def directory_participants(user):
    from . import access
    people = Participant.objects.filter(team__is_active=True).select_related("team", "user_profile__user").prefetch_related("identities").order_by("display_name", "id")
    if not user.is_superuser:
        people = people.filter(team_id__in=access.team_ids(user, ["team_lead", "finance"]))
    memberships = {(row.user_id, row.team_id): row.status for row in TeamMembership.objects.filter(status="active", team_id__in=people.values("team_id"))}
    result = []
    for person in people:
        profile = person.user_profile
        states = {identity.resolution_state for identity in person.identities.all()}
        result.append({
            "id": person.id, "team_id": person.team_id, "team_name": person.team.name,
            "display_name": person.display_name, "profile_id": person.user_profile_id,
            "user_id": profile.user_id if profile else None, "phone": profile.phone if profile else "",
            "resolution_state": "conflict" if "conflict" in states else "resolved" if profile else "phone_unknown",
            "access_status": "active" if profile and profile.user.is_active and (profile.user_id, person.team_id) in memberships else "not_granted",
            "aliases": sorted({identity.value for identity in person.identities.all() if identity.namespace.endswith(":name")}),
        })
    return result


def directory_sync_status(user):
    from . import access
    configs = WhatsAppConfig.objects.filter(is_active=True, team__is_active=True)
    if not user.is_superuser:
        configs = configs.filter(team_id__in=access.team_ids(user, ["team_lead", "finance"]))
    result = []
    for config in configs:
        event = OutboxEvent.objects.filter(event_type="whatsapp_participants", payload__config_id=config.id, payload__team_id=config.team_id, payload__session_name=config.session_name, payload__chat_id=config.group_jid).order_by("-id").first()
        result.append({"config_id": config.id, "chat_name": config.name, "state": event.state if event else "not_started", "error_code": event.error_code if event else "", "summary": config.snapshot.get("participant_sync")})
    return result
