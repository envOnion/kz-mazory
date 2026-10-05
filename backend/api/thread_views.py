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
)
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.pagination import PageNumberPagination
from . import access
from .candidate_context import message_data
from .models import DialogueThread, FactCandidate, ThreadRevision, ThreadSubscription


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


def thread_data(thread, user, revision=None):
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
    children = [
        {
            "id": child.id,
            "topic": child.topic,
            "state": child.state,
            "version": child.version,
            "updated_at": child.updated_at,
        }
        for child in children_qs
    ]

    if revision:
        candidates = FactCandidate.objects.filter(thread_revision=revision).order_by("id")
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

    if candidates.exists():
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
            for c in candidates
        ]
    else:
        rev = revision or thread.revisions.order_by("-version").first()
        if rev and rev.extraction:
            facts = [
                {
                    "id": idx + 1,
                    "fact_type": item.get("fact_type", "commitment"),
                    "status": "pending",
                    "proposed_changes": item,
                    "in_progress": bool(
                        item.get("in_progress", False)
                        or (rev.state == "open")
                        or (thread.state == "open")
                    ),
                }
                for idx, item in enumerate(rev.extraction)
                if isinstance(item, dict)
            ]
        else:
            facts = []

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

    if revision:
        snapshot = {link["raw_message_id"]: link for link in revision.message_snapshot}
        rows = (
            access.messages_for(user)
            .filter(pk__in=snapshot)
            .select_related("config")
            .order_by("timestamp", "id")
        )
        return {
            "id": thread.id,
            "topic": revision.topic or thread.topic,
            "summary": revision.summary,
            "state": revision.state,
            "version": revision.version,
            "parent_id": parent_id,
            "project_id": project_id,
            "project_name": project_name,
            "company_id": company_id,
            "company_name": company_name,
            "chat_name": chat_name,
            "counterparty": counterparty,
            "is_subscribed": is_subscribed,
            "children": children,
            "facts": facts,
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
        }
    links = (
        thread.message_links.filter(raw_message__in=access.messages_for(user))
        .select_related("raw_message__config")
        .order_by("raw_message__timestamp", "raw_message_id")
    )
    return {
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
        "facts": facts,
        "messages": [
            {
                **message_data(link.raw_message, 0, set()),
                "thought_state": link.thought_state,
                "relation": link.relation,
                "rationale": link.rationale,
            }
            for link in links
        ],
    }


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

        qs = (
            qs.select_related("project__company", "config")
            .annotate(
                children_count=Count(
                    "children",
                    filter=~Q(children__state="superseded"),
                    distinct=True,
                ),
                messages_count=Count("message_links", distinct=True),
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
        candidates = (
            FactCandidate.objects.filter(
                thread_revision__thread_id__in=thread_ids
            )
            .exclude(status="superseded")
            .select_related("thread_revision")
        )
        candidates_by_thread = {}
        for c in candidates:
            candidates_by_thread.setdefault(c.thread_revision.thread_id, []).append(c)

        revisions_by_thread = {}
        missing_ids = [tid for tid in thread_ids if tid not in candidates_by_thread]
        if missing_ids:
            for rev in (
                ThreadRevision.objects.filter(thread_id__in=missing_ids)
                .order_by("thread_id", "-version")
            ):
                if rev.thread_id not in revisions_by_thread:
                    revisions_by_thread[rev.thread_id] = rev

        accessible_project_ids = set(
            access.projects_for(request.user).values_list("id", flat=True)
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
        thread = get_object_or_404(visible_threads(request.user), pk=pk)
        return Response(thread_data(thread, request.user))


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
