"""CRM adapter. Calls happen only in worker tasks; imports create review proposals."""

import hashlib
import json
from html import escape
import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from .models import (
    Project,
    ProjectRevision,
    BitrixSettings,
    BitrixDealChangeLog,
    RawMessage,
    MessageProcessingTrace,
    FactCandidate,
    FactEvidence,
    Team,
)
from .providers import checked_url, ProviderUnavailable
from .bitrix_config import effective_webhook_url


class BitrixService:
    @staticmethod
    def call(method, params=None):
        cfg = BitrixSettings.get_active()
        webhook_url = effective_webhook_url(cfg)
        if not cfg.is_active or not webhook_url:
            raise ProviderUnavailable("crm_disabled")
        url = checked_url(f"{webhook_url.rstrip('/')}/{method}.json")
        response = requests.post(
            url, json=params or {}, timeout=25, allow_redirects=False
        )
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            raise ProviderUnavailable("crm_rejected")
        return data

    @staticmethod
    def sync_project(project_id, version):
        cfg = BitrixSettings.get_active()
        if not cfg.is_active:
            raise ProviderUnavailable("crm_disabled")
        project = Project.objects.get(pk=project_id, is_verified=True)
        if not project.team_id or project.version != version:
            return
        revision = ProjectRevision.objects.get(project=project, version=version)
        snapshot = revision.snapshot
        stage = settings.BITRIX_STAGE_MAP.get(snapshot["status"])
        if not stage:
            raise ProviderUnavailable("crm_stage_mapping_required")
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
