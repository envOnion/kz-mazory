"""Bounded, evidenced entity resolution; domain writes still use FactCandidate.

The supplied comparison function owns provider calls and durable checkpoints. Its
exceptions deliberately propagate so a worker can yield and resume the same step.
"""

from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
import unicodedata

from django.db.models import Q
from django.utils.dateparse import parse_datetime

from .deduplication import normalize_deal_name
from .message_context import source_scope
from .message_time import source_time
from .models import Commitment, FactEvent, FinancialRecord, ObjectAlias, ObjectIdentity, Project, ProjectAlias, PaymentScheduleItem
from .object_identity import create_confirmed, normalize_attributes, strong_identity_key

PAGE_SIZE = 10
MAX_PAGES = 3
ROLES = {"request", "promise", "deadline", "fulfillment", "cancellation", "source", "identity", "amount", "date", "contract", "party"}


def _origin_role(fact):
    return "request" if fact.get("assignment_kind") == "assignment" else "promise"


def catalog_fingerprint(team_id, fact, *, object_id=None, project_id=None):
    """Track only identity candidates relevant to this observation, locally."""
    import hashlib
    from .context_tokens import canonical_json
    from .facts import json_value
    attrs = normalize_attributes(fact)
    names = normalize_deal_name(fact.get("object_name", ""))
    object_query = Q(pk=object_id) if object_id else Q(pk__in=[])
    for field in ("external_id", "city", "address"):
        if attrs.get(field):
            object_query |= Q(**{field: attrs[field]})
    if names:
        object_query |= Q(aliases__normalized_name=names) | Q(canonical_name__iexact=fact["object_name"])
    objects = ObjectIdentity.objects.filter(team_id=team_id).filter(object_query).distinct()
    deal_query = Q(pk=project_id) if project_id else Q(pk__in=[])
    deal_query |= Q(object_identity__in=objects)
    if names:
        deal_query |= Q(normalized_name=names) | Q(aliases__normalized_name=names) | Q(name__iexact=fact["object_name"])
    if fact.get("deal_contract_number"):
        deal_query |= Q(contract_number__iexact=fact["deal_contract_number"])
    projects = Project.objects.filter(team_id=team_id).filter(deal_query).distinct()
    promise_query = Q(pk=fact["commitment_id"]) if fact.get("commitment_id") else Q(pk__in=[])
    if fact.get("promise_message_id"):
        promise_query |= Q(source_message_id=fact["promise_message_id"])
    if fact.get("responsible_name"):
        promise_query |= Q(project__in=projects, responsible_name__iexact=fact["responsible_name"])
    commitments = Commitment.objects.filter(team_id=team_id).filter(promise_query)
    state = {}
    for name, query, fields in (
        ("objects", objects, ("id", "version", "state", "identity_key", "external_id", "city", "address", "phase")),
        ("deals", projects, ("id", "object_identity_id", "company_id", "company__name", "normalized_name", "contract_number", "deal_period", "archived")),
        ("commitments", commitments, ("id", "version", "status", "source_message_id", "project_id", "responsible_name", "commitment_text")),
        ("payments", FinancialRecord.objects.filter(project__in=projects), ("id", "candidate_id", "candidate__proposed_changes", "amount", "currency", "payment_date", "event_kind", "is_verified")),
        ("payment_events", FactEvent.objects.filter(project__in=projects, is_superseded=False,
            event_type__in=["payment", "payment_schedule", "reported_invoice", "reported_balance", "reported_debt", "reported_transfer", "reported_cumulative"]),
            ("id", "decision_id", "event_type", "payload", "is_superseded")),
        ("payment_plans", PaymentScheduleItem.objects.filter(project__in=projects), ("id", "version", "state", "fact_event_id")),
        ("object_aliases", ObjectAlias.objects.filter(object_identity__in=objects), ("id", "object_identity_id", "normalized_name")),
        ("deal_aliases", ProjectAlias.objects.filter(project__in=projects), ("id", "project_id", "normalized_name")),
    ):
        # This is a local invalidation digest, never an AI input. Hash the
        # complete relevant selection with bounded memory: a 31-row preview
        # cannot prove that an older payment beyond its boundary is unchanged.
        digest, count = hashlib.sha256(), 0
        for row in query.order_by("id").values(*fields).iterator(chunk_size=256):
            encoded = canonical_json(json_value(row)).encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            count += 1
        state[name] = {"count": count, "rows_sha256": digest.hexdigest()}
    return hashlib.sha256(canonical_json(json_value(state)).encode()).hexdigest()


def _text(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split()) if isinstance(value, str) else ""


def _integer(value):
    return type(value) is int and value > 0


def _when(message):
    return source_time(message)[0]


class Resolver:
    def __init__(self, fact, raw, compare):
        self.fact = deepcopy(fact)
        self.raw, self.compare = raw, compare
        self.team_id = raw.config.team_id if raw.config_id else raw.team_id
        self.scope = source_scope(raw)
        self.retrieval = {"policy": "entity-resolution-v1", "team_id": self.team_id,
                          "source": {"config_id": raw.config_id, "source": raw.source,
                                     "session_name": raw.session_name, "chat_id": raw.chat_id}, "stages": {}}
        self.obj = None
        self.project = None
        self.messages = {}

    def result(self, state, reason):
        return {"fact": self.fact, "object_id": self.obj.pk if self.obj else None,
                "object_version": self.obj.version if self.obj else None,
                "project_id": self.project.pk if self.project else None,
                "project_version": self.project.version if self.project else None,
                "state": state, "reason": reason, "retrieval": self.retrieval,
                "source_message_ids": sorted({ref["raw_message_id"] for ref in self.fact.get("evidence_messages", [])
                                              if isinstance(ref, dict) and _integer(ref.get("raw_message_id"))})}

    def evidence(self):
        refs = self.fact.get("evidence_messages", [])
        if not isinstance(refs, list) or len(refs) > 80:
            return False
        refs = deepcopy(refs)
        if not refs and isinstance(self.fact.get("evidence"), str):
            anchor = self.fact.get("evidence_message_id", self.raw.id)
            refs = [{"raw_message_id": anchor, "quote": self.fact["evidence"],
                     "role": _origin_role(self.fact) if self.fact.get("fact_type") == "commitment" else "source"}]
        if not refs or any(not isinstance(ref, dict) or not _integer(ref.get("raw_message_id"))
                           or not isinstance(ref.get("quote"), str) or not ref["quote"].strip()
                           or ref.get("role") not in ROLES for ref in refs):
            return False
        ids = {ref["raw_message_id"] for ref in refs}
        self.messages = {row.pk: row for row in self.scope.filter(pk__in=ids)}
        if ids != set(self.messages) or any(ref["quote"] not in self.messages[ref["raw_message_id"]].content for ref in refs):
            return False
        self.fact["evidence_messages"] = refs
        if self.fact.get("fact_type") == "commitment" and not self.fact.get("promise_message_id"):
            promises = {ref["raw_message_id"] for ref in refs if ref["role"] == _origin_role(self.fact)}
            if len(promises) == 1:
                self.fact["promise_message_id"] = next(iter(promises))
        return True

    def grounded(self, value, *, name=False):
        needle = normalize_deal_name(value, max_length=None) if name else _text(value)
        if not needle:
            return False
        return any(re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)",
                             normalize_deal_name(ref["quote"], max_length=None) if name else _text(ref["quote"]))
                   for ref in self.fact["evidence_messages"])

    def pages(self, stage, queryset, parameters):
        """Fetch at most 31 rows through an indexed predicate, never a catalog."""
        rows = list(queryset.order_by("id").distinct()[:PAGE_SIZE * MAX_PAGES + 1])
        complete = len(rows) <= PAGE_SIZE * MAX_PAGES
        rows = rows[:PAGE_SIZE * MAX_PAGES]
        coverage = {"parameters": parameters, "page_size": PAGE_SIZE, "page_budget": MAX_PAGES,
                    "retrieved": len(rows), "complete": complete, "has_more": not complete,
                    "candidate_ids": [row.pk for row in rows], "pages": []}
        self.retrieval["stages"][stage] = coverage
        return rows, coverage

    def comparison(self, stage, rows, coverage, snapshot, *, mention=None):
        """Validate every model-selected ID and literal source before accepting it."""
        mention = mention or self.fact
        coverage["new_identity_eligible"] = bool(coverage["complete"] and (
            stage == "object" and strong_identity_key(normalize_attributes(self.fact))
            or stage == "deal" and self.obj and self.fact.get("deal_contract_number") and self.fact.get("company_name")
            or stage == "commitment" and self.fact.get("commitment_status", "pending") == "pending"
                and self.fact.get("responsible_name") and self.fact.get("commitment_text")
                and any(ref["role"] == _origin_role(self.fact)
                    and ref["raw_message_id"] == self.fact.get("promise_message_id")
                    for ref in self.fact.get("evidence_messages", []))
        ))
        chunks = [rows[index:index + PAGE_SIZE] for index in range(0, len(rows), PAGE_SIZE)] or [[]]
        matches, distinct = [], True
        for page_number, chunk in enumerate(chunks, start=1):
            candidates = [snapshot(row) for row in chunk]
            page = {**coverage, "page": page_number, "candidate_ids": [row.pk for row in chunk]}
            answer = self.compare(f"{stage}:{page_number}", mention, candidates, page)
            if not isinstance(answer, dict):
                return None, False, "invalid_comparison"
            decision, selected = answer.get("decision"), answer.get("candidate_id")
            refs = answer.get("evidence_messages")
            valid = (decision in {"match", "distinct", "insufficient_evidence"}
                     and isinstance(answer.get("reason"), str) and bool(answer["reason"].strip())
                     and isinstance(refs, list) and 0 < len(refs) <= 80)
            allowed_ids = {ref["raw_message_id"] for ref in self.fact["evidence_messages"]}
            allowed_ids.update(ref["raw_message_id"] for item in candidates for ref in item.get("evidence_messages", []))
            if valid:
                ids = {ref.get("raw_message_id") for ref in refs if isinstance(ref, dict) and _integer(ref.get("raw_message_id"))}
                sources = {row.pk: row for row in self.scope.filter(pk__in=ids)}
                mention_ids = {ref["raw_message_id"] for ref in self.fact["evidence_messages"]}
                valid = (len(ids) > 0 and bool(ids & mention_ids) and ids <= allowed_ids and ids == set(sources)
                         and all(isinstance(ref, dict) and _integer(ref.get("raw_message_id"))
                                 and isinstance(ref.get("quote"), str) and bool(ref["quote"].strip())
                                 and ref["raw_message_id"] in sources
                                 and ref["quote"] in sources[ref["raw_message_id"]].content for ref in refs))
            if decision == "match":
                valid = valid and _integer(selected) and selected in {row.pk for row in chunk}
            else:
                valid = valid and selected is None
            coverage["pages"].append({"page": page_number, "decision": decision,
                                      "candidate_id": selected, "validated": bool(valid)})
            if not valid:
                return None, False, "invalid_comparison_evidence"
            if decision == "match":
                matches.append(next(row for row in chunk if row.pk == selected))
            elif decision != "distinct":
                distinct = False
        if len(matches) == 1 and coverage["complete"]:
            return matches[0], False, "semantic_match"
        if matches:
            return None, False, "ambiguous_or_incomplete_search"
        return None, distinct and coverage["complete"], "distinct" if distinct and coverage["complete"] else "insufficient_evidence"

    def object_snapshot(self, row):
        aliases = list(row.aliases.order_by("id")[:PAGE_SIZE + 1])
        refs = []
        for alias in aliases[:PAGE_SIZE]:
            provenance = alias.provenance if isinstance(alias.provenance, dict) else {}
            refs.extend(provenance.get("evidence_messages", []))
        # Source IDs in catalog provenance are still checked against this source.
        ids = {ref.get("raw_message_id") for ref in refs if isinstance(ref, dict) and _integer(ref.get("raw_message_id"))}
        allowed = set(self.scope.filter(pk__in=ids).values_list("id", flat=True))
        return {"id": row.pk, "name": row.canonical_name, "attributes": self.object_attributes(row),
                "aliases": [alias.normalized_name for alias in aliases[:PAGE_SIZE]],
                "aliases_complete": len(aliases) <= PAGE_SIZE,
                "evidence_messages": [ref for ref in refs if ref.get("raw_message_id") in allowed]}

    @staticmethod
    def object_attributes(row):
        attrs = dict(row.identity_attributes or {})
        attrs.setdefault("name", row.canonical_name)
        attrs.update({field: getattr(row, field) for field in ("external_id", "city", "address", "phase") if getattr(row, field)})
        return normalize_attributes(attrs)

    def object_conflict(self, row, attrs):
        existing = self.object_attributes(row)
        return any(attrs.get(key) and existing.get(key) and attrs[key] != existing[key]
                   for key in ("external_id", "city", "address", "phase"))

    def remember_alias(self):
        name = normalize_deal_name(self.fact.get("object_name", ""))
        if name:
            ObjectAlias.objects.get_or_create(object_identity=self.obj, normalized_name=name,
                defaults={"provenance": {"raw_message_id": self.raw.pk,
                                          "evidence_messages": self.fact["evidence_messages"],
                                          "policy": "entity-resolution-v1"}})

    def object(self):
        attrs = normalize_attributes(self.fact)
        if not attrs:
            return "absent"
        for field, value in self.fact.items():
            if field.startswith("object_") and value and not self.grounded(value, name=field == "object_name"):
                return "unproven_object_attributes"
        query = Q(pk__in=[])
        key = strong_identity_key(attrs)
        catalog = ObjectIdentity.objects.filter(team_id=self.team_id, state="confirmed")
        if self.raw.project_id:
            bound = Project.objects.filter(pk=self.raw.project_id, team_id=self.team_id, archived=False,
                                           object_identity__state="confirmed").select_related("object_identity").first()
            if bound and bound.object_identity.team_id == self.team_id and not self.object_conflict(bound.object_identity, attrs):
                named = attrs.get("name")
                matches_name = named and (self.object_attributes(bound.object_identity).get("name") == named
                    or bound.object_identity.aliases.filter(normalized_name=named).exists())
                if matches_name or (attrs.get("external_id") and bound.object_identity.external_id == attrs["external_id"]):
                    self.obj = bound.object_identity
                    self.retrieval["stages"]["object"] = {"complete": True, "method": "confirmed_source_project",
                                                          "candidate_ids": [self.obj.pk]}
                    return "matched"
        if key:
            keyed = list(catalog.filter(identity_key=key)[:2])
            if len(keyed) == 1:
                if self.object_conflict(keyed[0], attrs):
                    return "conflicting_object_attributes"
                self.retrieval["stages"]["object"] = {"parameters": {"identity_key": key}, "complete": True,
                                                      "candidate_ids": [keyed[0].pk], "method": "strong_key"}
                self.obj = keyed[0]
                self.remember_alias()
                return "matched"
        if attrs.get("external_id"):
            exact_query = catalog.filter(external_id=attrs["external_id"])
        elif all(attrs.get(field) for field in ("name", "city", "address")):
            exact_query = catalog.filter(city=attrs["city"], address=attrs["address"], phase=attrs.get("phase", "")).filter(
                Q(identity_attributes__name=attrs["name"]) | Q(aliases__normalized_name=attrs["name"]))
        else:
            exact_query = catalog.none()
        exact_rows, exact_coverage = self.pages("object_exact", exact_query, attrs)
        exact_rows = [row for row in exact_rows if not self.object_conflict(row, attrs)]
        if len(exact_rows) == 1 and exact_coverage["complete"]:
            self.obj = exact_rows[0]
            self.remember_alias()
            return "matched"
        if key:
            query |= Q(identity_key=key)
        for field in ("external_id", "city", "address"):
            if attrs.get(field):
                query |= Q(**{field: attrs[field]})
        if attrs.get("name"):
            query |= Q(canonical_name__iexact=self.fact.get("object_name", ""))
            query |= Q(aliases__normalized_name=normalize_deal_name(self.fact.get("object_name", "")))
        rows, coverage = self.pages("object", catalog.filter(query), attrs)
        exact = [row for row in rows if not self.object_conflict(row, attrs) and (
            (key and row.identity_key == key)
            or (attrs.get("external_id") and row.external_id == attrs["external_id"])
            or (all(attrs.get(field) for field in ("name", "city", "address"))
                and all(self.object_attributes(row).get(field) == attrs[field] for field in ("name", "city", "address"))
                and self.object_attributes(row).get("phase", "") == attrs.get("phase", ""))
        )]
        if len(exact) == 1 and coverage["complete"]:
            self.obj = exact[0]
            self.remember_alias()
            return "matched"
        aliases = []
        if attrs.get("name"):
            named = set(ObjectAlias.objects.filter(object_identity_id__in=[row.pk for row in rows],
                                                  normalized_name=normalize_deal_name(self.fact.get("object_name", "")))
                        .values_list("object_identity_id", flat=True))
            for row in rows:
                old = self.object_attributes(row)
                corroborated = bool(attrs.get("external_id") and old.get("external_id") == attrs["external_id"]) or bool(
                    attrs.get("city") and attrs.get("address") and old.get("city") == attrs["city"] and old.get("address") == attrs["address"])
                if row.pk in named and corroborated and not self.object_conflict(row, attrs):
                    aliases.append(row)
        if len(aliases) == 1 and coverage["complete"]:
            self.obj = aliases[0]
            return "matched"
        if not rows and not key:
            # Nothing can be selected and the attributes cannot authorize a new
            # physical object. An existing deal may still have its own key.
            return "insufficient_object_attributes"
        selected, distinct, reason = self.comparison("object", rows, coverage, self.object_snapshot)
        if selected:
            if self.object_conflict(selected, attrs):
                return "conflicting_object_attributes"
            self.obj = selected
            self.remember_alias()
            return "matched"
        if distinct and key:
            name = self.fact.get("object_name") or self.fact.get("object_external_id", "")
            self.obj = create_confirmed(self.team_id, name, self.fact, distinct_confirmed=True,
                provenance={"raw_message_id": self.raw.pk, "evidence_messages": self.fact["evidence_messages"],
                            "policy": "entity-resolution-v1"})
            return "new" if self.obj else "object_creation_unproven"
        return reason

    def project_snapshot(self, row):
        return {"id": row.pk, "name": row.name, "object_id": row.object_identity_id,
                "contract_number": row.contract_number, "period": row.deal_period,
                "company_name": row.company.name if row.company_id else "", "bitrix_id": row.bitrix_id,
                "version": row.version, "evidence_messages": []}

    def project_conflict(self, row):
        return bool((self.obj and row.object_identity_id and row.object_identity_id != self.obj.pk)
                    or (row.object_identity_id and self.object_conflict(row.object_identity, normalize_attributes(self.fact)))
                    or (self.fact.get("deal_contract_number") and row.contract_number
                        and _text(self.fact["deal_contract_number"]) != _text(row.contract_number))
                    or (self.fact.get("deal_period") and row.deal_period and _text(self.fact["deal_period"]) != _text(row.deal_period))
                    or (self.fact.get("company_name") and row.company_id
                        and normalize_deal_name(self.fact["company_name"]) != normalize_deal_name(row.company.name)))

    def bind_project(self, row, method):
        """Keep an existing CRM deal usable before physical-object backfill."""
        self.project = row
        if not self.obj and row.object_identity_id and row.object_identity.team_id == self.team_id and row.object_identity.state == "confirmed":
            self.obj = row.object_identity
        name = normalize_deal_name(self.fact.get("object_name", ""))
        if name and self.grounded(self.fact["object_name"], name=True):
            # ProjectAlias has only a decision FK, so the accepting FactDecision
            # records this provenance before an alias becomes durable authority.
            self.retrieval["confirmed_project_alias"] = {"project_id": row.pk, "normalized_name": name,
                "evidence_messages": self.fact["evidence_messages"], "method": method}
        return "matched"

    def deal(self):
        for field in ("deal_contract_number", "deal_period", "company_name"):
            if self.fact.get(field) and not self.grounded(self.fact[field], name=field == "company_name"):
                return "unproven_deal_attributes"
        query = Q(pk__in=[])
        name = normalize_deal_name(self.fact.get("object_name", ""))
        catalog = Project.objects.filter(team_id=self.team_id, archived=False).select_related("company", "object_identity")
        contract = self.fact.get("deal_contract_number", "")
        names = Q(normalized_name=name) | Q(name__iexact=self.fact.get("object_name", "")) | Q(aliases__normalized_name=name)
        if name and contract and self.fact.get("company_name"):
            # Existing CRM rows intentionally start with a nullable physical
            # object link. A complete name/contract/party match stands alone.
            exact_rows, exact_coverage = self.pages("deal_composite", catalog.filter(contract_number__iexact=contract).filter(names),
                {"name": name, "contract": contract, "company_name": self.fact["company_name"]})
            exact_rows = [row for row in exact_rows if row.company_id and not self.project_conflict(row)]
            if len(exact_rows) == 1 and exact_coverage["complete"]:
                return self.bind_project(exact_rows[0], "strong_composite")
        if self.obj and contract and self.fact.get("company_name"):
            exact_rows, exact_coverage = self.pages("deal_exact", catalog.filter(object_identity=self.obj, contract_number__iexact=contract),
                {"object_id": self.obj.pk, "contract": contract, "company_name": self.fact["company_name"]})
            exact_rows = [row for row in exact_rows if row.company_id and not self.project_conflict(row)]
            if len(exact_rows) == 1 and exact_coverage["complete"]:
                return self.bind_project(exact_rows[0], "strong_composite")
        if self.raw.project_id:
            bound = catalog.filter(pk=self.raw.project_id).first()
            if bound and not self.project_conflict(bound) and (not name or normalize_deal_name(bound.name) == name
                    or (self.obj and bound.object_identity_id == self.obj.pk) or bound.aliases.filter(normalized_name=name).exists()):
                self.retrieval["stages"]["deal"] = {"complete": True, "method": "confirmed_source_project",
                                                    "candidate_ids": [bound.pk]}
                return self.bind_project(bound, "confirmed_source_project")
        if self.obj:
            query |= Q(object_identity_id=self.obj.pk)
        if name:
            query |= names
        if contract:
            query |= Q(contract_number__iexact=contract)
        if self.raw.project_id:
            query |= Q(pk=self.raw.project_id)
        if not self.obj and not name and not contract and not self.raw.project_id:
            return "absent"
        rows, coverage = self.pages("deal", catalog.filter(query),
                                    {"object_id": self.obj.pk if self.obj else None, "name": name, "contract": contract,
                                     "source_project_id": self.raw.project_id})
        eligible = [row for row in rows if not self.project_conflict(row)]
        exact = [row for row in eligible if contract and _text(row.contract_number) == _text(contract)
                 and self.obj and row.object_identity_id == self.obj.pk and self.fact.get("company_name") and row.company_id]
        bound = [row for row in eligible if row.pk == self.raw.project_id and (
            not name or normalize_deal_name(row.name) == name or (self.obj and row.object_identity_id == self.obj.pk))]
        if len(exact) == 1 and coverage["complete"]:
            return self.bind_project(exact[0], "strong_composite")
        if len(bound) == 1 and coverage["complete"]:
            return self.bind_project(bound[0], "confirmed_source_project")
        selected, distinct, reason = self.comparison("deal", rows, coverage, self.project_snapshot)
        if selected:
            if self.project_conflict(selected):
                return "conflicting_deal_attributes"
            return self.bind_project(selected, "semantic_match")
        if distinct and self.fact.get("fact_type") == "project" and self.obj and contract and self.fact.get("company_name"):
            return "new"
        return reason

    def commitment_snapshot(self, row):
        refs = []
        if row.candidate_id:
            refs = (row.candidate.proposed_changes or {}).get("evidence_messages", [])
        if not refs and row.source_message_id:
            original = row.candidate.proposed_changes if row.candidate_id else {}
            refs = [{"raw_message_id": row.source_message_id, "quote": row.source_message.content, "role": _origin_role(original)}]
        ids = {ref.get("raw_message_id") for ref in refs if isinstance(ref, dict) and _integer(ref.get("raw_message_id"))}
        allowed = set(self.scope.filter(pk__in=ids).values_list("id", flat=True))
        return {"id": row.pk, "project_id": row.project_id, "action": row.commitment_text,
                "responsible_name": row.responsible_name, "source_message_id": row.source_message_id,
                "version": row.version, "status": row.status, "deadline_at": row.deadline_at.isoformat() if row.deadline_at else None,
                "evidence_messages": [ref for ref in refs if ref.get("raw_message_id") in allowed]}

    def bind_commitment(self, row):
        if row.project_id != (self.project.pk if self.project else None):
            return "commitment_project_conflict"
        if not row.source_message_id or not self.scope.filter(pk=row.source_message_id).exists():
            return "commitment_origin_unavailable"
        if not self.fact.get("responsible_name") or _text(self.fact["responsible_name"]) != _text(row.responsible_name):
            return "commitment_actor_unproven"
        supplied = self.fact.get("commitment_id")
        if supplied is not None and (not _integer(supplied) or supplied != row.pk
                                    or self.fact.get("base_commitment_version") != row.version):
            return "commitment_version_conflict"
        original_kind = (row.candidate.proposed_changes or {}).get("assignment_kind", "promise") if row.candidate_id else "promise"
        origin_role = _origin_role({"assignment_kind": original_kind})
        original = [ref for ref in self.commitment_snapshot(row)["evidence_messages"] if ref.get("role") == origin_role
                    and ref.get("raw_message_id") == row.source_message_id]
        if not original:
            return "commitment_original_evidence_unavailable"
        refs = self.fact["evidence_messages"]
        status = self.fact.get("commitment_status", "pending")
        deadline = self.fact.get("deadline_at")
        if deadline is not None and not isinstance(deadline, datetime):
            deadline = parse_datetime(deadline) if isinstance(deadline, str) else None
            if deadline is None:
                return "invalid_commitment_deadline"
        if status not in {"pending", "fulfilled", "cancelled"}:
            return "invalid_commitment_status"
        changed = status != row.status or (deadline is not None and deadline != row.deadline_at)
        if not changed:
            return "duplicate"
        role = "fulfillment" if status == "fulfilled" else "cancellation" if status == "cancelled" else "deadline"
        updates = [ref for ref in refs if ref["role"] == role and ref["raw_message_id"] != row.source_message_id]
        if not updates:
            return "commitment_lifecycle_evidence_unavailable"
        ids = {ref["raw_message_id"] for ref in updates}
        messages = list(self.scope.filter(pk__in=ids))
        if any(_when(message) is None for message in messages) or not _when(row.source_message):
            return "commitment_chronology_unknown"
        latest = max(messages, key=lambda message: (_when(message), message.pk))
        if any(_when(message) < _when(row.source_message) for message in messages):
            return "commitment_chronology_conflict"
        # Last accepted lifecycle evidence, rather than the DB write time, orders
        # late-arriving historical messages and prevents a stale state rollback.
        previous = (row.candidate.proposed_changes or {}).get("evidence_messages", []) if row.candidate_id else []
        previous_ids = {ref.get("raw_message_id") for ref in previous if isinstance(ref, dict) and ref.get("role") in {"fulfillment", "cancellation", "deadline"}}
        events, coverage = self.pages("commitment_lifecycle", FactEvent.objects.filter(team_id=self.team_id, project_id=row.project_id,
            payload__commitment_id=row.pk, is_superseded=False), {"commitment_id": row.pk})
        if not coverage["complete"]:
            return "incomplete_commitment_history"
        for event in events:
            previous_ids.update(ref.get("raw_message_id") for ref in event.payload.get("evidence_messages", [])
                                if isinstance(ref, dict) and ref.get("role") in {"fulfillment", "cancellation", "deadline"})
        previous_ids = {pk for pk in previous_ids if _integer(pk)}
        prior_messages = list(self.scope.filter(pk__in=previous_ids))
        if any(_when(message) and _when(message) > _when(latest) for message in prior_messages):
            return "stale_commitment_update"
        if row.status in {"fulfilled", "cancelled"} and status == "pending":
            return "terminal_commitment_transition"
        self.fact["commitment_id"] = row.pk
        self.fact["base_commitment_version"] = row.version
        self.fact["promise_message_id"] = row.source_message_id
        self.fact["assignment_kind"] = original_kind
        # Preserve the promise/action/actor; lifecycle evidence only proposes
        # status/deadline changes to the existing versioned commitment.
        self.fact["commitment_text"] = row.commitment_text
        self.fact["responsible_name"] = row.responsible_name
        self.fact["evidence_messages"] = list({(ref["raw_message_id"], ref["quote"], ref["role"]): ref for ref in original + refs
                                               if ref["role"] != origin_role or ref["raw_message_id"] == row.source_message_id}.values())
        if role == "fulfillment":
            self.fact["fulfillment_message_id"] = latest.pk
            self.fact["fulfilled_at"] = _when(latest).isoformat()
        elif role == "deadline":
            self.fact["deadline_message_id"] = latest.pk
        return "matched"

    def commitment(self):
        origin = self.fact.get("promise_message_id")
        if origin is not None and (not _integer(origin) or not self.scope.filter(pk=origin).exists()):
            return "commitment_origin_unavailable"
        if self.fact.get("assignment_kind", "promise") == "promise" and origin:
            promise = self.scope.get(pk=origin)
            actor = source_time(promise)[1] or promise.sender_phone
            if actor:
                self.fact["responsible_name"] = actor
        actor = self.fact.get("responsible_name", "")
        if not actor:
            return "commitment_actor_unproven"
        if self.fact.get("assignment_kind", "promise") != "promise" and not self.grounded(actor):
            return "commitment_actor_unproven"
        query = Q(pk__in=[])
        if origin:
            query |= Q(source_message_id=origin)
        query |= Q(project_id=self.project.pk if self.project else None, responsible_name__iexact=actor)
        explicit = self.fact.get("commitment_id")
        if explicit is not None:
            if not _integer(explicit):
                return "invalid_commitment_id"
            query |= Q(pk=explicit)
        rows, coverage = self.pages("commitment", Commitment.objects.filter(team_id=self.team_id, is_verified=True,
            source_message_id__in=self.scope.values("id")).filter(query).select_related("candidate", "source_message"),
            {"project_id": self.project.pk if self.project else None, "responsible_name": actor, "promise_message_id": origin})
        exact = [row for row in rows if row.source_message_id == origin and row.project_id == (self.project.pk if self.project else None)
                 and _text(row.responsible_name) == _text(actor) and _text(row.commitment_text) == _text(self.fact.get("commitment_text", ""))]
        if explicit:
            exact = [row for row in exact if row.pk == explicit]
        if len(exact) == 1 and coverage["complete"]:
            return self.bind_commitment(exact[0])
        selected, distinct, reason = self.comparison("commitment", rows, coverage, self.commitment_snapshot)
        if selected:
            return self.bind_commitment(selected)
        if distinct and self.fact.get("commitment_status", "pending") == "pending" and origin:
            if not any(ref["role"] == _origin_role(self.fact) and ref["raw_message_id"] == origin for ref in self.fact["evidence_messages"]):
                return "commitment_original_evidence_unavailable"
            return "new"
        return reason

    @staticmethod
    def same_payment(data, other):
        left_ref, right_ref = Resolver.payment_reference(data), Resolver.payment_reference(other)
        if left_ref and right_ref and left_ref != right_ref:
            return False
        fields = ("payment_kind", "direction", "currency", "amount", "payment_date", "reverses_id")
        defaults = {"payment_kind": "increment", "direction": "income"}
        for field in fields:
            left, right = data.get(field, defaults.get(field)), other.get(field, defaults.get(field))
            if field == "amount":
                try:
                    if Decimal(str(left)) != Decimal(str(right)):
                        return False
                except InvalidOperation:
                    return False
            elif str(left) != str(right):
                return False
        # A common message can contain several equal payments: require the same
        # literal monetary assertion as well as the event attributes.
        def refs(fact):
            return {(ref.get("raw_message_id"), ref.get("quote")) for ref in fact.get("evidence_messages", [])
                    if ref.get("role") in {"source", "amount"}}
        return bool(refs(data) & refs(other))

    @staticmethod
    def payment_reference(data):
        value = _text(data.get("payment_reference", ""))
        kind = data.get("payment_reference_kind", "unknown")
        return (kind, value) if value and kind in {"transaction", "invoice"} else None

    def payment_snapshot(self, item):
        row, data, entity_kind = item
        refs = data.get("evidence_messages", [])
        allowed = set(self.scope.filter(pk__in=[ref.get("raw_message_id") for ref in refs if isinstance(ref, dict)]).values_list("pk", flat=True))
        return {"id": row.pk, "entity_kind": entity_kind, "project_id": self.project.pk,
                **{field: str(data[field]) if field in {"amount", "payment_date"} else data[field]
                   for field in ("payment_kind", "amount", "currency", "payment_date", "direction", "payment_reference", "payment_reference_kind") if field in data},
                "evidence_messages": [ref for ref in refs if isinstance(ref, dict) and ref.get("raw_message_id") in allowed]}

    def compare_payments(self, stage, items, coverage):
        # FinancialRecord and FactEvent PKs have separate namespaces. Never
        # offer their integer IDs in one model-selection list.
        by_pk = {row.pk: (row, data, kind) for row, data, kind in items}
        return self.comparison(stage, [row for row, _, _ in items], coverage,
                               lambda row: self.payment_snapshot(by_pk[row.pk]))

    def payment_distinction(self, similar):
        current_ref = self.payment_reference(self.fact)
        previous_refs = [self.payment_reference(data) for _, data, _ in similar]
        if current_ref and previous_refs and all(ref and ref[0] == current_ref[0] and ref[1] != current_ref[1] for ref in previous_refs):
            return {"kind": "different_grounded_references", "reference_kind": current_ref[0],
                    "current_reference": current_ref[1], "previous_references": sorted({ref[1] for ref in previous_refs})}
        pattern = re.compile(r"\b(?:ещ[её]\s+(?:(?:один|одна|одно)\s+)?(?:плат[её]ж|перевод|поступлен\w*)|втор(?:ой|ая|ое)\s+(?:плат[её]ж|перевод|поступлен\w*)|"
            r"друг(?:ой|ая|ое)\s+(?:плат[её]ж|перевод|поступлен\w*)|отдельн\w*\s+(?:плат[её]ж|перевод|поступлен\w*)|another\s+(?:payment|transfer)|second\s+(?:payment|transfer))\b", re.I)
        refs = [ref for ref in self.fact["evidence_messages"] if pattern.search(ref["quote"])]
        return {"kind": "explicit_separate_operation", "evidence_messages": refs} if refs else None

    def grounded_payment_reference(self):
        reference = self.payment_reference(self.fact)
        if not reference:
            return not self.fact.get("payment_reference")
        label = (r"(?:транзакц\w*|transaction|tx(?:id)?|плат[её]жн\w*\s+(?:поручен\w*|операц\w*)|"
                 r"(?:плат[её]ж|перевод|операц\w*)\s*(?:№|no\.?|номер|id|ref))" if reference[0] == "transaction"
                 else r"(?:сч[её]т\w*|invoice|инвойс\w*)")
        pattern = re.compile(label + r"[^\n.;]{0,32}(?<!\w)" + re.escape(reference[1]) + r"(?!\w)", re.I)
        return any(pattern.search(_text(ref["quote"])) for ref in self.fact["evidence_messages"])

    def payment(self):
        if not self.project:
            return "unbound_payment"
        if self.fact.get("payment_reference") and (not self.grounded(self.fact["payment_reference"]) or not self.grounded_payment_reference()):
            return "unproven_payment_reference"
        self.fact.setdefault("currency", self.project.currency)
        ids = {ref["raw_message_id"] for ref in self.fact["evidence_messages"]}
        kind = self.fact.get("payment_kind", "increment")
        origin_query = Q(candidate__trace__raw_message_id__in=ids) | Q(candidate__evidence__raw_message_id__in=ids)
        amount = self.fact.get("amount")
        currency = self.fact.get("currency", self.project.currency)
        payment_date = self.fact.get("payment_date")
        similar_query = Q(pk__in=[])
        if amount is not None and payment_date:
            similar_query = Q(amount=amount, currency=currency, payment_date=payment_date,
                              direction=self.fact.get("direction", "income"))
        reference = self.payment_reference(self.fact)
        reference_query = Q(pk__in=[])
        if reference:
            reference_query = Q(candidate__proposed_changes__payment_reference_kind=reference[0],
                                candidate__proposed_changes__payment_reference__iexact=self.fact["payment_reference"])
        query = FinancialRecord.objects.filter(project=self.project, project__team_id=self.team_id, is_verified=True).filter(
            origin_query | similar_query | reference_query).exclude(candidate__status="superseded")
        rows, coverage = self.pages("payment", query.select_related("candidate__trace__raw_message"),
                                    {"project_id": self.project.pk, "origin_message_ids": sorted(ids), "payment_kind": kind})
        financial = [(row, row.candidate.proposed_changes if row.candidate_id else {
            "amount": str(row.amount), "currency": row.currency, "payment_date": str(row.payment_date),
            "direction": row.direction, "payment_kind": "reversal" if row.reverses_id else "increment"}, "financial_record") for row in rows]
        duplicate = [row for row, data, _ in financial if self.same_payment(self.fact, data)]
        if duplicate:
            coverage["duplicate_ids"] = [row.pk for row in duplicate]
            return "duplicate"
        event_origin = Q(decision__candidate__trace__raw_message_id__in=ids) | Q(decision__candidate__evidence__raw_message_id__in=ids)
        event_similar = Q(pk__in=[])
        if amount is not None and payment_date:
            try:
                decimal = Decimal(str(amount))
                amount_forms = [str(decimal), str(decimal.normalize()), str(decimal.quantize(Decimal("0.01"))), float(decimal)]
                event_similar = Q(payload__amount__in=amount_forms, payload__payment_date=str(payment_date), payload__currency=currency)
            except (InvalidOperation, ValueError, OverflowError):
                return "invalid_payment_amount"
        event_reference = Q(payload__payment_reference_kind=reference[0], payload__payment_reference__iexact=self.fact["payment_reference"]) if reference else Q(pk__in=[])
        events, event_coverage = self.pages("payment_observation", FactEvent.objects.filter(team_id=self.team_id, project=self.project,
            is_superseded=False).filter(event_origin | event_similar | event_reference).filter(
                    event_type__in=["payment", "payment_schedule", "reported_invoice", "reported_balance", "reported_debt", "reported_transfer", "reported_cumulative"]),
            {"project_id": self.project.pk, "origin_message_ids": sorted(ids), "payment_kind": kind})
        observations = [(row, row.payload, "fact_event") for row in events]
        duplicate_events = [row.pk for row, data, _ in observations if self.same_payment(self.fact, data)]
        if duplicate_events:
            event_coverage["duplicate_ids"] = duplicate_events
            return "duplicate"
        # Revised originals cannot be treated as a second operation. Exact equal
        # amounts/dates on independent messages are deliberately not queried.
        origins = list(self.scope.filter(pk__in=ids).values("source", "session_name", "message_id"))
        revised_origin_query = Q(pk__in=[])
        for origin in origins:
            revised_origin_query |= Q(candidate__trace__raw_message__source=origin["source"],
                              candidate__trace__raw_message__session_name=origin["session_name"],
                              candidate__trace__raw_message__message_id=origin["message_id"])
        if FinancialRecord.objects.filter(project=self.project, is_verified=True).filter(revised_origin_query).exclude(
                candidate__trace__raw_message_id__in=ids).exclude(candidate__status="superseded").exists():
            return "payment_source_revision_requires_correction"
        if not coverage["complete"] or not event_coverage["complete"]:
            return "incomplete_payment_search"
        similar = []
        for item in financial + observations:
            row, data, entity_kind = item
            if data.get("payment_kind", "increment") != kind:
                continue
            other_ref = self.payment_reference(data)
            same_ref = reference and reference == other_ref
            attrs = dict(data)
            # Reference identity is evaluated above. Monetary/event attributes
            # select candidates even when references explicitly distinguish them.
            attrs.pop("payment_reference", None)
            attrs.pop("payment_reference_kind", None)
            # Compare event attributes independently of literal source identity.
            attrs["evidence_messages"] = self.fact["evidence_messages"]
            same_attrs = self.same_payment(self.fact, attrs)
            if same_ref and (reference[0] == "transaction" or kind == "invoice"):
                if not same_attrs:
                    return "payment_reference_requires_correction"
                coverage["duplicate_reference"] = {"entity_kind": entity_kind, "id": row.pk, "reference_kind": reference[0], "reference": reference[1]}
                return "duplicate"
            if same_attrs:
                similar.append(item)
        coverage["matching_shortlist_checked"] = True
        coverage["matching_candidate_count"] = len(similar)
        event_coverage["matching_shortlist_checked"] = True
        if not similar:
            coverage["distinguishing_proof"] = {"kind": "no_matching_event_in_complete_search", "origin_message_ids": sorted(ids)}
            return "new"
        proof = self.payment_distinction(similar)
        coverage["distinguishing_proof"] = proof
        if proof and proof["kind"] == "different_grounded_references":
            return "new"
        for stage, items, stage_coverage in (("payment", [item for item in similar if item[2] == "financial_record"], coverage),
                                              ("payment_observation", [item for item in similar if item[2] == "fact_event"], event_coverage)):
            if not items:
                continue
            selected, distinct, _ = self.compare_payments(stage, items, stage_coverage)
            if selected:
                return "payment_common_origin_unproven"
            if not distinct:
                return "payment_identity_unresolved"
        return "new" if proof else "payment_independent_operation_unproven"

    def run(self):
        if not self.evidence():
            return self.result("review", "invalid_source_evidence")
        obj_state = self.object()
        if obj_state in {"unproven_object_attributes", "conflicting_object_attributes", "object_creation_unproven"}:
            return self.result("review", obj_state)
        if obj_state not in {"matched", "new", "absent"}:
            self.retrieval["unresolved_object_reason"] = obj_state
        deal_state = self.deal()
        if obj_state not in {"matched", "new", "absent"} and deal_state != "matched":
            return self.result("review", obj_state)
        if deal_state not in {"matched", "new"}:
            # General obligations can exist without a deal. Unknown deal identity
            # never becomes a wildcard for a known project's obligation.
            if self.fact.get("fact_type") != "commitment" or self.fact.get("object_name") or self.obj:
                return self.result("review", deal_state)
        if self.fact.get("fact_type") == "commitment":
            state = self.commitment()
        elif self.fact.get("fact_type") == "payment":
            state = self.payment()
        else:
            state = deal_state
        return self.result(state if state in {"matched", "new", "duplicate"} else "review", state)


def resolve(fact: dict, raw, compare) -> dict:
    """Resolve one validated observation in its immutable tenant/source scope."""
    result = Resolver(fact, raw, compare).run()
    result["catalog_fingerprint"] = catalog_fingerprint(raw.config.team_id if raw.config_id else raw.team_id,
        result["fact"], object_id=result["object_id"], project_id=result["project_id"])
    return result
