"""Fact Review MCP Server with Streamable HTTP transport for Agent LLM."""

import json
from decimal import Decimal
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from rest_framework.exceptions import PermissionDenied, ValidationError

from . import access
from .facts import review, select_crm_match, lock_candidate_source
from .models import FactCandidate, Project, AuditEvent, ThreadMessage
from .notifications import notify_on_candidate_approved
from .security import Conflict


def create_fact_review_mcp(user):
    """Creates a user-scoped FastMCP server instance for Fact Review operations."""
    mcp = FastMCP(
        "mazory-fact-review",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool()
    def list_candidates(status: str = "pending", fact_type: str = "", page: int = 1) -> str:
        """Получить список предложений (фактов) на проверку.

        Args:
            status: Статус предложений ('pending', 'approved', 'rejected', 'all'). По умолчанию 'pending'.
            fact_type: Фильтр по типу факта ('project', 'payment', 'commitment' или пусто для всех).
            page: Номер страницы (начиная с 1, по 20 записей на страницу).
        """
        qs = access.candidates_for(user)
        if status != "all":
            qs = qs.filter(status=status)
        if fact_type in ("project", "payment", "commitment"):
            qs = qs.filter(fact_type=fact_type)

        total_count = qs.count()
        page_num = max(1, page)
        offset = (page_num - 1) * 20
        candidates = list(
            qs.select_related("project__company", "trace", "thread_revision__thread")
            .prefetch_related("crm_matches")
            .order_by("-id")[offset : offset + 20]
        )

        items = []
        for c in candidates:
            company_name = None
            if c.project and c.project.company:
                company_name = c.project.company.name
            elif isinstance(c.proposed_changes, dict):
                company_name = c.proposed_changes.get("company_name")

            items.append({
                "id": c.id,
                "fact_type": c.fact_type,
                "status": c.status,
                "project_id": c.project_id,
                "project_name": c.project.name if c.project else None,
                "company_name": company_name,
                "proposed_changes": c.proposed_changes,
                "confidence": c.trace.ai_confidence if c.trace else None,
                "crm_resolution_state": c.crm_match_state,
                "crm_matches_count": c.crm_matches.count(),
                "base_version": c.base_project_version or 0,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            })

        return json.dumps(
            {
                "total_count": total_count,
                "page": page_num,
                "page_size": 20,
                "candidates": items,
            },
            ensure_ascii=False,
        )

    @mcp.tool()
    def get_candidate_details(candidate_id: int) -> str:
        """Получить полные структурированные сведения по предложению для проверки.

        Args:
            candidate_id: Идентификатор кандидата на факт.
        """
        c = get_object_or_404(
            access.candidates_for(user).select_related("project__company", "trace"),
            pk=candidate_id,
        )

        evidence_list = [
            {
                "quote": e.quote,
                "role": e.field_name,
                "raw_message_id": e.raw_message_id,
            }
            for e in c.evidence.all()
        ]

        crm_matches = [
            {
                "id": m.id,
                "bitrix_deal_id": m.bitrix_deal_id,
                "title": m.deal_title,
                "company_title": m.company_name,
                "opportunity": str(m.opportunity) if m.opportunity is not None else None,
                "stage_id": m.stage_id,
                "selection_state": m.selection_state,
            }
            for m in c.crm_matches.all()
        ]

        company_name = None
        if c.project and c.project.company:
            company_name = c.project.company.name
        elif isinstance(c.proposed_changes, dict):
            company_name = c.proposed_changes.get("company_name")

        data = {
            "id": c.id,
            "fact_type": c.fact_type,
            "status": c.status,
            "base_version": c.base_project_version or 0,
            "project_id": c.project_id,
            "project_name": c.project.name if c.project else None,
            "project_version": c.project.version if c.project else None,
            "company_name": company_name,
            "proposed_changes": c.proposed_changes,
            "evidence": evidence_list,
            "crm_matches": crm_matches,
            "crm_match_state": c.crm_match_state,
            "crm_match_revision": c.crm_match_revision,
            "review_reason": c.review_reason,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        return json.dumps(data, ensure_ascii=False, indent=2)

    @mcp.tool()
    def get_candidate_context(candidate_id: int) -> str:
        """Получить переписку WhatsApp и контекст диалога вокруг найденного факта.

        Args:
            candidate_id: Идентификатор кандидата на факт.
        """
        c = get_object_or_404(
            access.candidates_for(user).select_related("trace__raw_message__config"),
            pk=candidate_id,
        )

        trace_data = None
        source_message = None
        thread_messages_list = []

        if c.trace:
            trace_data = {
                "confidence": c.trace.ai_confidence,
                "pipeline_action": c.trace.pipeline_action,
                "thought_trace": c.trace.result_summary or "",
            }
            raw = c.trace.raw_message
            if raw and access.messages_for(user).filter(pk=raw.pk).exists():
                source_message = {
                    "id": raw.id,
                    "sender_phone": raw.sender_phone,
                    "sender_name": raw.sender_name,
                    "content": raw.content,
                    "sent_at": raw.timestamp.isoformat() if raw.timestamp else None,
                    "chat_name": getattr(raw.config, "chat_name", "") if hasattr(raw, "config") else "",
                }

                # Find dialogue thread messages
                tm = ThreadMessage.objects.filter(raw_message=raw).select_related("thread").first()
                if tm and tm.thread:
                    msgs = (
                        ThreadMessage.objects.filter(thread=tm.thread)
                        .select_related("raw_message")
                        .order_by("raw_message__timestamp")
                    )
                    for m in msgs:
                        r = m.raw_message
                        if r and access.messages_for(user).filter(pk=r.pk).exists():
                            thread_messages_list.append({
                                "id": r.id,
                                "sender": r.sender_name or r.sender_phone,
                                "content": r.content,
                                "sent_at": r.timestamp.isoformat() if r.timestamp else None,
                                "thought_state": m.thought_state,
                                "relation": m.relation,
                            })

        evidence_quotes = [
            {
                "quote": e.quote,
                "role": e.field_name,
                "raw_message_id": e.raw_message_id,
            }
            for e in c.evidence.all()
        ]

        result = {
            "candidate_id": c.id,
            "fact_type": c.fact_type,
            "trace": trace_data,
            "source_message": source_message,
            "thread_messages": thread_messages_list,
            "evidence_quotes": evidence_quotes,
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    @mcp.tool()
    def list_projects(search: str = "", page: int = 1) -> str:
        """Получить список существующих проектов / объектов для сопоставления.

        Args:
            search: Поисковый запрос по названию проекта или контрагента.
            page: Номер страницы (по 20 на страницу).
        """
        qs = access.projects_for(user).select_related("company")
        if search.strip():
            qs = qs.filter(Q(name__icontains=search.strip()) | Q(company__name__icontains=search.strip()))

        total_count = qs.count()
        page_num = max(1, page)
        offset = (page_num - 1) * 20
        projects = list(qs.order_by("name")[offset : offset + 20])

        items = [
            {
                "id": p.id,
                "name": p.name,
                "company_name": p.company.name if p.company else None,
                "version": p.version,
                "bitrix_id": p.bitrix_id,
                "contract_amount": str(p.contract_amount) if p.contract_amount is not None else None,
                "currency": p.currency,
            }
            for p in projects
        ]

        return json.dumps(
            {
                "total_count": total_count,
                "page": page_num,
                "page_size": 20,
                "projects": items,
            },
            ensure_ascii=False,
        )

    @mcp.tool()
    def review_candidate(
        candidate_id: int,
        action: str,
        reason: str,
        changes: dict | None = None,
        project_id: int | None = None,
        crm_match_id: int | None = None,
        crm_match_revision: int | None = None,
        base_version: int | None = None,
    ) -> str:
        """Принять решение по предложению: подтвердить, исправить, отклонить или связать с проектом.

        Args:
            candidate_id: Идентификатор кандидата на факт.
            action: Действие: 'approve' (подтвердить), 'reject' (отклонить), 'match' (связать с проектом), 'match_crm' (выбрать сделку Bitrix).
            reason: Обязательное текстовое основание решения.
            changes: Словарь исправлений (например: {'amount': '50000000.00', 'deadline_at': '2026-10-15'}).
            project_id: Идентификатор проекта (обязателен при action='match').
            crm_match_id: Идентификатор варианта CRM сделки (обязателен при action='match_crm').
            crm_match_revision: Версия сопоставления CRM сделки (опционально при action='match_crm').
            base_version: Ожидаемая версия записи для защиты от одновременного редактирования.
        """
        if action not in ("approve", "reject", "match", "match_crm"):
            return json.dumps({"error": f"Неизвестное действие '{action}'. Допустимо: approve, reject, match, match_crm."})

        if not reason or not reason.strip():
            return json.dumps({"error": "Требуется указать основание (reason) для выполняемого действия."})

        candidate = get_object_or_404(access.candidates_for(user), pk=candidate_id)

        try:
            if action == "match_crm":
                if not crm_match_id:
                    return json.dumps({"error": "Укажите crm_match_id для выбора варианта CRM сделки."})
                rev = crm_match_revision if crm_match_revision is not None else candidate.crm_match_revision
                ver = base_version if base_version is not None else (candidate.base_project_version or 0)
                res = select_crm_match(candidate_id, user, crm_match_id, rev, ver, reason)
                return json.dumps({
                    "success": True,
                    "action": "match_crm",
                    "candidate_id": res.id,
                    "status": res.status,
                    "crm_match_state": res.crm_match_state,
                    "message": "CRM-сделка выбрана; подтвердите факт отдельно.",
                }, ensure_ascii=False)

            elif action == "match":
                if not project_id:
                    return json.dumps({"error": "Укажите project_id для связывания факта с проектом."})
                with transaction.atomic():
                    lock_candidate_source(candidate)
                    candidate = (
                        access.candidates_for(user)
                        .select_for_update(of=("self",))
                        .get(pk=candidate.id)
                    )
                    access.require_review(user, candidate)
                    if candidate.status != "pending":
                        return json.dumps({"error": f"Предложение уже имеет статус '{candidate.status}', изменение невозможно."})

                    project = get_object_or_404(
                        access.projects_for(user).select_for_update(of=("self",)),
                        pk=project_id,
                        team=candidate.team,
                    )
                    exp_version = base_version if base_version is not None else project.version
                    if exp_version != project.version:
                        return json.dumps({"error": f"Конфликт версий проекта: ожидалась {exp_version}, актуальная {project.version}."})

                    candidate.project = project
                    candidate.base_project_version = project.version
                    candidate.save(update_fields=["project", "base_project_version"])

                    AuditEvent.objects.create(
                        actor=user,
                        target_type="FactCandidate",
                        target_id=candidate.id,
                        action="match",
                        before_after={
                            "project_id": project.id,
                            "base_version": project.version,
                            "reason": reason,
                        },
                    )

                    return json.dumps({
                        "success": True,
                        "action": "match",
                        "candidate_id": candidate.id,
                        "project_id": project.id,
                        "project_name": project.name,
                        "message": f"Факт #{candidate.id} успешно связан с проектом '{project.name}'.",
                    }, ensure_ascii=False)

            else:
                # approve or reject
                if action == "approve" and candidate.trace and candidate.trace.raw_message_id:
                    if not access.messages_for(user).filter(pk=candidate.trace.raw_message_id).exists():
                        return json.dumps({"error": "Для подтверждения необходим доступ к первоисточнику сообщения."})

                combined_changes = dict(changes or {})
                if "fact_type" not in combined_changes and "fact_type" not in candidate.proposed_changes:
                    combined_changes["fact_type"] = candidate.fact_type
                if "evidence" not in combined_changes and "evidence" not in candidate.proposed_changes:
                    ev = candidate.evidence.first()
                    combined_changes["evidence"] = ev.quote if ev else "Извлечено из чата"

                ver = base_version if base_version is not None else (candidate.base_project_version or 0)
                res = review(candidate_id, user, action, reason, combined_changes, ver)

                if res.status == "approved":
                    notify_on_candidate_approved(res)

                status_msg = "Факт успешно подтверждён." if res.status == "approved" else "Предложение отклонено."
                return json.dumps({
                    "success": True,
                    "action": action,
                    "candidate_id": res.id,
                    "status": res.status,
                    "message": status_msg,
                }, ensure_ascii=False)

        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    return mcp
