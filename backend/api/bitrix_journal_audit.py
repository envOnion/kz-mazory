"""Reconstruct known historical corrections from audit evidence, without writes.

The audit records the historical action. Provider reads only establish the
current state; neither an absent object nor its current fields prove a write
without that separate audit record.
"""

from django.conf import settings
from django.utils import timezone

from .models import AuditEvent, BitrixDealChangeLog, Commitment, CrmDelivery, Project


ACTIONS = (
    "source_review_crm_correction",
    "remove_unconfirmed_boiler_comments",
    "consolidate_owned_source_duplicate",
    "remove_provider_default_deadline",
)


class EvidenceUnavailable(Exception):
    """A local, non-sensitive reason why current evidence is insufficient."""


def _external_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    value = str(value)
    return value if value.isascii() and value.isdigit() and int(value) > 0 else ""


def _deal_project(audit, external):
    if audit.target_type == "Project":
        project = Project.objects.filter(pk=audit.target_id, bitrix_id=external).first()
        if project:
            return project
    return Project.objects.filter(bitrix_id=external).first()


def _comment_parent(audit, external, explicit_deal):
    if explicit_deal:
        return _deal_project(audit, explicit_deal), explicit_deal
    # A correction may remove a deal and a comment on a DIFFERENT deal. Never
    # infer the comment's parent from the removed_deal field of the same audit.
    projects = {
        delivery.fact_event.project_id: delivery.fact_event.project
        for delivery in CrmDelivery.objects.filter(
            external_object_link__object_type="timeline",
            external_object_link__external_id=external,
            fact_event__project__isnull=False,
        ).select_related("fact_event__project")
    }
    if len(projects) == 1:
        project = next(iter(projects.values()))
        return project, _external_id(project.bitrix_id)
    return None, ""


def _targets(audit):
    data = audit.before_after
    if not isinstance(data, dict):
        return []
    targets = []

    def deleted(kind, value, verified, explicit_deal=""):
        external = _external_id(value)
        if not external:
            return
        if kind == "deal":
            project, deal = _deal_project(audit, external), external
            method = "crm.deal.delete"
        else:
            project, deal = _comment_parent(audit, external, explicit_deal)
            method = "crm.timeline.comment.delete"
        targets.append({
            "kind": kind, "external": external, "method": method,
            "action": "delete", "project": project, "deal": deal,
            "params": {"id": external}, "audit_verified": verified,
        })

    if audit.action == "source_review_crm_correction":
        verified = data.get("readback_verified") is True
        deleted("deal", data.get("removed_deal"), verified)
        deleted("timeline", data.get("removed_comment"), verified,
                _external_id(data.get("external_deal")))
    elif audit.action == "remove_unconfirmed_boiler_comments":
        comments = data.get("removed_comments", [])
        if isinstance(comments, list):
            for external in comments:
                deleted("timeline", external, data.get("readback_verified") is True,
                        _external_id(data.get("external_deal")))
    elif audit.action == "consolidate_owned_source_duplicate":
        deleted("deal", data.get("removed_crm_id"), data.get("readback_absent") is True)
    elif audit.action == "remove_provider_default_deadline" and audit.target_type == "Commitment":
        task = Commitment.objects.select_related("project").filter(pk=audit.target_id).first()
        external = _external_id(task.bitrix_task_id) if task else ""
        if external:
            before, after = data.get("before"), data.get("after")
            verified = isinstance(before, dict) and isinstance(after, dict) and "deadline" in before and "deadline" in after
            targets.append({
                "kind": "task", "external": external, "method": "tasks.task.update",
                "action": "change", "project": task.project,
                "deal": _external_id(task.project.bitrix_id) if task.project else "",
                "params": {"taskId": int(external), "fields": {"DEADLINE": ""}},
                "audit_verified": verified, "task": task,
                "expected_deadline": after.get("deadline") if isinstance(after, dict) else None,
            })
    return targets


def _readback(target):
    from .bitrix_service import BitrixService

    external = target["external"]
    if target["kind"] == "deal":
        response = BitrixService.call("crm.deal.list", {"filter": {"=ID": external}, "select": ["ID"]})
        rows = response.get("result")
        if not isinstance(rows, list) or response.get("next"):
            raise EvidenceUnavailable("incomplete_deal_readback")
        if rows:
            raise EvidenceUnavailable("removed_deal_still_present")
        return {"method": "crm.deal.list", "id": external, "absent": True}

    if target["kind"] == "timeline":
        if not target["deal"]:
            raise EvidenceUnavailable("comment_parent_unknown")
        start = 0
        seen_starts = set()
        for _ in range(10):
            if start in seen_starts:
                raise EvidenceUnavailable("incomplete_comment_readback")
            seen_starts.add(start)
            response = BitrixService.call("crm.timeline.comment.list", {
                "filter": {"ENTITY_ID": target["deal"], "ENTITY_TYPE": "deal"},
                "order": {"ID": "DESC"}, "start": start,
            })
            rows = response.get("result")
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not _external_id(row.get("ID")) for row in rows):
                raise EvidenceUnavailable("invalid_comment_readback")
            if any(str(row["ID"]) == external for row in rows):
                raise EvidenceUnavailable("removed_comment_still_present")
            next_page = response.get("next")
            if next_page is None:
                return {"method": "crm.timeline.comment.list", "id": external,
                        "parent_deal_id": target["deal"], "absent": True,
                        "pages_read": len(seen_starts)}
            if isinstance(next_page, bool) or not isinstance(next_page, int) or next_page <= start:
                raise EvidenceUnavailable("incomplete_comment_readback")
            start = next_page
        raise EvidenceUnavailable("incomplete_comment_readback")

    response = BitrixService.call("tasks.task.get", {
        "taskId": int(external), "select": ["ID", "TITLE", "DEADLINE", "RESPONSIBLE_ID"],
    })
    result = response.get("result")
    current = result.get("task") if isinstance(result, dict) else None
    task = target["task"]
    marker = f"[MAZORY-TASK:{task.team_id}:{task.id}]"
    if not isinstance(current, dict) or str(current.get("id")) != external or not str(current.get("title", "")).startswith(marker):
        raise EvidenceUnavailable("task_identity_unconfirmed")
    if "deadline" not in current or (current["deadline"] or "") != (target["expected_deadline"] or ""):
        raise EvidenceUnavailable("task_deadline_differs_from_audit")
    return {"method": "tasks.task.get", **{key: current.get(key) for key in ("id", "title", "deadline", "responsibleId")}}


def reconstruct_audit_history():
    """Run in Q2. Read remote state and upsert one row per audited operation."""
    from .bitrix_journal import _safe

    counts = {"confirmed": 0, "unavailable": 0, "skipped": 0}
    for audit in AuditEvent.objects.filter(action__in=ACTIONS).order_by("id").iterator():
        targets = _targets(audit)
        if not targets:
            counts["skipped"] += 1
        for target in targets:
            key = f"history-audit:{audit.id}:{target['kind']}:{target['external']}"
            if BitrixDealChangeLog.objects.filter(journal_key=key, status="confirmed").exists():
                counts["skipped"] += 1
                continue
            proof, error = {}, ""
            try:
                if not target["audit_verified"]:
                    raise EvidenceUnavailable("audit_readback_evidence_missing")
                proof = _readback(target)
            except EvidenceUnavailable as exc:
                error = str(exc)
            except Exception as exc:
                # Provider failures, including permission errors and timeouts,
                # are never interpreted as proof that an object was deleted.
                error = type(exc).__name__
            state = "unavailable" if error else "confirmed"
            project = target["project"]
            row, _ = BitrixDealChangeLog.objects.update_or_create(journal_key=key, defaults={
                "project": project, "source_project_id": project.pk if project else None,
                "object_type": target["kind"], "external_id": target["external"],
                "bitrix_deal_id": target["deal"], "method": target["method"],
                "action": target["action"], "status": state, "reconstructed": True,
                "changed_fields": ["DEADLINE"] if target["kind"] == "task" else [],
                "payload": {"historical_request": target["params"], "audit_id": audit.id,
                            "audit_action": audit.action, "audit_evidence": _safe(audit.before_after)},
                "triggered_by": f"history_audit:{audit.id}", "error_message": error,
                "response_data": {"environment": settings.MAZORY_ENV,
                    "readback_at": timezone.now().isoformat(), "proof": _safe(proof),
                    "audit_id": audit.id, "timestamp_source": "AuditEvent.created_at",
                    "note": "Историческое действие восстановлено из AuditEvent. Отдельное чтение проверяет только текущее состояние объекта. Время события — время записи аудита; повторная отправка в Bitrix не выполнялась."},
            })
            BitrixDealChangeLog.objects.filter(pk=row.pk).update(created_at=audit.created_at)
            counts[state] += 1
    return counts
