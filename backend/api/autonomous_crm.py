"""Versioned, field-limited CRM commands run exclusively by the CRM worker."""

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from decimal import Decimal, InvalidOperation

from .models import AISettings, BitrixSettings, Commitment, Company, CrmDelivery, ExternalObjectLink, FieldAssertion, OutboxEvent, Project
from .providers import ProviderUnavailable


def verify_deal(external, patch):
    from .bitrix_service import BitrixService
    current = BitrixService.call("crm.deal.get", {"id": external}).get("result")
    if not isinstance(current, dict) or str(current.get("ID")) != external:
        raise ProviderUnavailable("crm_readback_failed")
    for field, expected in patch.items():
        actual = current.get(field)
        if field == "OPPORTUNITY":
            try:
                matches = Decimal(str(actual)) == Decimal(str(expected))
            except InvalidOperation:
                matches = False
        else:
            matches = str(actual) == str(expected)
        if not matches:
            raise ProviderUnavailable("crm_readback_mismatch")


def _current_price_assertion(project):
    """Use the same source ordering as commercial_amount."""
    known = set(project.whatsapp_fields)
    fields = []
    if project.sale_amount is not None and 'sale_amount' in known:
        fields.append('sale_amount')
    if project.contract_known and 'contract_amount' in known:
        fields.append('contract_amount')
    assertions = FieldAssertion.objects.filter(project=project, field_name__in=fields,
        fact_event__is_superseded=False).select_related('fact_event').order_by(
            F('fact_event__occurred_at').desc(nulls_last=True), '-id')
    for assertion in assertions:
        try:
            value = Decimal(str(assertion.value))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if value.is_finite() and value > 0:
            return assertion
    return None


def _price_has_own_delivery(project):
    """A status update cannot revive a blocked price or bypass its readback."""
    assertion = _current_price_assertion(project)
    return bool(assertion and assertion.fact_event.event_key.startswith('sale-clarification-reply:'))


def enqueue_project(project, fact_event, cfg=None, *, price_only=False, crm_baseline=None, force=False):
    cfg = cfg or AISettings.get_active()
    if not cfg.autonomous_crm_enabled and not force:
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
    if price_only and not isinstance(crm_baseline, dict):
        raise ValueError('price_delivery_baseline_required')
    data = fact_event.payload if isinstance(fact_event.payload, dict) else {}
    patch = {}
    field_map = integration.deal_field_map if isinstance(integration.deal_field_map, dict) else {}
    for source, key in (("object_name", "object_name"), ("equipment_type", "equipment_type"),
                        ("meeting_at", "meeting_at"), ("project_received", "project_received"),
                        ("deal_period", "deal_period")):
        value = data.get(source)
        code = field_map.get(key)
        if value is not None and value != "" and code:
            if key == "project_received":
                value = "82" if value is True else "84"
            patch[code] = value
    for key in ("paid_amount", "due_amount"):
        code = field_map.get(key)
        value = data.get(key)
        if code and value is not None and value != "":
            patch[code] = str(value)
    stage_missing = False
    if data.get("sales_stage"):
        stage_id = integration.deal_stage_map.get(data["sales_stage"])
        if stage_id and (project.bitrix_id or project.identity_confirmed):
            patch["STAGE_ID"] = stage_id
        elif not stage_id and (project.bitrix_id or project.identity_confirmed):
            stage_missing = True
    if ("sale_amount" in data or "contract_amount" in data) or price_only and "OPPORTUNITY" in (crm_baseline or {}):
        from .commercial_amounts import commercial_amount
        price = commercial_amount(project)
        if price is not None and not price_only:
            patch.update(OPPORTUNITY=str(price), CURRENCY_ID=project.currency)
    if data.get("fact_type") == "payment" or fact_event.event_type == "payment":
        if data.get("direction") == "income" and data.get("amount_precision") == "exact" and project.paid_amount is not None and data.get("payment_kind", "increment") in ("increment", "reversal"):
            for key, value in (("paid_amount", project.paid_amount),
                               ("due_amount", project.due_amount if project.contract_known else None)):
                code = field_map.get(key)
                if code and value is not None:
                    patch[code] = str(value)
    if price_only:
        patch = {key: value for key, value in patch.items() if key in {"OPPORTUNITY", "CURRENCY_ID"}}
    if not project.bitrix_id and not project.identity_confirmed:
        patch.pop("STAGE_ID", None)
        stage_missing = False
        patch = {key: value for key, value in patch.items() if key != "OPPORTUNITY"}
    if not integration.auto_create_deals and not project.bitrix_id:
        return
    snapshot = getattr(project, "crm_snapshot", None)
    external_fields = snapshot.external_fields if snapshot and isinstance(snapshot.external_fields, dict) else {}
    baseline = {key: external_fields[key] for key in patch if key in external_fields}
    if "STAGE_ID" in patch and snapshot:
        baseline["STAGE_ID"] = snapshot.external_stage_id
    payload = {"project_id": project.id, "version": project.version,
               "crm_baseline": crm_baseline if price_only else None,
               "field_baseline": baseline,
               "crm_baseline_synced_at": snapshot.synced_at.isoformat() if snapshot else None}
    if price_only:
        if patch and set(patch) != {'OPPORTUNITY', 'CURRENCY_ID'}:
            raise ValueError('price_delivery_amount_required')
        if patch:
            payload.update(price_only=True, crm_baseline=crm_baseline)
        else:
            return
    if not patch and not stage_missing:
        return
    event, created = OutboxEvent.objects.get_or_create(deduplication_key=f"autonomous-crm:{project.id}:{project.version}", defaults={
        "event_type": "autonomous_crm", "payload": payload,
    })
    if created and (patch or price_only or stage_missing) and (project.bitrix_id or project.identity_confirmed):
        CrmDelivery.objects.create(outbox_event=event, external_object_link=link, fact_event=fact_event, target_version=project.version, patch=patch, error_code="crm_stage_mapping_missing" if stage_missing else "")


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
        if delivery.state in ("delivered", "superseded", "blocked", "partial", "cancelled"):
            return
        if delivery.error_code == "crm_stage_mapping_missing":
            delivery.state = "blocked"
            delivery.save(update_fields=["state", "updated_at"])
            return
        if not delivery.patch:
            delivery.state, delivery.error_code = "blocked", "crm_no_safe_fields"
            delivery.save(update_fields=["state", "error_code", "updated_at"])
            return
        price_only = payload.get('price_only') is True
        current_price = _current_price_assertion(project) if price_only else None
        baseline = payload.get('crm_baseline', {})
        try:
            same_price = current_price and Decimal(str(current_price.value)) == Decimal(str(delivery.patch.get('OPPORTUNITY')))
        except (InvalidOperation, ValueError, TypeError):
            same_price = False
        # A new stage/description does not invalidate an already accepted
        # price. A newer price source or changed identity still does.
        price_is_current = price_only and isinstance(baseline, dict) and same_price and (
            current_price.fact_event_id == delivery.fact_event_id
            and current_price.fact_event.team_id == project.team_id
            and not project.archived
            and project.bitrix_id == str(baseline.get('ID'))
            and str(delivery.external_object_link.external_id) == str(baseline.get('ID'))
            and project.name == baseline.get('TITLE')
            and project.currency == delivery.patch.get('CURRENCY_ID'))
        if price_only and not price_is_current or not price_only and project.version != version:
            delivery.state = "superseded"
            delivery.save(update_fields=["state", "updated_at"])
            return
        if not price_only and _price_has_own_delivery(project):
            delivery.patch.pop('OPPORTUNITY', None)
            delivery.patch.pop('CURRENCY_ID', None)
            delivery.save(update_fields=['patch'])
        integration = BitrixSettings.get_active()
        if not integration.is_active:
            raise ProviderUnavailable("crm_disabled")
        link = delivery.external_object_link
        external = link.external_id or project.bitrix_id
        if price_only and not external:
            raise ProviderUnavailable('price_delivery_identity_missing')
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
            if price_only:
                baseline = payload.get('crm_baseline', {})
                identity_matches = isinstance(baseline, dict) and set(delivery.patch) == {'OPPORTUNITY', 'CURRENCY_ID'} and all(
                    str(current.get(key, '')) == str(baseline.get(key, ''))
                    for key in ('ID', 'TITLE', 'CURRENCY_ID', 'ORIGINATOR_ID', 'ORIGIN_ID'))
                try:
                    amount_matches = Decimal(str(current.get('OPPORTUNITY'))) in (
                        Decimal(str(baseline.get('OPPORTUNITY'))), Decimal(str(delivery.patch.get('OPPORTUNITY'))))
                except (InvalidOperation, TypeError):
                    amount_matches = False
                if not identity_matches or not amount_matches:
                    delivery.state, delivery.error_code = 'blocked', 'crm_price_changed'
                    delivery.save(update_fields=['state', 'error_code', 'updated_at'])
                    return
            # Only fields unchanged since the captured CRM baseline are writable.
            baseline = payload.get("field_baseline", {})
            patch, conflicts = {}, []
            category = integration.deal_category_id
            stage_code = "DEAL_STAGE" if category == 0 else f"DEAL_STAGE_{category}"
            stages = BitrixService.call("crm.status.list", {"filter": {"ENTITY_ID": stage_code}}).get("result", []) if "STAGE_ID" in delivery.patch else []
            valid_stages = {str(row.get("STATUS_ID")) for row in stages if isinstance(row, dict) and str(row.get("ENTITY_ID")) == stage_code}
            for key, value in delivery.patch.items():
                if key == "STAGE_ID" and str(value) not in valid_stages:
                    conflicts.append("STAGE_ID")
                    continue
                if key == "STAGE_ID" and current.get("STAGE_SEMANTIC_ID") in ("S", "F"):
                    conflicts.append(key)
                    continue
                if str(current.get(key, "")) == str(value):
                    continue
                if key not in baseline or str(current.get(key, "")) != str(baseline[key]):
                    conflicts.append(key)
                    continue
                patch[key] = value
            if patch:
                BitrixService.call("crm.deal.update", {"id": external, "fields": patch})
        else:
            if not integration.auto_create_deals:
                raise ProviderUnavailable("crm_creation_disabled")
            candidate = delivery.fact_event.decision.candidate if delivery.fact_event and delivery.fact_event.decision_id else None
            if not candidate or candidate.crm_match_state != "not_found":
                raise ProviderUnavailable("crm_identity_unresolved")
            if "STAGE_ID" in delivery.patch:
                category = integration.deal_category_id
                stage_code = "DEAL_STAGE" if category == 0 else f"DEAL_STAGE_{category}"
                stages = BitrixService.call("crm.status.list", {"filter": {"ENTITY_ID": stage_code}}).get("result", [])
                if str(delivery.patch["STAGE_ID"]) not in {str(row.get("STATUS_ID")) for row in stages if isinstance(row, dict) and str(row.get("ENTITY_ID")) == stage_code}:
                    delivery.state, delivery.error_code = "blocked", "crm_invalid_stage_mapping"
                    delivery.save(update_fields=["state", "error_code", "updated_at"])
                    return
            fields = {
                **delivery.patch,
                "TITLE": project.name,
                "ORIGINATOR_ID": "MAZORY",
                "ORIGIN_ID": link.origin_key,
                "CATEGORY_ID": integration.deal_category_id,
            }
            external = str(BitrixService.call("crm.deal.add", {"fields": fields})["result"])
            patch, conflicts = fields, []
        if not external or not external.isdigit():
            raise ProviderUnavailable("crm_invalid_response")
        if patch:
            verify_deal(external, patch)
        link.external_id = external
        link.save(update_fields=["external_id"])
        sync_state = {'bitrix_id': external, 'last_bitrix_synced_at': timezone.now()}
        if project.version == version:
            sync_state['needs_bitrix_sync'] = False
        Project.objects.filter(pk=project.id).update(**sync_state)
        delivery.state = "partial" if conflicts else "delivered"
        delivery.error_code = "crm_human_edit_conflict" if conflicts else ""
        delivery.save(update_fields=["state", "error_code", "updated_at"])


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
    if candidate.fact_type == "commitment" and integration.auto_create_tasks:
        task = Commitment.objects.filter(pk=candidate.proposed_changes.get("commitment_id")).first() if candidate.proposed_changes.get("commitment_id") else Commitment.objects.filter(candidate=candidate).first()
        if task:
            queue("task", task, task.version, {})
    if candidate.project_id:
        for party in candidate.project.parties.filter(decision=fact_event.decision).select_related("company"):
            company = party.company
            if party.role != "unknown" and company.team_id == candidate.team_id and not company.bitrix_company_id:
                queue("company", company, 1, {"TITLE": company.name})


def _result_id(value):
    external = str(value or "")
    if not external.isdigit() or int(external) < 1:
        raise ProviderUnavailable("crm_invalid_response")
    return external


def deliver_effect(payload):
    from .bitrix_service import BitrixService
    if payload.get("kind") == "timeline":
        CrmDelivery.objects.filter(
            outbox_event__deduplication_key=f"autonomous-timeline:{payload['local_id']}:1",
            state__in=("pending", "retrying"),
        ).update(state="cancelled", error_code="crm_timeline_disabled")
        return
    if not AISettings.get_active().autonomous_crm_enabled:
        raise ProviderUnavailable("autonomous_crm_paused")
    integration = BitrixSettings.get_active()
    if not integration.is_active:
        raise ProviderUnavailable("crm_disabled")
    kind, local_id, version = payload["kind"], payload["local_id"], payload["version"]
    with transaction.atomic():
        delivery = CrmDelivery.objects.select_for_update(of=("self",)).select_related("external_object_link", "fact_event__project", "fact_event__decision__candidate").get(outbox_event__deduplication_key=f"autonomous-{kind}:{local_id}:{version}")
        if delivery.state in ("delivered", "superseded", "blocked", "partial", "cancelled"):
            return
        if delivery.error_code == "crm_stage_mapping_missing":
            delivery.state = "blocked"
            delivery.save(update_fields=["state", "updated_at"])
            return
        link = delivery.external_object_link
        external = link.external_id
        if kind == "company":
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
                # The live portal treats an appended '%' literally. Search
                # the literal marker, then validate the returned title below.
                response = BitrixService.call("tasks.task.list", {"filter": {"TITLE": marker}, "select": ["ID", "TITLE", "STATUS"]})
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
            fields["DEADLINE"] = ""
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
                assigned = profile.bitrix_user_id if profile else None
                if not assigned:
                    # Explicit, team-scoped operator mappings do not create
                    # phone accounts or grant access to WhatsApp participants.
                    assigned = settings.BITRIX_ASSIGNEE_MAP.get(str(task.team_id), {}).get(task.responsible_name.strip().casefold())
                if not assigned:
                    raise ProviderUnavailable("blocked_missing_external_assignee")
                fields["RESPONSIBLE_ID"] = int(_result_id(assigned))
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
            readback_fields = ["ID", "TITLE", "RESPONSIBLE_ID", "UF_CRM_TASK", "STATUS", "DEADLINE"]
            current = BitrixService.call("tasks.task.get", {"taskId": int(external), "select": readback_fields}).get("result", {}).get("task", {})
            if str(current.get("id")) != external or current.get("title") != title:
                raise ProviderUnavailable("crm_readback_mismatch")
            # Bitrix can insert its configured default on creation even when
            # no source time was provided. Clear it and verify the write.
            if not fields["DEADLINE"] and current.get("deadline"):
                BitrixService.call("tasks.task.update", {"taskId": int(external), "fields": {"DEADLINE": ""}})
                current = BitrixService.call("tasks.task.get", {"taskId": int(external), "select": readback_fields}).get("result", {}).get("task", {})
                if str(current.get("id")) != external or current.get("title") != title or current.get("deadline"):
                    raise ProviderUnavailable("crm_readback_mismatch")
            if "RESPONSIBLE_ID" in fields and str(current.get("responsibleId")) != str(fields["RESPONSIBLE_ID"]):
                raise ProviderUnavailable("crm_readback_mismatch")
            if "UF_CRM_TASK" in fields and current.get("ufCrmTask") != fields["UF_CRM_TASK"]:
                raise ProviderUnavailable("crm_readback_mismatch")
            Commitment.objects.filter(pk=task.pk).update(bitrix_task_id=external)
        else:
            raise ProviderUnavailable("crm_command_invalid")
        link.external_id = external
        link.save(update_fields=["external_id"])
        delivery.state, delivery.error_code = "delivered", ""
        delivery.save(update_fields=["state", "error_code", "updated_at"])
