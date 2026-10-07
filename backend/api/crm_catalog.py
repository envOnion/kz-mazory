"""Paginated CRM identities. Imported catalog entries do not approve finances."""

from decimal import Decimal, InvalidOperation
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .bitrix_service import BitrixService, CrmReadConfig, CrmReadBudget
from .models import (
    BitrixSettings,
    Company,
    CrmCatalogSync,
    OutboxEvent,
    Project,
    Team,
    UserProfile,
    CrmProjectSnapshot,
)
from .providers import ProviderUnavailable


def available_projects(queryset):
    return queryset.filter(Q(is_verified=True) | Q(identity_confirmed=True)).exclude(
        archived=True
    )


def upsert_company(company_id, title="", phone=""):
    cid = str(company_id or "").strip()[:64]
    if not cid or cid == "0":
        return None

    company = Company.objects.filter(bitrix_company_id=cid).first()
    provided_title = (title or "").strip()[:255]
    clean_title = provided_title or f"Компания #{cid}"
    if company:
        if provided_title and company.name != clean_title:
            company.name = clean_title
        if phone and not company.phone:
            company.phone = str(phone).strip()[:64]
        company.save()
        return company

    return Company.objects.create(
        bitrix_company_id=cid,
        name=clean_title,
        phone=str(phone).strip()[:64] if phone else "",
    )


def enqueue_catalog(team_id, *, refresh=False, sync_companies=False):
    with transaction.atomic():
        team = Team.objects.select_for_update().get(pk=team_id, is_active=True)
        sync, _ = CrmCatalogSync.objects.get_or_create(team=team)
        if sync.state in ("queued", "running") or (
            sync.state == "succeeded" and not refresh
        ):
            return sync
        sync.generation += 1
        sync.state, sync.cursor, sync.imported_count, sync.error_code = (
            "queued",
            0,
            0,
            "",
        )
        sync.save()
        queue_page(sync, phase="companies" if sync_companies else "deals")
        return sync


def queue_page(sync, phase="deals"):
    phase_suffix = f":{phase}" if phase != "deals" else ""
    return OutboxEvent.objects.get_or_create(
        deduplication_key=f"crm-catalog:{sync.team_id}:{sync.generation}:{sync.cursor}{phase_suffix}",
        defaults={
            "event_type": "crm_catalog",
            "payload": {
                "team_id": sync.team_id,
                "generation": sync.generation,
                "cursor": sync.cursor,
                "phase": phase,
            },
        },
    )[0]


def _sync_companies_page(sync, config, payload):
    response = BitrixService.read_call(
        "crm.company.list",
        {
            "start": sync.cursor,
            "order": {"ID": "ASC"},
            "select": ["ID", "TITLE", "PHONE", "ASSIGNED_BY_ID"],
        },
        config=config,
        budget=CrmReadBudget(),
    )
    rows, next_cursor = response.get("result"), response.get("next")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ProviderUnavailable("crm_invalid_response")
    if next_cursor is not None and (
        type(next_cursor) is not int or next_cursor <= sync.cursor
    ):
        raise ProviderUnavailable("crm_invalid_pagination")

    for row in rows:
        cid = str(row.get("ID", "")).strip()
        if not cid or not cid.isascii() or not cid.isdigit() or len(cid) > 64:
            raise ProviderUnavailable("crm_invalid_response")
        phone = ""
        raw_phone = row.get("PHONE")
        if isinstance(raw_phone, list) and raw_phone:
            phone = str(raw_phone[0].get("VALUE", "")).strip()[:64]
        upsert_company(cid, row.get("TITLE"), phone)

    with transaction.atomic():
        locked = CrmCatalogSync.objects.select_for_update().get(pk=sync.pk)
        if (
            locked.generation != sync.generation
            or locked.cursor != sync.cursor
            or locked.state == "succeeded"
        ):
            return
        locked.imported_count += len(rows)
        locked.error_code = ""
        if next_cursor is None:
            locked.cursor = 0
            locked.save()
            queue_page(locked, phase="deals")
        else:
            locked.cursor = next_cursor
            locked.save()
            queue_page(locked, phase="companies")


def _sync_deals_page(sync, config, cfg, payload):
    response = BitrixService.read_call(
        "crm.deal.list",
        {
            "start": sync.cursor,
            "order": {"ID": "ASC"},
            "filter": {"CATEGORY_ID": cfg.deal_category_id},
            "select": [
                "ID",
                "TITLE",
                "COMPANY_ID",
                "ASSIGNED_BY_ID",
                "CURRENCY_ID",
                "OPPORTUNITY",
                "STAGE_ID",
            ],
        },
        config=config,
        budget=CrmReadBudget(),
    )
    rows, next_cursor = response.get("result"), response.get("next")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ProviderUnavailable("crm_invalid_response")
    if next_cursor is not None and (
        type(next_cursor) is not int or next_cursor <= sync.cursor
    ):
        raise ProviderUnavailable("crm_invalid_pagination")
    for row in rows:
        if (
            not isinstance(row.get("ID"), str)
            or not row["ID"].isascii()
            or not row["ID"].isdigit()
            or len(row["ID"]) > 64
            or not str(row.get("TITLE", "")).strip()
        ):
            raise ProviderUnavailable("crm_invalid_response")
    stages = {}
    if any(row.get("STAGE_ID") for row in rows):
        entity = (
            "DEAL_STAGE"
            if not cfg.deal_category_id
            else f"DEAL_STAGE_{cfg.deal_category_id}"
        )
        result = BitrixService.read_call(
            "crm.status.list",
            {"filter": {"ENTITY_ID": entity}, "order": {"SORT": "ASC"}},
            config=config,
            budget=CrmReadBudget(),
        ).get("result")
        if not isinstance(result, list) or any(
            not isinstance(item, dict) for item in result
        ):
            raise ProviderUnavailable("crm_invalid_response")
        stages = {
            str(item["STATUS_ID"]): str(item.get("NAME", ""))[:255]
            for item in result
            if item.get("STATUS_ID")
        }
    manager_ids = sorted(
        {str(row["ASSIGNED_BY_ID"]) for row in rows if row.get("ASSIGNED_BY_ID")}
    )
    manager_names = {}
    if manager_ids:
        # Some read-only webhooks lack user_brief. The CRM identity remains useful.
        try:
            users = BitrixService.read_call(
                "user.get",
                {"filter": {"ID": manager_ids}},
                config=config,
                budget=CrmReadBudget(),
            ).get("result")
            if isinstance(users, list):
                manager_names = {
                    str(item["ID"]): " ".join(
                        str(item.get(k) or "").strip() for k in ["NAME", "LAST_NAME"]
                    ).strip()[:255]
                    for item in users
                    if isinstance(item, dict) and item.get("ID")
                }
        except ProviderUnavailable:
            pass
    with transaction.atomic():
        locked = CrmCatalogSync.objects.select_for_update().get(pk=sync.pk)
        if (
            locked.generation != sync.generation
            or locked.cursor != sync.cursor
            or locked.state == "succeeded"
        ):
            return
        for row in rows:
            project = (
                Project.objects.select_for_update().filter(bitrix_id=row["ID"]).first()
            )
            if project and project.team_id != locked.team_id:
                raise ProviderUnavailable("crm_project_scope_conflict")
            profiles = (
                UserProfile.objects.filter(
                    bitrix_user_id=row.get("ASSIGNED_BY_ID", ""),
                    user__memberships__team_id=locked.team_id,
                    user__memberships__status="active",
                    user__is_active=True,
                ).distinct()
                if row.get("ASSIGNED_BY_ID")
                else UserProfile.objects.none()
            )
            manager = profiles.first() if profiles.count() == 1 else None

            company = None
            company_id = str(row.get("COMPANY_ID") or "").strip()
            if company_id and company_id != "0":
                company = upsert_company(company_id, row.get("COMPANY_TITLE", ""))

            if project is None:
                project = Project(
                    team_id=locked.team_id,
                    bitrix_id=row["ID"],
                    source="bitrix_crm",
                    is_verified=False,
                )
            if not project.is_verified:
                project.name = str(row["TITLE"]).strip()[:255]
                project.normalized_name = f"bitrix:{row['ID']}"
                project.manager = manager
                if company:
                    project.company = company
                currency = row.get("CURRENCY_ID", "KZT")
                project.currency = (
                    currency if currency in ("KZT", "USD", "EUR", "RUB") else "KZT"
                )
            elif company and not project.company:
                project.company = company

            project.last_bitrix_synced_at = timezone.now()
            project.identity_confirmed = True
            project.save()
            opportunity = None
            if row.get("OPPORTUNITY") not in (None, ""):
                try:
                    opportunity = Decimal(str(row["OPPORTUNITY"]))
                    if (
                        not opportunity.is_finite()
                        or abs(opportunity) >= Decimal("1000000000000")
                        or opportunity != opportunity.quantize(Decimal(".01"))
                    ):
                        raise InvalidOperation
                except (ValueError, InvalidOperation):
                    raise ProviderUnavailable("crm_invalid_amount") from None
            stage_id = str(row.get("STAGE_ID") or "")[:128]
            manager_id = str(row.get("ASSIGNED_BY_ID") or "")[:64]
            CrmProjectSnapshot.objects.update_or_create(
                project=project,
                defaults={
                    "external_stage_id": stage_id,
                    "external_stage_name": stages.get(stage_id, ""),
                    "external_manager_id": manager_id,
                    "external_manager_name": manager_names.get(manager_id)
                    or ("Сотрудник CRM #" + manager_id if manager_id else ""),
                    "opportunity": opportunity,
                    "currency": str(row.get("CURRENCY_ID") or project.currency)[:3],
                    "synced_at": timezone.now(),
                },
            )
        locked.imported_count += len(rows)
        locked.error_code = ""
        if next_cursor is None:
            locked.state, locked.last_success_at = "succeeded", timezone.now()
        else:
            locked.state, locked.cursor = "queued", next_cursor
        locked.save()
        if next_cursor is not None:
            queue_page(locked, phase="deals")


def sync_page(payload):
    sync = CrmCatalogSync.objects.get(team_id=payload["team_id"])
    if (
        sync.generation != payload["generation"]
        or sync.cursor != payload["cursor"]
        or sync.state == "succeeded"
    ):
        return
    cfg = BitrixSettings.objects.first()
    try:
        if not cfg or not (
            cfg.crm_matching_enabled or (cfg.is_active and cfg.auto_import_deals)
        ):
            raise ProviderUnavailable("crm_import_disabled")
        if sync.team_id != settings.BITRIX_TEAM_ID or not sync.team.is_active:
            raise ProviderUnavailable("crm_team_mapping_required")
        config = CrmReadConfig.from_model(cfg)
        CrmCatalogSync.objects.filter(pk=sync.pk, generation=sync.generation).update(
            state="running", error_code=""
        )

        phase = payload.get("phase")
        if phase == "companies":
            _sync_companies_page(sync, config, payload)
        else:
            _sync_deals_page(sync, config, cfg, payload)
    except Exception as exc:
        code = (
            str(exc)[:64]
            if isinstance(exc, ProviderUnavailable)
            else "crm_catalog_request_failed"
        )
        CrmCatalogSync.objects.filter(pk=sync.pk, generation=sync.generation).update(
            state="error", error_code=code
        )
        raise
