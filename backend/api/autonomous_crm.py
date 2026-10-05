"""Versioned, field-limited CRM commands run exclusively by the CRM worker."""

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import AISettings, BitrixSettings, Commitment, Company, CrmDelivery, ExternalObjectLink, OutboxEvent, Project
from .providers import ProviderUnavailable


def enqueue_project(project, fact_event, cfg=None):
    cfg = cfg or AISettings.get_active()
    if not cfg.autonomous_crm_enabled:
        return
    integration = BitrixSettings.get_active()
    if not integration.is_active:
        return
    integration_key = f"bitrix:{integration.pk}"
    origin = f"mazory:{project.team_id}:project:{project.id}"
    link, _ = ExternalObjectLink.objects.get_or_create(origin_key=origin, defaults={
        "team_id": project.team_id, "integration_key": integration_key,
        "object_type": "deal", "local_type": "Project", "local_id": project.id,
        "external_id": project.bitrix_id or None,
    })
    patch = {"TITLE": project.name} if project.source == "chat" else {}
    known = set(project.whatsapp_fields)
    if project.contract_known and "contract_amount" in known:
        patch.update(OPPORTUNITY=str(project.contract_amount), CURRENCY_ID=project.currency)
    if "stage" in known and settings.BITRIX_STAGE_MAP.get(project.status):
        patch["STAGE_ID"] = settings.BITRIX_STAGE_MAP[project.status]
    event, created = OutboxEvent.objects.get_or_create(deduplication_key=f"autonomous-crm:{project.id}:{project.version}", defaults={
        "event_type": "autonomous_crm", "payload": {"project_id": project.id, "version": project.version},
    })
    if created:
        CrmDelivery.objects.create(outbox_event=event, external_object_link=link, fact_event=fact_event, target_version=project.version, patch=patch)


def deliver(payload):
    from .bitrix_service import BitrixService
    if payload.get("kind"):
        return deliver_effect(payload)
    cfg = AISettings.get_active()
    if not cfg.autonomous_crm_enabled:
        raise ProviderUnavailable("autonomous_crm_paused")
    project_id, version = payload["project_id"], payload["version"]
    # Project row serializes delivery versions. A newer local apply waits for
    # this bounded external operation, preventing out-of-order overwrites.
    with transaction.atomic():
        project = Project.objects.select_for_update().get(pk=project_id)
        delivery = CrmDelivery.objects.select_for_update(of=("self",)).select_related("external_object_link").get(outbox_event__deduplication_key=f"autonomous-crm:{project.id}:{version}")
        if delivery.state in ("delivered", "superseded"):
            return
        if project.version != version:
            delivery.state = "superseded"
            delivery.save(update_fields=["state", "updated_at"])
            return
        stage_missing = "stage" in project.whatsapp_fields and not settings.BITRIX_STAGE_MAP.get(project.status)
        if "stage" in project.whatsapp_fields and settings.BITRIX_STAGE_MAP.get(project.status):
            delivery.patch["STAGE_ID"] = settings.BITRIX_STAGE_MAP[project.status]
            delivery.save(update_fields=["patch"])
        integration = BitrixSettings.get_active()
        if not integration.is_active:
            raise ProviderUnavailable("crm_disabled")
        link = delivery.external_object_link
        external = link.external_id or project.bitrix_id
        if not external:
            # Reconcile every unknown add outcome using the stable origin.
            rows = BitrixService.call("crm.deal.list", {"filter": {"=ORIGINATOR_ID": "MAZORY", "=ORIGIN_ID": link.origin_key}, "select": ["ID"]}).get("result", [])
            if len(rows) > 1:
                raise ProviderUnavailable("crm_origin_conflict")
            if rows:
                external = str(rows[0]["ID"])
        if external:
            current = BitrixService.call("crm.deal.get", {"id": external}).get("result")
            if not isinstance(current, dict):
                raise ProviderUnavailable("crm_invalid_response")
            # Human-owned CRM values are not read back as WhatsApp facts.
            patch = {key: value for key, value in delivery.patch.items() if str(current.get(key, "")) != str(value)}
            if patch:
                BitrixService.call("crm.deal.update", {"id": external, "fields": patch})
        else:
            if not integration.auto_create_deals:
                raise ProviderUnavailable("crm_creation_disabled")
            candidate = delivery.fact_event.decision.candidate if delivery.fact_event and delivery.fact_event.decision_id else None
            if not candidate or candidate.crm_match_state != "not_found":
                raise ProviderUnavailable("crm_identity_unresolved")
            fields = {**delivery.patch, "ORIGINATOR_ID": "MAZORY", "ORIGIN_ID": link.origin_key, "CATEGORY_ID": integration.deal_category_id}
            external = str(BitrixService.call("crm.deal.add", {"fields": fields})["result"])
        if not external or not external.isdigit():
            raise ProviderUnavailable("crm_invalid_response")
        link.external_id = external
        link.save(update_fields=["external_id"])
        Project.objects.filter(pk=project.id).update(bitrix_id=external, last_bitrix_synced_at=timezone.now(), needs_bitrix_sync=False)
        delivery.state, delivery.error_code = ("partial", "crm_stage_mapping_missing") if stage_missing else ("delivered", "")
        delivery.save(update_fields=["state", "error_code", "updated_at"])
    if stage_missing:
        raise ProviderUnavailable("crm_stage_mapping_missing")


def enqueue_effects(candidate, fact_event, cfg):
    """Separate deliveries: an unresolved assignee never blocks a deal/report."""
    if not cfg.autonomous_crm_enabled:
        return
    integration = BitrixSettings.get_active()
    if not integration.is_active:
        return
    def queue(kind, local, version, patch):
        origin = f"mazory:{candidate.team_id}:{kind}:{local.id}"
        existing_id = local.bitrix_task_id if kind == "task" else local.bitrix_company_id if kind == "company" else None
        link, _ = ExternalObjectLink.objects.get_or_create(origin_key=origin, defaults={"team_id": candidate.team_id, "integration_key": f"bitrix:{integration.pk}", "object_type": kind, "local_type": type(local).__name__, "local_id": local.id, "external_id": existing_id})
        event, created = OutboxEvent.objects.get_or_create(deduplication_key=f"autonomous-{kind}:{local.id}:{version}", defaults={"event_type": "autonomous_crm", "payload": {"kind": kind, "local_id": local.id, "version": version}})
        if created:
            CrmDelivery.objects.create(outbox_event=event, external_object_link=link, fact_event=fact_event, target_version=version, patch=patch)
    if integration.sync_timeline_comments and candidate.project_id:
        queue("timeline", fact_event, 1, {})
    if candidate.fact_type == "commitment" and integration.auto_create_tasks:
        task = Commitment.objects.filter(pk=candidate.proposed_changes.get("commitment_id")).first() if candidate.proposed_changes.get("commitment_id") else Commitment.objects.filter(candidate=candidate).first()
        if task:
            queue("task", task, task.version, {})
    if candidate.project_id:
        for party in candidate.project.parties.filter(decision=fact_event.decision).select_related("company"):
            company = party.company
            if company.team_id == candidate.team_id and not company.bitrix_company_id:
                queue("company", company, 1, {"TITLE": company.name})


def _result_id(value):
    external = str(value or "")
    if not external.isdigit() or int(external) < 1:
        raise ProviderUnavailable("crm_invalid_response")
    return external


def deliver_effect(payload):
    from .bitrix_service import BitrixService
    if not AISettings.get_active().autonomous_crm_enabled:
        raise ProviderUnavailable("autonomous_crm_paused")
    integration = BitrixSettings.get_active()
    if not integration.is_active:
        raise ProviderUnavailable("crm_disabled")
    kind, local_id, version = payload["kind"], payload["local_id"], payload["version"]
    with transaction.atomic():
        delivery = CrmDelivery.objects.select_for_update(of=("self",)).select_related("external_object_link", "fact_event__project", "fact_event__decision__candidate").get(outbox_event__deduplication_key=f"autonomous-{kind}:{local_id}:{version}")
        if delivery.state in ("delivered", "superseded"):
            return
        link = delivery.external_object_link
        external = link.external_id
        if kind == "timeline":
            if not integration.sync_timeline_comments:
                raise ProviderUnavailable("crm_timeline_disabled")
            project = delivery.fact_event.project
            if not project or not project.bitrix_id:
                raise ProviderUnavailable("crm_deal_delivery_pending")
            marker = f"[MAZORY-EVENT:{delivery.fact_event_id}]"
            # The documented filter supports only the parent entity. Search
            # pages explicitly; never use an undocumented COMMENT filter.
            if not external:
                for page in range(10):
                    response = BitrixService.call("crm.timeline.comment.list", {"filter": {"ENTITY_ID": project.bitrix_id, "ENTITY_TYPE": "deal"}, "order": {"ID": "DESC"}, "start": page * 50})
                    rows = response.get("result")
                    if not isinstance(rows, list):
                        raise ProviderUnavailable("crm_invalid_response")
                    matches = [row for row in rows if marker in str(row.get("COMMENT", ""))]
                    if len(matches) > 1:
                        raise ProviderUnavailable("crm_origin_conflict")
                    if matches:
                        external = _result_id(matches[0]["ID"])
                        break
                    if not response.get("next"):
                        break
                else:
                    raise ProviderUnavailable("crm_reconciliation_incomplete")
            if not external:
                candidate = delivery.fact_event.decision.candidate
                decision = delivery.fact_event.decision
                text = f"{marker}\nWhatsApp · {delivery.fact_event.event_type}\n{candidate.proposed_changes.get('evidence', '')}\nОснование: {decision.explanation}\nMazory: факт {candidate.id}, событие {delivery.fact_event_id}."
                external = _result_id(BitrixService.call("crm.timeline.comment.add", {"fields": {"ENTITY_ID": project.bitrix_id, "ENTITY_TYPE": "deal", "COMMENT": text}}).get("result"))
        elif kind == "company":
            company = Company.objects.select_for_update().get(pk=local_id)
            external = external or company.bitrix_company_id
            if not external:
                rows = BitrixService.call("crm.company.list", {"filter": {"=ORIGINATOR_ID": "MAZORY", "=ORIGIN_ID": link.origin_key}, "select": ["ID"]}).get("result")
                if not isinstance(rows, list) or len(rows) > 1:
                    raise ProviderUnavailable("crm_origin_conflict")
                if rows:
                    external = _result_id(rows[0]["ID"])
                else:
                    matches = BitrixService.call("crm.company.list", {"filter": {"=TITLE": company.name}, "select": ["ID", "TITLE"]}).get("result")
                    if not isinstance(matches, list):
                        raise ProviderUnavailable("crm_invalid_response")
                    if matches:
                        raise ProviderUnavailable("crm_company_identity_unresolved")
                    external = _result_id(BitrixService.call("crm.company.add", {"fields": {**delivery.patch, "ORIGINATOR_ID": "MAZORY", "ORIGIN_ID": link.origin_key}}).get("result"))
            Company.objects.filter(pk=company.pk).update(bitrix_company_id=external)
        elif kind == "task":
            if not integration.auto_create_tasks:
                raise ProviderUnavailable("crm_task_creation_disabled")
            task = Commitment.objects.select_for_update(of=("self",)).select_related("participant__user_profile", "project").get(pk=local_id)
            if task.version != version:
                delivery.state = "superseded"
                delivery.save(update_fields=["state", "updated_at"])
                return
            external = external or task.bitrix_task_id
            marker = f"[MAZORY-TASK:{task.team_id}:{task.id}]"
            title = f"{marker} {task.commitment_text}"[:255]
            if not external:
                response = BitrixService.call("tasks.task.list", {"filter": {"TITLE": f"{marker}%"}, "select": ["ID", "TITLE", "STATUS"]})
                rows = response.get("result", {}).get("tasks")
                if not isinstance(rows, list) or response.get("next") or len(rows) > 1:
                    raise ProviderUnavailable("crm_origin_conflict")
                if rows:
                    if not str(rows[0].get("title", "")).startswith(marker):
                        raise ProviderUnavailable("crm_origin_conflict")
                    external = _result_id(rows[0].get("id"))
            if not external and task.status in ("fulfilled", "cancelled"):
                delivery.state, delivery.error_code = "superseded", "historical_task_timeline_only"
                delivery.save(update_fields=["state", "error_code", "updated_at"])
                return
            fields = {"TITLE": title, "DESCRIPTION": f"WhatsApp. Исполнитель: {task.responsible_name}.\n{task.commitment_text}\nСостояние Mazory: {task.status}. Основание: событие {delivery.fact_event_id}."}
            if task.project_id:
                if not task.project.bitrix_id:
                    raise ProviderUnavailable("crm_deal_delivery_pending")
                fields["UF_CRM_TASK"] = [f"D_{task.project.bitrix_id}"]
            # A date is not an invented exact time; preserve its day-level
            # meaning in the description and avoid a fake CRM time deadline.
            if task.deadline_at and task.deadline_precision == "datetime":
                fields["DEADLINE"] = task.deadline_at.isoformat()
            elif task.deadline:
                fields["DESCRIPTION"] += f"\nСрок по переписке: {task.deadline.isoformat()} (дата, время не указано)."
            if not external:
                profile = task.participant.user_profile if task.participant_id else None
                if not profile or not profile.bitrix_user_id:
                    raise ProviderUnavailable("blocked_missing_external_assignee")
                fields["RESPONSIBLE_ID"] = int(_result_id(profile.bitrix_user_id))
                external = _result_id(BitrixService.call("tasks.task.add", {"fields": fields}).get("result", {}).get("task", {}).get("id"))
            else:
                BitrixService.call("tasks.task.update", {"taskId": int(external), "fields": fields})
                current = BitrixService.call("tasks.task.get", {"taskId": int(external), "select": ["ID", "STATUS"]}).get("result", {}).get("task", {})
                if task.status == "fulfilled" and str(current.get("status")) not in ("4", "5"):
                    BitrixService.call("tasks.task.complete", {"taskId": int(external)})
                elif task.status == "cancelled" and str(current.get("status")) != "6":
                    # Bitrix has no cancelled status: mark cancellation in the
                    # description and defer; never falsely complete the task.
                    BitrixService.call("tasks.task.defer", {"taskId": int(external)})
            Commitment.objects.filter(pk=task.pk).update(bitrix_task_id=external)
        else:
            raise ProviderUnavailable("crm_command_invalid")
        link.external_id = external
        link.save(update_fields=["external_id"])
        delivery.state, delivery.error_code = "delivered", ""
        delivery.save(update_fields=["state", "error_code", "updated_at"])
