"""Authorized themes, including open thoughts outside the fact review inbox."""

from decimal import Decimal
from django.db.models import (
    Count,
    Q,
    Case,
    When,
    Value,
    IntegerField,
    Exists,
    OuterRef,
    Subquery,
)
from django.shortcuts import get_object_or_404
from django.db.models.functions import Coalesce
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.pagination import PageNumberPagination
from . import access
from .candidate_context import message_data
from .models import DialogueThread, FactCandidate, ThreadMessage, ThreadRevision, ThreadSubscription
from .messenger_api import decode_cursor, encode_cursor


def visible_threads(user):
    return (
        DialogueThread.objects.filter(
            message_links__raw_message__in=access.messages_for(user)
        )
        .exclude(state="superseded")
        .distinct()
    )


def parse_amount(val):
    if val is None or val == "":
        return Decimal(0)
    try:
        cleaned = str(val).replace(" ", "").replace("\xa0", "").replace(",", ".")
        return Decimal(cleaned)
    except (ValueError, TypeError, ArithmeticError):
        return Decimal(0)


def _detail_cursor(request, thread, kind, revision=None):
    token = request.query_params.get(f"{kind}_cursor")
    if not token:
        return None
    payload = decode_cursor(token) or {}
    if not isinstance(payload, dict):
        from rest_framework.exceptions import ValidationError
        raise ValidationError({"error": "Некорректный курсор темы."})
    expected_revision = revision.pk if revision else None
    if payload.get("thread") != thread.pk or payload.get("user") != user_id(request.user) or payload.get("kind") != kind or payload.get("revision") != expected_revision:
        from rest_framework.exceptions import ValidationError
        raise ValidationError({"error": "Курсор относится к другой теме или разделу."})
    return payload


def user_id(user):
    return user.pk


def _detail_next(request, thread, kind, marker, revision=None):
    return encode_cursor({"thread": thread.pk, "user": user_id(request.user), "kind": kind, "revision": revision.pk if revision else None, **marker})


def thread_data(thread, user, revision=None, request=None):
    limit = 50
    if request:
        try:
            limit = max(1, min(100, int(request.query_params.get("page_size", 50))))
        except (TypeError, ValueError):
            limit = 50
    has_project_access = (
        bool(thread.project_id)
        and access.projects_for(user).filter(pk=thread.project_id).exists()
    )
    project = thread.project if has_project_access else None
    project_id = project.id if project else None
    project_name = project.name if project else None
    company = project.company if project else None
    company_id = company.id if company else None
    company_name = company.name if company else None

    children_qs = (
        visible_threads(user)
        .filter(parent=thread)
        .order_by("id")
    )
    children_cursor = _detail_cursor(request, thread, "children", revision) if request else None
    if children_cursor:
        if type(children_cursor.get("id")) is not int or children_cursor["id"] <= 0:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"error": "Некорректный курсор поддиалогов."})
        children_qs = children_qs.filter(pk__gt=children_cursor["id"])
    children_rows = list(children_qs[:limit + 1] if request else children_qs)
    children_more = request is not None and len(children_rows) > limit
    if children_more:
        children_rows = children_rows[:limit]
    children = [
        {
            "id": child.id,
            "topic": child.topic,
            "state": child.state,
            "version": child.version,
            "updated_at": child.updated_at,
        }
        for child in children_rows
    ]
    children_next_cursor = _detail_next(request, thread, "children", {"id": children_rows[-1].id}, revision) if children_more and children_rows else None

    latest_revision = revision
    if request and latest_revision is None:
        latest_revision = thread.revisions.only("id", "state", "version").order_by("-version").first()
    if revision:
        candidates = FactCandidate.objects.filter(thread_revision=revision).order_by("id")
    elif request and latest_revision:
        candidates = FactCandidate.objects.filter(thread_revision=latest_revision).exclude(status="superseded").order_by("id")
    else:
        candidates = (
            FactCandidate.objects.filter(thread_revision__thread=thread)
            .exclude(status="superseded")
            .select_related("thread_revision")
            .order_by("id")
        )
        if not candidates.exists():
            latest_rev = thread.revisions.order_by("-version").first()
            if latest_rev:
                candidates = latest_rev.candidates.order_by("id")

    facts_cursor = _detail_cursor(request, thread, "facts", revision) if request else None
    facts_offset = 0
    if facts_cursor:
        if type(facts_cursor.get("id")) is int and facts_cursor["id"] > 0:
            candidates = candidates.filter(pk__gt=facts_cursor["id"])
        elif type(facts_cursor.get("offset")) is int and facts_cursor["offset"] >= 0:
            facts_offset = facts_cursor["offset"]
        else:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"error": "Некорректный курсор фактов."})
    candidate_page = list(candidates.select_related("thread_revision")[:limit + 1] if request else candidates.select_related("thread_revision"))
    facts_more = request is not None and len(candidate_page) > limit
    if facts_more:
        candidate_page = candidate_page[:limit]
    if candidate_page:
        facts = [
            {
                "id": c.id,
                "fact_type": c.fact_type,
                "status": c.status,
                "proposed_changes": c.proposed_changes,
                "in_progress": bool(
                    (
                        c.proposed_changes.get("in_progress", False)
                        if isinstance(c.proposed_changes, dict)
                        else False
                    )
                    or (c.thread_revision and c.thread_revision.state == "open")
                    or (revision.state == "open" if revision else thread.state == "open")
                ),
            }
            for c in candidate_page
        ]
    else:
        rev = latest_revision or revision or thread.revisions.order_by("-version").first()
        if request and latest_revision and (facts_cursor is None or "offset" in facts_cursor):
            latest_revision = ThreadRevision.objects.only("id", "state", "extraction", "version").get(pk=latest_revision.pk)
            rev = latest_revision
        if (not facts_cursor or "offset" in facts_cursor) and rev and rev.extraction:
            facts_offset = facts_cursor.get("offset", 0) if facts_cursor else 0
            extraction = [item for item in rev.extraction if isinstance(item, dict)]
            extraction_page = extraction[facts_offset:facts_offset + limit + 1] if request else extraction
            facts_more = request is not None and len(extraction_page) > limit
            if facts_more:
                extraction_page = extraction_page[:limit]
            facts = [
                {
                    "id": facts_offset + idx + 1,
                    "fact_type": item.get("fact_type", "commitment"),
                    "status": "pending",
                    "proposed_changes": item,
                    "in_progress": bool(
                        item.get("in_progress", False)
                        or (rev.state == "open")
                        or (thread.state == "open")
                    ),
                }
                for idx, item in enumerate(extraction_page)
            ]
        else:
            facts = []
    facts_next_cursor = None
    if facts_more and candidate_page:
        facts_next_cursor = _detail_next(request, thread, "facts", {"id": candidate_page[-1].pk}, revision)
    elif facts_more:
        facts_next_cursor = _detail_next(request, thread, "facts", {"offset": facts_offset + len(facts)}, revision)

    parent_id = (
        thread.parent_id
        if thread.parent_id and visible_threads(user).filter(pk=thread.parent_id).exists()
        else None
    )

    is_subscribed = ThreadSubscription.objects.filter(user=user, thread=thread).exists()
    chat_name = thread.config.name if thread.config else None
    counterparty = company_name
    if not counterparty:
        for f in facts:
            prop = f.get("proposed_changes")
            if isinstance(prop, dict):
                counterparty = (
                    prop.get("company_name")
                    or prop.get("counterparty")
                    or prop.get("party")
                )
                if counterparty:
                    break
    if not counterparty:
        counterparty = chat_name

    messages_cursor = _detail_cursor(request, thread, "messages", revision) if request else None
    snapshot = None
    if revision:
        snapshot = {link["raw_message_id"]: link for link in revision.message_snapshot}
        rows_qs = (
            access.messages_for(user)
            .filter(pk__in=snapshot)
            .select_related("config")
            .order_by("timestamp", "id")
        )
    else:
        rows_qs = None
    links_qs = (
        thread.message_links.filter(raw_message__in=access.messages_for(user))
        .select_related("raw_message__config", "routing_attempt")
        .order_by("raw_message__timestamp", "raw_message_id")
    )
    if not revision:
        rows_qs = access.messages_for(user).filter(pk__in=links_qs.values("raw_message_id")).select_related("config").order_by("timestamp", "id")
    if messages_cursor:
        from django.db.models import Q
        from django.utils.dateparse import parse_datetime
        marker_time = parse_datetime(messages_cursor.get("t", ""))
        marker_id = messages_cursor.get("i")
        if marker_time is None or type(marker_id) is not int or marker_id <= 0:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"error": "Некорректный курсор сообщений."})
        rows_qs = rows_qs.filter(Q(timestamp__gt=marker_time) | Q(timestamp=marker_time, pk__gt=marker_id))
    rows = list(rows_qs[:limit + 1] if request else rows_qs)
    messages_more = request is not None and len(rows) > limit
    if messages_more:
        rows = rows[:limit]
    messages_next_cursor = _detail_next(request, thread, "messages", {"t": rows[-1].timestamp.isoformat(), "i": rows[-1].pk}, revision) if messages_more and rows else None
    if revision:
        return {
            **routing_data(thread),
            "id": thread.id,
            "topic": revision.topic or thread.topic,
            "summary": revision.summary,
            "state": revision.state,
            "version": revision.version,
            "revision_id": revision.pk,
            "parent_id": parent_id,
            "project_id": project_id,
            "project_name": project_name,
            "company_id": company_id,
            "company_name": company_name,
            "chat_name": chat_name,
            "counterparty": counterparty,
            "is_subscribed": is_subscribed,
            "children": children,
            "children_next_cursor": children_next_cursor,
            "facts": facts,
            "facts_next_cursor": facts_next_cursor,
            "messages": [
                {
                    **message_data(row, 0, set()),
                    **{
                        key: value
                        for key, value in snapshot[row.id].items()
                        if key != "raw_message_id"
                    },
                }
                for row in rows
            ],
            "messages_next_cursor": messages_next_cursor,
        }
    links = {link.raw_message_id: link for link in links_qs.filter(raw_message_id__in=[row.id for row in rows])}
    return {
        **routing_data(thread),
        "id": thread.id,
        "topic": thread.topic,
        "state": thread.state,
        "version": thread.version,
        "parent_id": parent_id,
        "project_id": project_id,
        "project_name": project_name,
        "company_id": company_id,
        "company_name": company_name,
        "chat_name": chat_name,
        "counterparty": counterparty,
        "is_subscribed": is_subscribed,
        "summary": thread.summary,
        "children": children,
        "children_next_cursor": children_next_cursor,
        "facts": facts,
        "facts_next_cursor": facts_next_cursor,
        "messages": [
            {
                **message_data(row, 0, set()),
                "thought_state": links[row.id].thought_state,
                "relation": links[row.id].relation,
                "rationale": links[row.id].rationale,
                "assignment_method": links[row.id].assignment_method,
                "routing_reason": links[row.id].routing_attempt.reason_code if links[row.id].routing_attempt_id else "",
                "routing_latency_ms": links[row.id].routing_attempt.latency_ms if links[row.id].routing_attempt_id else None,
            }
            for row in rows
        ],
        "messages_next_cursor": messages_next_cursor,
    }


def routing_data(thread):
    return {'routing_enabled':bool(thread.config_id and thread.config.thread_routing_enabled),"routing_state": thread.routing_state, "routing_descriptor_version": thread.routing_descriptor_version,
            "routing_generation": thread.routing_generation, "last_extracted_routing_generation": thread.last_extracted_routing_generation,
            "extraction_pending": thread.routing_generation > thread.last_extracted_routing_generation}


class ThreadFlushView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        from django.db import transaction
        from .dialogue_threads import lock_source
        from .processing_attempts import require_reanalysis
        from .thread_routing import schedule_extraction
        thread = get_object_or_404(visible_threads(request.user), pk=pk)
        raw = thread.message_links.select_related("raw_message__config__team").order_by("raw_message_id").first().raw_message
        require_reanalysis(request.user, raw)
        with transaction.atomic():
            lock_source(raw)
            thread = DialogueThread.objects.select_for_update().get(pk=pk)
            event = schedule_extraction(thread, flush=True)
        return Response({"event_id": event.id if event else None, "state": event.state if event else "complete"}, status=202 if event else 200)


class ThreadPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_paginated_response(self, data, stats=None):
        return Response(
            {
                "next": self.get_next_link(),
                "previous": self.get_previous_link(),
                "count": self.page.paginator.count,
                "results": data,
                "stats": stats or {},
            }
        )


class ThreadListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        base_qs = visible_threads(request.user)
        search = request.query_params.get("search", "").strip()[:255]
        if search:
            base_qs = base_qs.filter(
                Q(topic__icontains=search)
                | Q(project__name__icontains=search)
                | Q(project__company__name__icontains=search)
                | Q(summary__icontains=search)
            )

        company_id = request.query_params.get("company_id")
        if company_id:
            try:
                base_qs = base_qs.filter(project__company_id=int(company_id))
            except (ValueError, TypeError):
                pass

        project_id = request.query_params.get("project_id")
        if project_id:
            try:
                base_qs = base_qs.filter(project_id=int(project_id))
            except (ValueError, TypeError):
                pass

        parent_id = request.query_params.get("parent_id")
        if parent_id is not None and parent_id != "":
            if parent_id.lower() in ("null", "none", "root"):
                base_qs = base_qs.filter(parent__isnull=True)
            else:
                try:
                    base_qs = base_qs.filter(parent_id=int(parent_id))
                except (ValueError, TypeError):
                    pass

        stats = {
            "total": base_qs.count(),
            "open": base_qs.filter(state="open").count(),
            "ready": base_qs.filter(state="ready").count(),
            "commitments": FactCandidate.objects.filter(
                thread_revision__thread__in=base_qs, fact_type="commitment"
            )
            .exclude(status="superseded")
            .count(),
        }

        qs = base_qs
        state = request.query_params.get("state", "").strip()
        if state in ("open", "ready"):
            qs = qs.filter(state=state)

        has_candidate = Exists(
            FactCandidate.objects.filter(
                thread_revision__thread=OuterRef("pk")
            ).exclude(status="superseded")
        )

        child_counts = DialogueThread.objects.filter(parent_id=OuterRef("pk")).exclude(state="superseded").order_by().values("parent_id").annotate(total=Count("id")).values("total")[:1]
        message_counts = ThreadMessage.objects.filter(thread_id=OuterRef("pk")).order_by().values("thread_id").annotate(total=Count("id")).values("total")[:1]
        qs = (
            qs.select_related("project__company", "config")
            .annotate(
                children_count=Coalesce(Subquery(child_counts, output_field=IntegerField()), Value(0), output_field=IntegerField()),
                messages_count=Coalesce(Subquery(message_counts, output_field=IntegerField()), Value(0), output_field=IntegerField()),
                priority=Case(
                    When(state="ready", then=Value(2)),
                    When(has_candidate, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                ),
            )
            .order_by("-priority", "-updated_at", "-id")
        )

        pagination = ThreadPagination()
        page = pagination.paginate_queryset(qs, request)

        thread_ids = [item.id for item in page]
        user_subscribed_ids = set(
            ThreadSubscription.objects.filter(
                user=request.user, thread_id__in=thread_ids
            ).values_list("thread_id", flat=True)
        )
        latest_revisions = list(
            ThreadRevision.objects.filter(thread_id__in=thread_ids)
            .only("id", "thread_id", "version", "state")
            .order_by("thread_id", "-version")
            .distinct("thread_id")
        )
        revisions_by_thread = {}
        for rev in latest_revisions:
            revisions_by_thread.setdefault(rev.thread_id, rev)
        latest_revision_ids = [rev.id for rev in revisions_by_thread.values()]
        candidates = (
            FactCandidate.objects.filter(thread_revision_id__in=latest_revision_ids)
            .exclude(status="superseded")
            .select_related("thread_revision")
            .only("id", "thread_revision_id", "fact_type", "status", "proposed_changes", "thread_revision__state")
        )
        candidates_by_thread = {}
        for c in candidates:
            candidates_by_thread.setdefault(c.thread_revision.thread_id, []).append(c)
        fallback_revision_ids = [
            rev.id for thread_id, rev in revisions_by_thread.items()
            if thread_id not in candidates_by_thread
        ]
        for rev in ThreadRevision.objects.filter(pk__in=fallback_revision_ids).only("id", "thread_id", "state", "extraction"):
            revisions_by_thread[rev.thread_id] = rev

        page_project_ids = {item.project_id for item in page if item.project_id}
        accessible_project_ids = set(
            access.projects_for(request.user).filter(pk__in=page_project_ids).values_list("id", flat=True)
        )

        results = []
        for item in page:
            if item.project_id and item.project_id in accessible_project_ids:
                proj = item.project
                proj_id = proj.id
                proj_name = proj.name
                comp = proj.company
                comp_id = comp.id if comp else None
                comp_name = comp.name if comp else None
            else:
                proj_id = None
                proj_name = None
                comp_id = None
                comp_name = None

            item_candidates = candidates_by_thread.get(item.id, [])
            total_amount = Decimal(0)
            comp_from_facts = None
            if item_candidates:
                commitments = [c for c in item_candidates if c.fact_type == "commitment"]
                commitments_count = len(commitments) if commitments else len(item_candidates)
                for c in item_candidates:
                    if isinstance(c.proposed_changes, dict):
                        val = (
                            c.proposed_changes.get("amount")
                            or c.proposed_changes.get("contract_amount")
                            or c.proposed_changes.get("payment_amount")
                            or c.proposed_changes.get("total_amount")
                            or c.proposed_changes.get("sum")
                        )
                        total_amount += parse_amount(val)
                        if not comp_from_facts:
                            comp_from_facts = (
                                c.proposed_changes.get("company_name")
                                or c.proposed_changes.get("counterparty")
                                or c.proposed_changes.get("party")
                            )
            elif item.id in revisions_by_thread:
                ext = revisions_by_thread[item.id].extraction or []
                commitments = [
                    f for f in ext if isinstance(f, dict) and f.get("fact_type") == "commitment"
                ]
                commitments_count = len(commitments) if commitments else len(ext)
                for f in ext:
                    if isinstance(f, dict):
                        val = (
                            f.get("amount")
                            or f.get("contract_amount")
                            or f.get("payment_amount")
                            or f.get("total_amount")
                            or f.get("sum")
                        )
                        total_amount += parse_amount(val)
                        if not comp_from_facts:
                            comp_from_facts = (
                                f.get("company_name")
                                or f.get("counterparty")
                                or f.get("party")
                            )
            else:
                commitments_count = 0

            amt_float = float(total_amount)
            total_amount_val = int(amt_float) if amt_float.is_integer() else round(amt_float, 2)
            chat_name = item.config.name if item.config else None
            counterparty = comp_name or comp_from_facts or chat_name

            results.append(
                {
                    "id": item.id,
                    "topic": item.topic,
                    "state": item.state,
                    "version": item.version,
                    "parent_id": item.parent_id,
                    "project_id": proj_id,
                    "project_name": proj_name,
                    "company_id": comp_id,
                    "company_name": comp_name,
                    "chat_name": chat_name,
                    "counterparty": counterparty,
                    "is_subscribed": item.id in user_subscribed_ids,
                    "summary": item.summary,
                    "updated_at": item.updated_at,
                    "children_count": item.children_count,
                    "messages_count": item.messages_count,
                    "commitments_count": commitments_count,
                    "total_amount": total_amount_val,
                    **routing_data(item),
                }
            )

        return pagination.get_paginated_response(results, stats=stats)


class ThreadSubscribeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        thread = get_object_or_404(visible_threads(request.user), pk=pk)
        sub = ThreadSubscription.objects.filter(user=request.user, thread=thread).first()
        if sub:
            sub.delete()
            subscribed = False
        else:
            ThreadSubscription.objects.create(user=request.user, thread=thread)
            subscribed = True
        return Response({"subscribed": subscribed, "thread_id": thread.id})


class ThreadDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        thread = get_object_or_404(visible_threads(request.user).select_related("project__company", "config"), pk=pk)
        revision = None
        revision_id = request.query_params.get("revision_id")
        if not revision_id:
            for cursor_name in ("messages_cursor", "facts_cursor", "children_cursor"):
                payload = decode_cursor(request.query_params.get(cursor_name)) or {}
                if payload.get("revision"):
                    revision_id = str(payload["revision"])
                    break
        if revision_id:
            from rest_framework.exceptions import ValidationError
            if not revision_id.isascii() or not revision_id.isdigit() or int(revision_id) <= 0:
                raise ValidationError({"error": "revision_id должен быть положительным числовым ID."})
            revision = get_object_or_404(ThreadRevision.objects.filter(thread=thread), pk=int(revision_id))
        return Response(thread_data(thread, request.user, revision=revision, request=request))


class BackfillInput(serializers.Serializer):
    config_id = serializers.IntegerField(min_value=1)
    request_key = serializers.CharField(max_length=64)


class ThreadBackfillView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .thread_backfill import enqueue_backfill

        schema = BackfillInput(data=request.data)
        schema.is_valid(raise_exception=True)
        values = schema.validated_data
        get_object_or_404(access.configs_for(request.user), pk=values["config_id"])
        event = enqueue_backfill(
            request.user, values["config_id"], values["request_key"]
        )
        return Response({"event_id": event.id, "state": event.state}, status=202)
