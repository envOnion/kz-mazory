"""Team-scoped physical-object identity from explicitly evidenced attributes."""

import hashlib
import json
import unicodedata

from django.db import IntegrityError, transaction
from django.db.models import Q

from .deduplication import normalize_deal_name
from .models import ObjectAlias, ObjectIdentity, Team

IDENTITY_VERSION = 2
IDENTITY_FIELDS = ("external_id", "name", "city", "address", "phase")


def _value(value, *, name=False):
    if not isinstance(value, str):
        return ""
    value = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    # Only display names may discard the conventional ЖК/БЦ prefixes. External
    # IDs, addresses and building phases retain legally significant punctuation.
    return normalize_deal_name(value, max_length=None) if name else value


def normalize_attributes(attributes):
    if not isinstance(attributes, dict):
        return {}
    aliases = {"object_external_id": "external_id", "object_name": "name",
               "object_city": "city", "object_address": "address", "object_phase": "phase"}
    normalized = {}
    for key, value in attributes.items():
        field = aliases.get(key, key)
        if field in IDENTITY_FIELDS:
            value = _value(value, name=field == "name")
            if value:
                normalized[field] = value
    return normalized


def strong_identity_key(attributes):
    """Return a key only when independent attributes distinguish the object."""
    attrs = normalize_attributes(attributes)
    enough = bool(attrs.get("external_id")) or all(attrs.get(field) for field in ("name", "city", "address"))
    if not enough:
        return None
    identity_fields = ("external_id",) if attrs.get("external_id") else IDENTITY_FIELDS
    payload = {"version": IDENTITY_VERSION, **{key: attrs[key] for key in identity_fields if key in attrs}}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def resolve_existing(team_id, attributes):
    """Resolve only a strong exact key; return a bounded shortlist otherwise."""
    attrs = normalize_attributes(attributes)
    key = strong_identity_key(attrs)
    if key:
        matches = list(ObjectIdentity.objects.filter(team_id=team_id, identity_key=key, state="confirmed")[:2])
        if len(matches) == 1:
            return {"state": "matched", "object": matches[0], "candidates": [matches[0].pk], "identity_key": key}
        if len(matches) > 1:
            return {"state": "conflict", "object": None, "candidates": [item.pk for item in matches], "identity_key": key}
    # This is retrieval assistance only; a page with no result never proves absence.
    query = ObjectIdentity.objects.filter(team_id=team_id, state="confirmed")
    candidate_ids = set()
    attribute_match = Q(pk__in=[])
    for field in ("external_id", "city", "address", "phase"):
        if attrs.get(field):
            attribute_match |= Q(**{field: attrs[field]})
    if attrs:
        candidate_ids.update(query.filter(attribute_match).values_list("id", flat=True)[:11])
    if attrs.get("name"):
        display_name = attributes.get("object_name", attributes.get("name", ""))
        candidate_ids.update(query.filter(canonical_name__iexact=display_name).values_list("id", flat=True)[:11])
        alias_ids = ObjectAlias.objects.filter(
            object_identity__team_id=team_id, object_identity__state="confirmed",
            normalized_name=normalize_deal_name(display_name),
        ).values_list("object_identity_id", flat=True)[:11]
        candidate_ids.update(alias_ids)
    rows = list(query.filter(pk__in=candidate_ids).order_by("id")[:11])
    return {"state": "unresolved", "object": None, "candidates": [row.pk for row in rows[:10]],
            "has_more": len(rows) > 10, "identity_key": key}


@transaction.atomic
def create_confirmed(team_id, canonical_name, attributes, *, distinct_confirmed, provenance, decision=None):
    """Create only after a separate comparison established a distinct identity."""
    attrs = normalize_attributes(attributes)
    key = strong_identity_key(attrs)
    if not distinct_confirmed or not key or not canonical_name.strip():
        return None
    Team.objects.select_for_update().get(pk=team_id)
    try:
        obj, _ = ObjectIdentity.objects.get_or_create(
            team_id=team_id, identity_key=key,
            defaults={"canonical_name": canonical_name.strip(), "identity_attributes": attrs,
                      "identity_version": IDENTITY_VERSION, "state": "confirmed",
                      "external_id": attrs.get("external_id", ""), "city": attrs.get("city", ""),
                      "address": attrs.get("address", ""), "phase": attrs.get("phase", "")},
        )
    except IntegrityError:
        obj = ObjectIdentity.objects.get(team_id=team_id, identity_key=key)
    existing = normalize_attributes(obj.identity_attributes)
    existing.update({field: getattr(obj, field) for field in ("external_id", "city", "address", "phase") if getattr(obj, field)})
    if obj.state != "confirmed" or any(attrs.get(field) and existing.get(field) and attrs[field] != existing[field]
            for field in ("external_id", "city", "address", "phase")):
        return None
    alias = normalize_deal_name(canonical_name)
    if alias:
        ObjectAlias.objects.get_or_create(object_identity=obj, normalized_name=alias,
            defaults={"provenance": provenance or {}, "decision": decision})
    return obj
