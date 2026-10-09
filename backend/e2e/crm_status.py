"""Settings fixtures and database evidence for the isolated browser scenario."""

import json
from django.contrib.auth.models import Permission, User
from django.conf import settings
from django.utils import timezone
from api.models import (
    AISettings, BitrixSettings, Commitment, Company, CrmDelivery,
    FinancialRecord, OutboxEvent, Project,
)

AI_FIELDS = ("is_active", "autonomous_enabled", "autonomous_crm_enabled")
BITRIX_FIELDS = ("is_active", "webhook_url")
CASES = {
    "write_paused": (True, True, True, False, False),
    "write_enabled": (True, True, True, False, True),
    "processing_enabled": (True, True, True, True, True),
    "integration_disabled": (False, True, True, False, True),
    "webhook_missing": (True, False, True, False, True),
    "ai_missing": (True, True, False, False, True),
}


def original_settings():
    viewer, _ = User.objects.get_or_create(username="crm-status-viewer", defaults={"is_staff": True})
    viewer.set_password("local-e2e-only")
    viewer.save(update_fields=["password"])
    viewer.user_permissions.set(Permission.objects.filter(
        content_type__app_label="api", codename__in=("view_bitrixsettings", "change_whatsappconfig"),
    ))
    return {
        "ai": AISettings.objects.values("id", *AI_FIELDS).first(),
        "bitrix": BitrixSettings.objects.values("id", *BITRIX_FIELDS).first(),
    }


def apply_case(name, original):
    ai, bitrix = original["ai"], original["bitrix"]
    if name == "restore":
        AISettings.objects.filter(pk=ai["id"]).update(**{key: ai[key] for key in AI_FIELDS})
        BitrixSettings.objects.filter(pk=bitrix["id"]).update(**{key: bitrix[key] for key in BITRIX_FIELDS})
        return
    integration, webhook, ai_active, processing, writing = CASES[name]
    AISettings.objects.filter(pk=ai["id"]).update(
        is_active=ai_active, autonomous_enabled=processing, autonomous_crm_enabled=writing,
    )
    BitrixSettings.objects.filter(pk=bitrix["id"]).update(
        is_active=integration, webhook_url=bitrix["webhook_url"] if webhook else "   ",
    )


def write_proof(name, original):
    ai = AISettings.objects.get(pk=original["ai"]["id"])
    bitrix = BitrixSettings.objects.get(pk=original["bitrix"]["id"])
    proof = {
        "observed_at": timezone.now().isoformat(),
        "case": name, "ai_id": ai.pk, "bitrix_id": bitrix.pk,
        "settings": {
            "ai_active": ai.is_active, "processing": ai.autonomous_enabled,
            "writing": ai.autonomous_crm_enabled, "integration": bitrix.is_active,
            "webhook_configured": bool(bitrix.webhook_url.strip()),
            "ai_updated_at": ai.updated_at.isoformat(),
            "bitrix_updated_at": bitrix.updated_at.isoformat(),
        },
        "records": {
            "projects": Project.objects.count(), "companies": Company.objects.count(),
            "commitments": Commitment.objects.count(), "finances": FinancialRecord.objects.count(),
            "crm_deliveries": CrmDelivery.objects.count(),
            "crm_outbox": OutboxEvent.objects.filter(event_type="autonomous_crm").count(),
        },
    }
    temporary = settings.E2E_DIR / "crm-status-proof.tmp"
    temporary.write_text(json.dumps(proof))
    temporary.replace(settings.E2E_DIR / "crm-status-proof.json")
