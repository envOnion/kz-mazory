"""Authorized themes, including open thoughts outside the fact review inbox."""

from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.pagination import PageNumberPagination
from . import access
from .candidate_context import message_data
from .models import DialogueThread


def visible_threads(user):
    return (
        DialogueThread.objects.filter(
            message_links__raw_message__in=access.messages_for(user)
        )
        .exclude(state="superseded")
        .distinct()
    )


def thread_data(thread, user, revision=None):
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
            "parent_id": None,
            "project_id": thread.project_id
            if access.projects_for(user).filter(pk=thread.project_id).exists()
            else None,
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
        "version": revision.version if revision else thread.version,
        "parent_id": thread.parent_id
        if visible_threads(user).filter(pk=thread.parent_id).exists()
        else None,
        "project_id": thread.project_id
        if access.projects_for(user).filter(pk=thread.project_id).exists()
        else None,
        "summary": thread.summary,
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


class ThreadListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = visible_threads(request.user).order_by("-updated_at", "-id")
        search = request.query_params.get("search", "")[:255]
        if search:
            qs = qs.filter(topic__icontains=search)
        pagination = PageNumberPagination()
        page = pagination.paginate_queryset(qs, request)
        return pagination.get_paginated_response(
            [
                {
                    "id": item.id,
                    "topic": item.topic,
                    "state": item.state,
                    "version": item.version,
                    "updated_at": item.updated_at,
                }
                for item in page
            ]
        )


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
