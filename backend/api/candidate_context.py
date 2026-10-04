"""Authorized conversation and the exact saved AI history for fact review."""

import json

from django.db.models import (
    Q,
    Case,
    When,
    F,
    DateTimeField,
    Exists,
    OuterRef,
    IntegerField,
)
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import access


class ContextInput(serializers.Serializer):
    anchor = serializers.IntegerField(min_value=1, required=False)
    direction = serializers.ChoiceField(
        choices=["around", "before", "after"], default="around"
    )
    ai_page = serializers.IntegerField(min_value=1, default=1)


def message_data(raw, target_id, ai_ids, target_message_id=None):
    from .message_time import source_metadata
    original = source_metadata(raw)
    return {
        "source_metadata": original,
        "id": raw.id,
        "sender_name": original["sender"] or "Автор не указан",
        "sent_at": original["sent_at"],
        "received_at": raw.received_at,
        "content": raw.content,
        "is_source": raw.id == target_id,
        "used_by_ai": raw.id in ai_ids,
        "is_source_revision": raw.id != target_id
        and raw.message_id == target_message_id,
    }


class CandidateContextView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        candidate = get_object_or_404(
            access.candidates_for(request.user).select_related(
                "trace__raw_message__config"
            ),
            pk=pk,
        )
        schema = ContextInput(data=request.query_params)
        schema.is_valid(raise_exception=True)
        params = schema.validated_data
        trace = candidate.trace
        visible = access.messages_for(request.user)
        source = visible.filter(pk=trace.raw_message_id).first()
        # Never reveal snapshot contents or identifiers when the source is inaccessible.
        if source is None:
            return Response(
                {
                    "source": None,
                    "messages": [],
                    "before": None,
                    "after": None,
                    "ai_messages": [],
                    "ai_next_page": None,
                    "coverage": "Источник недоступен. Обратитесь к руководителю за доступом.",
                    "chat_name": "",
                    "history_available": False,
                }
            )
        snapshots = list(trace.earlier_messages_context) if isinstance(trace.earlier_messages_context, list) else []
        for batch in trace.context_metadata.get("commitment_resolution_batches", []):
            try:
                payload = batch["request"]
                request_message = next(item for item in payload["messages"] if item["role"] == "user")
                snapshots.extend(json.loads(request_message["content"])["context"])
            except (KeyError, TypeError, ValueError, StopIteration):
                continue
        snapshots = snapshots if isinstance(snapshots, list) else []
        snapshots = [
            m
            for m in snapshots
            if isinstance(m, dict) and type(m.get("raw_message_id")) is int
        ]
        allowed = set(
            visible.filter(pk__in=[m["raw_message_id"] for m in snapshots]).values_list(
                "id", flat=True
            )
        )
        ai_items = [m for m in snapshots if m["raw_message_id"] in allowed]
        ai_ids = allowed
        ai_page = params["ai_page"]
        ai_batch = ai_items[(ai_page - 1) * 25 : ai_page * 25]
        ai_messages = []
        ai_raws = {
            m.id: m
            for m in visible.filter(pk__in=[m["raw_message_id"] for m in ai_batch])
        }
        for item in ai_batch:
            raw = ai_raws[item["raw_message_id"]]
            data = message_data(raw, source.id, ai_ids, source.message_id)
            # Saved content can be partial; a current full message is not the AI snapshot.
            data["content"] = (
                item.get("content", "") if isinstance(item.get("content"), str) else ""
            )
            data["partial"] = bool(item.get("partial"))
            if isinstance(item.get("sender_name"), str):
                data["sender_name"] = item["sender_name"] or "Автор не указан"
            if "timestamp" in item:
                data["sent_at"] = item["timestamp"]
            ai_messages.append(data)
        metadata = (
            trace.context_metadata if isinstance(trace.context_metadata, dict) else {}
        )
        if not snapshots:
            coverage = "Сохранённой истории для этого анализа нет. Ниже можно посмотреть окружающую переписку."
        elif metadata.get("commitment_resolution_batches"):
            coverage = "Основной анализ получил ближайшую переписку; оставшаяся история проверена последовательными частями для поиска выполнения обязательства."
        elif metadata.get("history_complete_in_request") is True:
            coverage = (
                "В анализ вошла вся история, доступная системе на момент обработки."
            )
        elif metadata.get("history_complete_in_request") is False:
            coverage = "AI получил часть истории: более ранние сообщения или часть текста не поместились."
        else:
            coverage = "Сохранены сообщения, использованные AI. Полнота истории для этой попытки неизвестна."
        if len(ai_items) != len(snapshots):
            coverage += " Часть источников сейчас недоступна и скрыта."
        history_available = bool(source.chat_id and source.config_id)
        scope = (
            visible.filter(
                config_id=source.config_id,
                chat_id=source.chat_id,
                session_name=source.session_name,
                source=source.source,
            )
            if history_available
            else visible.filter(pk=source.id)
        )
        # Keep one current revision per neighboring message; preserve the exact source revision.
        newer = scope.filter(message_id=OuterRef("message_id")).filter(
            Q(received_at__gt=OuterRef("received_at"))
            | Q(received_at=OuterRef("received_at"), id__gt=OuterRef("id"))
        )
        scope = scope.annotate(has_newer=Exists(newer)).filter(
            Q(has_newer=False) | Q(pk=source.id)
        )
        scope = scope.exclude(
            Q(processing_state__in=["deleted", "superseded"]) & ~Q(pk=source.id)
        )
        scope = scope.annotate(
            order_time=Case(
                When(sent_at_known=True, then=F("timestamp")),
                default=F("received_at"),
                output_field=DateTimeField(),
            )
        )
        anchor = get_object_or_404(scope, pk=params.get("anchor", source.id))
        before_q = Q(order_time__lt=anchor.order_time) | Q(
            order_time=anchor.order_time, id__lt=anchor.id
        )
        after_q = Q(order_time__gt=anchor.order_time) | Q(
            order_time=anchor.order_time, id__gt=anchor.id
        )
        direction = params["direction"]
        if direction == "before":
            messages = list(
                reversed(
                    list(scope.filter(before_q).order_by("-order_time", "-id")[:25])
                )
            )
        elif direction == "after":
            messages = list(scope.filter(after_q).order_by("order_time", "id")[:25])
        else:
            earlier = list(scope.filter(before_q).order_by("-order_time", "-id")[:12])
            later = list(scope.filter(after_q).order_by("order_time", "id")[:12])
            messages = list(reversed(earlier)) + [anchor] + later
        first, last = (messages[0], messages[-1]) if messages else (anchor, anchor)
        more_before = scope.filter(
            Q(order_time__lt=first.order_time)
            | Q(order_time=first.order_time, id__lt=first.id)
        ).exists()
        more_after = scope.filter(
            Q(order_time__gt=last.order_time)
            | Q(order_time=last.order_time, id__gt=last.id)
        ).exists()
        thread = None
        if candidate.thread_revision_id:
            from .thread_views import thread_data, visible_threads
            revision = candidate.thread_revision
            if visible_threads(request.user).filter(pk=revision.thread_id).exists():
                thread = thread_data(revision.thread, request.user, revision)
        return Response(
            {
                "thread": thread,
                "source": message_data(source, source.id, ai_ids, source.message_id),
                "messages": [
                    message_data(m, source.id, ai_ids, source.message_id)
                    for m in messages
                ],
                "before": first.id if more_before else None,
                "after": last.id if more_after else None,
                "ai_messages": ai_messages,
                "ai_next_page": ai_page + 1 if ai_page * 25 < len(ai_items) else None,
                "coverage": coverage,
                "chat_name": source.config.name if source.config else "Источник факта",
                "history_available": history_available,
            }
        )


class CandidatePaymentsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from rest_framework.pagination import PageNumberPagination
        from .models import FinancialRecord

        candidate = get_object_or_404(access.candidates_for(request.user), pk=pk)
        access.require_team_role(request.user, candidate.team_id, ["finance"])
        project = get_object_or_404(
            access.projects_for(request.user), pk=candidate.project_id
        )
        qs = FinancialRecord.objects.filter(
            project=project, is_verified=True, status="received"
        ).order_by("-payment_date", "-id")
        selected_id = candidate.proposed_changes.get("reverses_id")
        if type(selected_id) is int:
            qs = qs.annotate(
                selection_order=Case(
                    When(pk=selected_id, then=0),
                    default=1,
                    output_field=IntegerField(),
                )
            ).order_by("selection_order", "-payment_date", "-id")
        pagination = PageNumberPagination()
        page = pagination.paginate_queryset(qs, request)
        return pagination.get_paginated_response(
            [
                {
                    "id": p.id,
                    "project_id": p.project_id,
                    "amount": str(p.amount),
                    "currency": p.currency,
                    "payment_date": p.payment_date,
                }
                for p in page
            ]
        )
