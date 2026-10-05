"""CRM adapter. Calls happen only in worker tasks; imports create review proposals."""

import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import escape

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .bitrix_config import checked_bitrix_webhook_base, effective_webhook_url
from .models import (
    BitrixDealChangeLog,
    BitrixSettings,
    CandidateCrmMatch,
    FactCandidate,
    FactEvidence,
    MessageProcessingTrace,
    Project,
    ProjectRevision,
    RawMessage,
    Team,
)
from .providers import ProviderUnavailable, checked_url


READ_ONLY_METHODS = frozenset(
    {
        "crm.company.list",
        "crm.company.get",
        "crm.deal.list",
        "crm.deal.get",
    }
)
OBJECT_FIELD_PATTERN = re.compile(r"^UF_CRM_[A-Z0-9_]+$")
OBJECT_PREFIXES = re.compile(
    r"\b(?:жк|бц|тоо|too|ао|мжд|объект|мкр|микрорайон)\b",
    re.IGNORECASE,
)
COMPANY_LEGAL_FORMS = re.compile(
    (
        r"\b(?:тоо|too|ао|ип|ооо|зао|пао|llp|ltd|limited|inc|corp|"
        r"corporation)\b"
    ),
    re.IGNORECASE,
)
CRM_MATCH_DEADLINE_SECONDS = 60
CRM_MATCH_MAX_PAGES_PER_LIST = 10
CRM_MATCH_REQUEST_TIMEOUT_SECONDS = 8
STRONG_MATCH_REASONS = frozenset(
    {"title_exact", "object_field_exact", "existing_project_bitrix_id"}
)


@dataclass(frozen=True)
class CrmReadConfig:
    webhook_base: str
    deal_category_id: int
    deal_object_field_code: str

    @classmethod
    def from_model(cls, config):
        return cls(
            webhook_base=checked_bitrix_webhook_base(
                effective_webhook_url(config)
            ),
            deal_category_id=config.deal_category_id,
            deal_object_field_code=config.deal_object_field_code,
        )


def _normalized_words(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[«»\"'“”„()\[\]{}]", " ", text)
    text = re.sub(r"[^\w\-]+", " ", text, flags=re.UNICODE)
    return re.sub(r"[_\s]+", " ", text).strip()


def normalize_company_name(value):
    return re.sub(
        r"\s+", " ", COMPANY_LEGAL_FORMS.sub(" ", _normalized_words(value))
    ).strip()


def normalize_crm_object_name(value):
    return re.sub(
        r"\s+", " ", OBJECT_PREFIXES.sub(" ", _normalized_words(value))
    ).strip()


def _partial_object_score(expected, actual):
    if not expected or not actual or expected == actual:
        return 0
    shorter, longer = sorted((expected, actual), key=len)
    if len(shorter) < 4 or shorter not in longer:
        return 0
    ratio = len(shorter) / len(longer)
    return min(55, 40 + round(15 * ratio))


def _fuzzy_prefilter_term(value):
    tokens = [token for token in re.findall(r"\w+", value) if len(token) >= 4]
    return tokens[0] if tokens else ""


def _company_prefilter_terms(value):
    raw = unicodedata.normalize("NFKC", str(value or ""))
    raw = re.sub(r"[^\w\s\-&«»\"'“”„().]+", " ", raw, flags=re.UNICODE)
    raw = re.sub(r"\s+", " ", raw).strip()[:255]
    normalized = normalize_company_name(value)[:255]
    tokens = [token for token in re.findall(r"\w+", normalized) if len(token) >= 3]
    broad_terms = []
    token_terms = []
    seen = set()
    for term in (raw, normalized):
        marker = term.casefold()
        if term and marker not in seen:
            broad_terms.append(term)
            seen.add(marker)
    for term in tokens[:2]:
        marker = term.casefold()
        if marker not in seen:
            token_terms.append(term)
            seen.add(marker)
    return broad_terms, token_terms


def crm_match_query(candidate):
    data = (
        candidate.proposed_changes
        if isinstance(candidate.proposed_changes, dict)
        else {}
    )
    object_name = normalize_crm_object_name(data.get("object_name", ""))
    project = candidate.project
    if (
        not object_name
        and project
        and candidate.team_id is not None
        and project.team_id == candidate.team_id
        and not project.archived
    ):
        object_name = normalize_crm_object_name(project.name)
    return {
        "object_name": object_name,
        "company_name": normalize_company_name(data.get("company_name", "")),
    }


def _has_strong_crm_signal(reasons):
    return not STRONG_MATCH_REASONS.isdisjoint(reasons)


def _classify_crm_options(options):
    strong = [
        option
        for option in options
        if _has_strong_crm_signal(option["match_reasons"])
    ]
    selected = strong[0] if len(strong) == 1 else None
    state = "matched" if selected else ("ambiguous" if options else "not_found")
    return selected, state


class CrmReadBudget:
    def __init__(
        self,
        *,
        deadline_seconds=CRM_MATCH_DEADLINE_SECONDS,
        clock=None,
    ):
        self._clock = clock or time.monotonic
        self._deadline = self._clock() + deadline_seconds
        self.request_count = 0

    def ensure_active(self):
        remaining = self._deadline - self._clock()
        if remaining <= 1:
            raise ProviderUnavailable("crm_match_deadline")
        return remaining

    def next_timeout(self):
        remaining = self.ensure_active()
        self.request_count += 1
        return min(CRM_MATCH_REQUEST_TIMEOUT_SECONDS, remaining - 0.5)


class BitrixService:
    @staticmethod
    def _request(webhook_url, method, params=None, *, timeout=25):
        webhook_base = checked_bitrix_webhook_base(webhook_url)
        url = checked_url(f"{webhook_base}{method}.json")
        response = requests.post(
            url, json=params or {}, timeout=timeout, allow_redirects=False
        )
        response.raise_for_status()
        try:
            data = response.json()
        except (TypeError, ValueError):
            raise ProviderUnavailable("crm_invalid_response") from None
        if not isinstance(data, dict):
            raise ProviderUnavailable("crm_invalid_response")
        if data.get("error"):
            raise ProviderUnavailable("crm_rejected")
        return data

    @staticmethod
    def call(method, params=None):
        """Outbound-capable path. The legacy name remains fail-closed for callers."""
        cfg = BitrixSettings.get_active()
        webhook_url = effective_webhook_url(cfg)
        if not cfg.is_active or not webhook_url:
            raise ProviderUnavailable("crm_disabled")
        return BitrixService._request(webhook_url, method, params)

    @staticmethod
    def matching_config():
        return (
            BitrixSettings.objects.filter(crm_matching_enabled=True)
            .order_by("id")
            .first()
        )

    @staticmethod
    def read_call(method, params=None, *, config, budget):
        """Read-only CRM path. Never extend without reviewing the provider method."""
        if method not in READ_ONLY_METHODS:
            raise ProviderUnavailable("crm_read_method_forbidden")
        timeout = budget.next_timeout()
        data = BitrixService._request(
            config.webhook_base, method, params, timeout=timeout
        )
        budget.ensure_active()
        return data

    @staticmethod
    def read_all(method, params=None, *, config, budget):
        if method not in ("crm.company.list", "crm.deal.list"):
            raise ProviderUnavailable("crm_read_method_forbidden")
        base = dict(params or {})
        items, cursor, seen = [], None, set()
        for _ in range(CRM_MATCH_MAX_PAGES_PER_LIST):
            request_params = dict(base)
            if cursor is not None:
                request_params["start"] = cursor
            data = BitrixService.read_call(
                method, request_params, config=config, budget=budget
            )
            if "result" not in data:
                raise ProviderUnavailable("crm_invalid_response")
            result = data["result"]
            if not isinstance(result, list):
                raise ProviderUnavailable("crm_invalid_response")
            if any(not isinstance(item, dict) for item in result):
                raise ProviderUnavailable("crm_invalid_response")
            items.extend(result)
            next_cursor = data.get("next")
            if next_cursor is None:
                return items
            if type(next_cursor) is int:
                if next_cursor < 0:
                    raise ProviderUnavailable("crm_invalid_pagination")
            elif not (
                isinstance(next_cursor, str)
                and next_cursor.isascii()
                and next_cursor.isdigit()
            ):
                raise ProviderUnavailable("crm_invalid_pagination")
            marker = str(next_cursor)
            if not marker or marker in seen:
                raise ProviderUnavailable("crm_invalid_pagination")
            seen.add(marker)
            cursor = next_cursor
        raise ProviderUnavailable("crm_pagination_limit")

    @staticmethod
    def _safe_external_id(value):
        if value is None:
            return ""
        return str(value).strip()[:64]

    @staticmethod
    def _safe_text(value, limit=255):
        if isinstance(value, list):
            value = " | ".join(str(item) for item in value)
        return str(value or "").strip()[:limit]

    @staticmethod
    def _safe_opportunity(value):
        if value in (None, ""):
            return None
        try:
            amount = Decimal(str(value)).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if (
            not amount.is_finite()
            or amount < 0
            or amount >= Decimal("1000000000000")
        ):
            return None
        return amount

    @staticmethod
    def _matching_fields(cfg):
        field_code = (cfg.deal_object_field_code or "").strip().upper()
        if field_code and not OBJECT_FIELD_PATTERN.fullmatch(field_code):
            raise ProviderUnavailable("crm_object_field_invalid")
        fields = [
            "ID",
            "TITLE",
            "COMPANY_ID",
            "STAGE_ID",
            "OPPORTUNITY",
            "CURRENCY_ID",
            "CATEGORY_ID",
            "CLOSED",
        ]
        if field_code:
            fields.append(field_code)
        return field_code, fields

    @staticmethod
    def _deal_filter(cfg, extra=None):
        value = {"=CATEGORY_ID": cfg.deal_category_id}
        value.update(extra or {})
        return value

    @staticmethod
    def _company_rows(company_name, *, config, budget):
        normalized = normalize_company_name(company_name)
        if not normalized:
            return []
        rows_by_id = {}
        broad_terms, token_terms = _company_prefilter_terms(company_name)

        def collect_term(term):
            rows = BitrixService.read_all(
                "crm.company.list",
                {
                    "filter": {"%TITLE": term},
                    "select": ["ID", "TITLE"],
                    "order": {"ID": "ASC"},
                },
                config=config,
                budget=budget,
            )
            for row in rows:
                company_id = BitrixService._safe_external_id(row.get("ID"))
                if not company_id:
                    raise ProviderUnavailable("crm_invalid_response")
                rows_by_id[company_id] = {
                    **rows_by_id.get(company_id, {}),
                    **row,
                }

        def exact_rows():
            return [
                row
                for row in rows_by_id.values()
                if normalize_company_name(row.get("TITLE", "")) == normalized
            ]

        for term in broad_terms:
            collect_term(term)
        exact = exact_rows()
        if exact:
            return sorted(
                exact,
                key=lambda row: BitrixService._safe_external_id(row.get("ID")),
            )
        for term in token_terms:
            collect_term(term)
            exact = exact_rows()
            if exact:
                return sorted(
                    exact,
                    key=lambda row: BitrixService._safe_external_id(row.get("ID")),
                )
        return []

    @staticmethod
    def _company_details(company_ids, known, *, config, budget):
        result = dict(known)
        for company_id in sorted(company_ids):
            if not company_id or company_id == "0" or company_id in result:
                continue
            data = BitrixService.read_call(
                "crm.company.get",
                {"id": company_id},
                config=config,
                budget=budget,
            )
            row = data.get("result")
            if not isinstance(row, dict):
                raise ProviderUnavailable("crm_invalid_response")
            result[company_id] = BitrixService._safe_text(row.get("TITLE"))
        return result

    @staticmethod
    def _cancel_stale_match(candidate_id, revision):
        return (
            FactCandidate.objects.filter(
                pk=candidate_id,
                fact_type="project",
                crm_match_revision=revision,
                crm_match_state__in=("queued", "error"),
            )
            .exclude(status="pending")
            .update(
                crm_match_state="not_requested",
                crm_match_error_code="crm_match_stale",
                crm_checked_at=timezone.now(),
            )
        )

    @staticmethod
    def _local_project_match(candidate, query):
        project = candidate.project
        if not project:
            return ""
        if project.archived or project.team_id != candidate.team_id:
            updated = FactCandidate.objects.filter(
                pk=candidate.id,
                status="pending",
                crm_match_revision=candidate.crm_match_revision,
                crm_match_state__in=("queued", "error"),
            ).update(
                crm_match_state="error",
                crm_match_error_code="crm_project_scope_conflict",
                crm_match_query=query,
                crm_checked_at=timezone.now(),
            )
            if updated:
                return "error"
            BitrixService._cancel_stale_match(
                candidate.id, candidate.crm_match_revision
            )
            return "stale"
        if not project.bitrix_id:
            return ""
        with transaction.atomic():
            from .dialogue_threads import lock_candidate_source
            lock_candidate_source(candidate)
            locked = FactCandidate.objects.select_for_update(of=("self",)).get(
                pk=candidate.id
            )
            if (
                locked.crm_match_revision != candidate.crm_match_revision
                or locked.crm_match_state not in ("queued", "error")
            ):
                return "stale"
            if locked.status != "pending":
                locked.crm_match_state = "not_requested"
                locked.crm_match_error_code = "crm_match_stale"
                locked.crm_checked_at = timezone.now()
                locked.save(
                    update_fields=[
                        "crm_match_state",
                        "crm_match_error_code",
                        "crm_checked_at",
                    ]
                )
                return "stale"
            locked.crm_matches.filter(selection_state="selected").update(
                selection_state="dismissed"
            )
            locked.crm_matches.filter(
                crm_match_revision=locked.crm_match_revision
            ).delete()
            CandidateCrmMatch.objects.create(
                candidate=locked,
                crm_match_revision=locked.crm_match_revision,
                project=project,
                bitrix_deal_id=str(project.bitrix_id),
                bitrix_company_id=str(
                    project.company.bitrix_company_id
                    if project.company and project.company.bitrix_company_id
                    else ""
                ),
                deal_title=project.name[:255],
                normalized_deal_title=normalize_crm_object_name(project.name),
                company_name=(project.company.name[:255] if project.company else ""),
                normalized_company_name=normalize_company_name(
                    project.company.name if project.company else ""
                ),
                object_label=project.name[:255],
                opportunity=project.contract_amount,
                currency=project.currency[:3],
                score=100,
                match_reasons=["existing_project_bitrix_id"],
                selection_state="selected",
            )
            locked.crm_match_state = "matched"
            locked.crm_match_query = query
            locked.crm_match_error_code = ""
            locked.crm_checked_at = timezone.now()
            locked.base_project_version = project.version
            locked.save(
                update_fields=[
                    "crm_match_state",
                    "crm_match_query",
                    "crm_match_error_code",
                    "crm_checked_at",
                    "base_project_version",
                ]
            )
        return "matched"

    @staticmethod
    def match_candidate(candidate_id, revision):
        candidate = (
            FactCandidate.objects.select_related("project__company")
            .filter(
                pk=candidate_id,
            )
            .first()
        )
        if not candidate or candidate.crm_match_revision != revision:
            return "stale"
        if candidate.status != "pending":
            BitrixService._cancel_stale_match(candidate.id, revision)
            return "stale"
        if candidate.crm_match_state not in ("queued", "error"):
            return "stale"
        query = crm_match_query(candidate)
        config_model = BitrixService.matching_config()
        if not config_model:
            updated = FactCandidate.objects.filter(
                pk=candidate.id,
                status="pending",
                crm_match_revision=revision,
                crm_match_state__in=("queued", "error"),
            ).update(
                crm_match_state="disabled",
                crm_match_query=query,
                crm_match_error_code="",
                crm_checked_at=timezone.now(),
            )
            if updated:
                return "disabled"
            BitrixService._cancel_stale_match(candidate.id, revision)
            return "stale"
        config = CrmReadConfig.from_model(config_model)

        local_state = BitrixService._local_project_match(candidate, query)
        if local_state:
            return local_state

        if not query["object_name"] and not query["company_name"]:
            updated = FactCandidate.objects.filter(
                pk=candidate.id,
                status="pending",
                crm_match_revision=revision,
                crm_match_state__in=("queued", "error"),
            ).update(
                crm_match_state="error",
                crm_match_query=query,
                crm_match_error_code="crm_match_query_empty",
                crm_checked_at=timezone.now(),
            )
            if updated:
                return "error"
            BitrixService._cancel_stale_match(candidate.id, revision)
            return "stale"

        data = (
            candidate.proposed_changes
            if isinstance(candidate.proposed_changes, dict)
            else {}
        )
        company_name = BitrixService._safe_text(data.get("company_name"))
        object_normalized = query["object_name"]
        field_code, select_fields = BitrixService._matching_fields(config)
        budget = CrmReadBudget()

        company_rows = BitrixService._company_rows(
            company_name, config=config, budget=budget
        )
        company_titles = {
            BitrixService._safe_external_id(row.get("ID")): BitrixService._safe_text(
                row.get("TITLE")
            )
            for row in company_rows
        }
        company_titles.pop("", None)
        exact_company_ids = set(company_titles)
        deals = {}

        def collect(rows):
            for row in rows:
                deal_id = BitrixService._safe_external_id(row.get("ID"))
                if not deal_id:
                    raise ProviderUnavailable("crm_invalid_response")
                deals[deal_id] = {**deals.get(deal_id, {}), **row}

        def collect_by_object(term):
            collect(
                BitrixService.read_all(
                    "crm.deal.list",
                    {
                        "filter": BitrixService._deal_filter(
                            config, {"%TITLE": term}
                        ),
                        "select": select_fields,
                        "order": {"ID": "ASC"},
                    },
                    config=config,
                    budget=budget,
                )
            )
            if field_code:
                collect(
                    BitrixService.read_all(
                        "crm.deal.list",
                        {
                            "filter": BitrixService._deal_filter(
                                config, {f"%{field_code}": term}
                            ),
                            "select": select_fields,
                            "order": {"ID": "ASC"},
                        },
                        config=config,
                        budget=budget,
                    )
                )

        for company_id in sorted(exact_company_ids):
            collect(
                BitrixService.read_all(
                    "crm.deal.list",
                    {
                        "filter": BitrixService._deal_filter(
                            config, {"=COMPANY_ID": company_id}
                        ),
                        "select": select_fields,
                        "order": {"ID": "ASC"},
                    },
                    config=config,
                    budget=budget,
                )
            )
        if object_normalized:
            collect_by_object(object_normalized)
            fuzzy_term = _fuzzy_prefilter_term(object_normalized)
            if (
                not exact_company_ids
                and fuzzy_term
                and fuzzy_term != object_normalized
            ):
                collect_by_object(fuzzy_term)

        relevant = []
        projects_by_deal = {
            str(project.bitrix_id): project
            for project in Project.objects.filter(
                bitrix_id__in=list(deals),
            )
        }
        deal_company_ids = {
            BitrixService._safe_external_id(row.get("COMPANY_ID"))
            for row in deals.values()
        }
        company_titles = BitrixService._company_details(
            deal_company_ids - {"", "0"},
            company_titles,
            config=config,
            budget=budget,
        )
        for deal_id, row in deals.items():
            deal_title = BitrixService._safe_text(row.get("TITLE"))
            deal_normalized = normalize_crm_object_name(deal_title)
            deal_company_id = BitrixService._safe_external_id(row.get("COMPANY_ID"))
            object_label = (
                BitrixService._safe_text(row.get(field_code)) if field_code else ""
            )
            object_field_normalized = normalize_crm_object_name(object_label)
            reasons = []
            if deal_company_id in exact_company_ids:
                reasons.append("company_exact")
            title_partial_score = _partial_object_score(
                object_normalized, deal_normalized
            )
            field_partial_score = _partial_object_score(
                object_normalized, object_field_normalized
            )
            if object_normalized and deal_normalized == object_normalized:
                reasons.append("title_exact")
            elif title_partial_score:
                reasons.append("title_partial")
            if object_normalized and object_field_normalized == object_normalized:
                reasons.append("object_field_exact")
            elif field_partial_score:
                reasons.append("object_field_partial")
            if not reasons:
                continue
            project = projects_by_deal.get(deal_id)
            if project and (
                project.archived or project.team_id != candidate.team_id
            ):
                updated = BitrixService.record_match_error(
                    candidate.id, revision, "crm_project_scope_conflict"
                )
                return "error" if updated else "stale"
            if project:
                reasons.append("existing_project_bitrix_id")
            strong = _has_strong_crm_signal(reasons)
            score = 0
            if strong:
                score = 95 + (5 if "company_exact" in reasons else 0)
            if not score and project:
                score = 70
            if not score and (title_partial_score or field_partial_score):
                score = max(title_partial_score, field_partial_score)
                if "company_exact" in reasons:
                    score = min(65, score + 10)
            if not score:
                score = 60
            company_title = company_titles.get(deal_company_id, "")
            relevant.append(
                {
                    "project": project,
                    "bitrix_deal_id": deal_id,
                    "bitrix_company_id": (
                        deal_company_id if deal_company_id != "0" else ""
                    ),
                    "deal_title": deal_title,
                    "normalized_deal_title": deal_normalized,
                    "company_name": company_title,
                    "normalized_company_name": normalize_company_name(company_title),
                    "object_label": object_label,
                    "stage_id": BitrixService._safe_text(row.get("STAGE_ID"), 64),
                    "opportunity": BitrixService._safe_opportunity(
                        row.get("OPPORTUNITY")
                    ),
                    "currency": BitrixService._safe_text(row.get("CURRENCY_ID"), 3),
                    "score": score,
                    "match_reasons": reasons,
                }
            )
        relevant.sort(key=lambda item: (-item["score"], item["bitrix_deal_id"]))
        selected, state = _classify_crm_options(relevant)
        if selected and candidate.project_id and (
            not selected["project"]
            or candidate.project_id != selected["project"].id
        ):
            selected, state = None, "ambiguous"

        budget.ensure_active()
        with transaction.atomic():
            from .dialogue_threads import lock_candidate_source
            lock_candidate_source(candidate)
            locked = FactCandidate.objects.select_for_update(of=("self",)).get(
                pk=candidate.id
            )
            if (
                locked.crm_match_revision != revision
                or locked.crm_match_state not in ("queued", "error")
            ):
                return "stale"
            if locked.status != "pending":
                locked.crm_match_state = "not_requested"
                locked.crm_match_error_code = "crm_match_stale"
                locked.crm_checked_at = timezone.now()
                locked.save(
                    update_fields=[
                        "crm_match_state",
                        "crm_match_error_code",
                        "crm_checked_at",
                    ]
                )
                return "stale"
            budget.ensure_active()
            locked.crm_matches.filter(selection_state="selected").update(
                selection_state="dismissed"
            )
            locked.crm_matches.filter(crm_match_revision=revision).delete()
            for item in relevant:
                CandidateCrmMatch.objects.create(
                    candidate=locked,
                    crm_match_revision=revision,
                    project=item["project"],
                    bitrix_deal_id=item["bitrix_deal_id"],
                    bitrix_company_id=item["bitrix_company_id"],
                    deal_title=item["deal_title"],
                    normalized_deal_title=item["normalized_deal_title"],
                    company_name=item["company_name"],
                    normalized_company_name=item["normalized_company_name"],
                    object_label=item["object_label"],
                    stage_id=item["stage_id"],
                    opportunity=item["opportunity"],
                    currency=item["currency"],
                    score=item["score"],
                    match_reasons=item["match_reasons"],
                    selection_state=(
                        "selected"
                        if selected
                        and item["bitrix_deal_id"] == selected["bitrix_deal_id"]
                        else "suggested"
                    ),
                )
            if selected and selected["project"]:
                locked.project = selected["project"]
                locked.base_project_version = selected["project"].version
            locked.crm_match_state = state
            locked.crm_match_query = query
            locked.crm_match_error_code = ""
            locked.crm_checked_at = timezone.now()
            locked.save(
                update_fields=[
                    "project",
                    "base_project_version",
                    "crm_match_state",
                    "crm_match_query",
                    "crm_match_error_code",
                    "crm_checked_at",
                ]
            )
            if locked.thread_revision_id and locked.project_id:
                from .models import DialogueThread
                DialogueThread.objects.filter(pk=locked.thread_revision.thread_id, team_id=locked.team_id).update(project_id=locked.project_id)
        return state

    @staticmethod
    def record_match_error(candidate_id, revision, code):
        updated = FactCandidate.objects.filter(
            pk=candidate_id,
            status="pending",
            fact_type="project",
            crm_match_revision=revision,
            crm_match_state__in=("queued", "error"),
        ).update(
            crm_match_state="error",
            crm_match_error_code=str(code or "crm_lookup_failed")[:64],
            crm_checked_at=timezone.now(),
        )
        if not updated:
            BitrixService._cancel_stale_match(candidate_id, revision)
        return bool(updated)

    @staticmethod
    def sync_project(project_id, version):
        cfg = BitrixSettings.get_active()
        if not cfg.is_active:
            raise ProviderUnavailable("crm_disabled")
        from django.db.models import Q
        project = Project.objects.filter(Q(is_verified=True) | Q(identity_confirmed=True)).get(pk=project_id)
        if not project.team_id or project.version != version:
            return
        # Once autonomous accounting owns delivery, old scheduled jobs may
        # only enqueue its field-limited command, never restore a full snapshot.
        from .models import AISettings, FactEvent
        if AISettings.get_active().autonomous_enabled:
            from .autonomous_crm import enqueue_project
            event = FactEvent.objects.filter(project=project).exclude(event_type="daily_report").order_by("-id").first()
            if event:
                enqueue_project(project, event)
            return
        revision = ProjectRevision.objects.get(project=project, version=version)
        snapshot = revision.snapshot
        stage = settings.BITRIX_STAGE_MAP.get(snapshot["status"])
        fields = {
            "STAGE_ID": stage,
            "TITLE": snapshot["name"],
            "OPPORTUNITY": snapshot["contract_amount"],
            "CURRENCY_ID": snapshot["currency"],
            "ORIGINATOR_ID": "MAZORY",
            "ORIGIN_ID": str(project.id),
            "COMMENTS": escape(snapshot.get("current_action", ""))
            + "\n"
            + escape(snapshot.get("next_action", "")),
        }
        if not stage:
            fields.pop("STAGE_ID", None)
        if not snapshot.get("contract_known") and Decimal(str(snapshot.get("contract_amount") or 0)) <= 0:
            fields.pop("OPPORTUNITY", None)
        if not project.is_verified:
            fields = {"TITLE": snapshot["name"], "ORIGINATOR_ID": "MAZORY", "ORIGIN_ID": str(project.id)}
        # Stable external key permits reconciliation after an unknown create outcome.
        if not project.bitrix_id:
            matches = BitrixService.call(
                "crm.deal.list",
                {
                    "filter": {
                        "=ORIGINATOR_ID": "MAZORY",
                        "=ORIGIN_ID": str(project.id),
                    },
                    "select": ["ID"],
                },
            ).get("result", [])
            if len(matches) > 1:
                raise ProviderUnavailable("crm_origin_conflict")
            if matches:
                project.bitrix_id = str(matches[0]["ID"])
                Project.objects.filter(pk=project.id).update(
                    bitrix_id=project.bitrix_id
                )
        if not project.bitrix_id:
            if not cfg.auto_create_deals:
                raise ProviderUnavailable("crm_creation_disabled")
            result = BitrixService.call("crm.deal.add", {"fields": fields})
            external = str(result["result"])
            action = "create"
        else:
            external = project.bitrix_id
            result = BitrixService.call(
                "crm.deal.update", {"id": external, "fields": fields}
            )
            action = "update"
        with transaction.atomic():
            Project.objects.filter(pk=project.id, version=version).update(
                bitrix_id=external,
                needs_bitrix_sync=False,
                last_bitrix_synced_at=timezone.now(),
            )
            BitrixDealChangeLog.objects.create(
                project=project,
                bitrix_deal_id=external,
                action=action,
                payload=fields,
                response_data={"ok": True},
                status="success",
                triggered_by=f"approved_revision:{version}",
            )

    @staticmethod
    def propose_import(payload):
        cfg = BitrixSettings.get_active()
        if not cfg.is_active or not cfg.auto_import_deals:
            return
        data = BitrixService.call("crm.deal.get", {"id": payload["deal_id"]})["result"]
        project = Project.objects.filter(bitrix_id=payload["deal_id"]).first()
        team = (
            project.team
            if project
            else Team.objects.filter(pk=settings.BITRIX_TEAM_ID, is_active=True).first()
        )
        if not team:
            raise ProviderUnavailable("crm_team_mapping_required")
        clean = {
            "name": str(data.get("TITLE", ""))[:255],
            "contract_amount": str(data.get("OPPORTUNITY", "0")),
            "currency": str(data.get("CURRENCY_ID", "KZT")),
            "external_stage": str(data.get("STAGE_ID", "")),
        }
        digest = hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest()
        content = json.dumps(clean, ensure_ascii=False)
        with transaction.atomic():
            raw, created = RawMessage.objects.get_or_create(
                source="bitrix",
                session_name="crm",
                message_id=payload["deal_id"],
                source_revision=digest,
                defaults={
                    "team": team,
                    "project": project,
                    "timestamp": timezone.now(),
                    "sent_at_known": False,
                    "content": content,
                    "processed": True,
                    "processing_state": "needs_review",
                },
            )
            if not created:
                return
            trace = MessageProcessingTrace.objects.create(
                raw_message=raw,
                whatsapp_message_id=raw.message_id,
                whatsapp_content=content,
                pipeline_action="proposed_facts",
                status="warning",
                prompt_version="crm-import-v1",
            )
            proposed = {
                "fact_type": "project",
                "object_name": clean["name"],
                "contract_amount": clean["contract_amount"],
                "currency": clean["currency"],
                "evidence": content,
                "confidence": 1,
                "uncertainties": ["Изменения CRM требуют подтверждения."],
            }
            stages = [
                local
                for local, external in settings.BITRIX_STAGE_MAP.items()
                if external == clean["external_stage"]
                and local in dict(Project.STATUS_CHOICES)
            ]
            if len(stages) == 1:
                proposed["stage"] = stages[0]
            else:
                proposed["uncertainties"].append(
                    "Стадия CRM не имеет однозначного сопоставления."
                )
            candidate = FactCandidate.objects.create(
                trace=trace,
                team=team,
                project=project,
                manager=project.manager if project else None,
                fact_type="project",
                proposed_changes=proposed,
                base_project_version=project.version if project else 0,
                source_key=f"crm:{raw.id}",
                confidence=1,
            )
            FactEvidence.objects.create(
                candidate=candidate, raw_message=raw, quote=content, field_name="source"
            )
