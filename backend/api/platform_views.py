from datetime import timedelta
from decimal import Decimal
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Max, Count, Sum, Avg
from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from . import access
from .models import (
    FactCandidate,
    Project,
    Commitment,
    FinancialRecord,
    PaymentScheduleItem,
    SalesTarget,
    AuditEvent,
    Team,
    TeamMembership,
    ChatAccess,
    ClientProjectAccess,
    UserProfile,
    OutboxEvent,
    AsyncOperation,
    NotificationDelivery,
    WhatsAppConfig,
)
from .facts import review, allocate_payment, json_value
from .datamart import datamart, scoped_projects
from .notifications import change_commitment
from .security import Conflict
from .views import Filters, filters_for, create_operation


def candidate_data(candidate, user):
    source_ids = access.messages_for(user).values_list("id", flat=True)
    evidence = [
        {
            "id": e.id,
            "quote": e.quote,
            "source_id": e.raw_message_id,
            "source_url": f"/api/messages/{e.raw_message_id}/",
        }
        for e in candidate.evidence.filter(raw_message_id__in=source_ids)
    ]
    values = dict(candidate.proposed_changes)
    if not evidence:
        values.pop("evidence", None)
    return {
        "id": candidate.id,
        "project_id": candidate.project_id,
        "project_name": candidate.project.name if candidate.project else "",
        "team_id": candidate.team_id,
        "manager_id": candidate.manager_id,
        "fact_type": candidate.fact_type,
        "proposed_changes": values,
        "status": candidate.status,
        "base_version": candidate.base_project_version,
        "current_version": candidate.project.version if candidate.project else 0,
        "confidence": candidate.confidence,
        "uncertainties": candidate.uncertainties,
        "evidence": evidence,
        "review_reason": candidate.review_reason,
        "created_at": candidate.created_at,
    }


class CandidateListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        state = request.query_params.get("status", "pending")
        if state not in ("pending", "approved", "rejected", "superseded"):
            raise ValidationError("Неизвестный статус.")
        qs = (
            access.candidates_for(request.user)
            .filter(status=state)
            .select_related("project")
            .prefetch_related("evidence")
            .order_by("id")
        )
        pagination = PageNumberPagination()
        page = pagination.paginate_queryset(qs, request)
        return pagination.get_paginated_response(
            [candidate_data(c, request.user) for c in page]
        )


class ReviewInput(serializers.Serializer):
    action = serializers.ChoiceField(choices=["approve", "reject", "match", "rebase"])
    base_version = serializers.IntegerField(min_value=0)
    reason = serializers.CharField(max_length=2000, allow_blank=True, default="")
    changes = serializers.DictField(required=False, default=dict)
    project_id = serializers.IntegerField(min_value=1, required=False)


class CandidateReviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        schema = ReviewInput(data=request.data)
        schema.is_valid(raise_exception=True)
        values = schema.validated_data
        candidate = get_object_or_404(access.candidates_for(request.user), pk=pk)
        access.require_review(request.user, candidate)
        if values["action"] in ("match", "rebase"):
            if not values["reason"].strip():
                raise ValidationError(
                    "Укажите основание сопоставления или новой проверки."
                )
            with transaction.atomic():
                candidate = FactCandidate.objects.select_for_update(of=("self",)).get(
                    pk=candidate.id
                )
                if candidate.status != "pending":
                    raise Conflict()
                project = get_object_or_404(
                    access.projects_for(request.user).select_for_update(of=("self",)),
                    pk=values.get("project_id", candidate.project_id),
                    team=candidate.team,
                )
                if values["base_version"] != project.version:
                    raise Conflict()
                candidate.project, candidate.base_project_version = (
                    project,
                    project.version,
                )
                candidate.save(update_fields=["project", "base_project_version"])
                AuditEvent.objects.create(
                    actor=request.user,
                    target_type="FactCandidate",
                    target_id=candidate.id,
                    action=values["action"],
                    before_after={
                        "project_id": project.id,
                        "base_version": project.version,
                        "reason": values["reason"],
                    },
                )
        else:
            if (
                values["action"] == "approve"
                and candidate.trace.raw_message_id
                and not access.messages_for(request.user)
                .filter(pk=candidate.trace.raw_message_id)
                .exists()
            ):
                raise PermissionDenied(
                    "Для подтверждения необходим доступ к источнику."
                )
            candidate = review(
                pk,
                request.user,
                values["action"],
                values["reason"],
                values["changes"],
                values["base_version"],
            )
        return Response(candidate_data(candidate, request.user))


class CommitmentListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            datamart.get_commitments_sla_mart(
                request.user, filters_for(request)["period"]
            )
        )


class CommitmentInput(serializers.Serializer):
    version = serializers.IntegerField(min_value=1)
    action = serializers.ChoiceField(choices=["fulfill", "postpone", "help"])
    reason = serializers.CharField(max_length=2000, allow_blank=True, default="")
    deadline_at = serializers.DateTimeField(required=False)


class CommitmentActionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        get_object_or_404(access.commitments_for(request.user), pk=pk)
        schema = CommitmentInput(data=request.data)
        schema.is_valid(raise_exception=True)
        values = schema.validated_data
        c = change_commitment(
            request.user,
            pk,
            values["version"],
            values["action"],
            values["reason"],
            values.get("deadline_at"),
        )
        return Response(
            {
                "id": c.id,
                "status": c.status,
                "version": c.version,
                "deadline_at": c.deadline_at,
            }
        )


class FinanceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        filters = filters_for(request)
        qs, currency = scoped_projects(request.user, filters)
        return Response(
            {
                "receivables": datamart.receivables(request.user, filters),
                "payments": list(
                    FinancialRecord.objects.filter(project__in=qs, is_verified=True)
                    .order_by("-payment_date", "-id")[:500]
                    .values(
                        "id",
                        "project_id",
                        "amount",
                        "currency",
                        "payment_date",
                        "candidate_id",
                        "reverses_id",
                        "credited_profile_id",
                    )
                ),
                "targets": list(
                    SalesTarget.objects.filter(
                        profile__in=access.profiles_for(request.user),
                        team_id__in=qs.values("team_id"),
                        is_active=True,
                    ).values(
                        "id",
                        "team_id",
                        "profile_id",
                        "month",
                        "amount",
                        "currency",
                        "version",
                    )
                ),
            }
        )


class ScheduleInput(serializers.Serializer):
    project_id = serializers.IntegerField(min_value=1)
    due_date = serializers.DateField()
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal(".01")
    )


class PaymentScheduleView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        schema = ScheduleInput(data=request.data)
        schema.is_valid(raise_exception=True)
        values = schema.validated_data
        project = get_object_or_404(
            access.projects_for(request.user), pk=values["project_id"], is_verified=True
        )
        access.require_team_role(request.user, project.team_id, ["finance"])
        with transaction.atomic():
            Project.objects.select_for_update(of=("self",)).get(pk=project.id)
            item = PaymentScheduleItem.objects.create(
                project=project,
                amount=values["amount"],
                currency=project.currency,
                due_date=values["due_date"],
                is_verified=True,
            )
            AuditEvent.objects.create(
                actor=request.user,
                target_type="PaymentScheduleItem",
                target_id=item.id,
                action="create",
                before_after=json_value(values),
            )
        return Response({"id": item.id}, status=201)


class AllocationInput(serializers.Serializer):
    payment_id = serializers.IntegerField(min_value=1)
    schedule_id = serializers.IntegerField(min_value=1)
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal(".01")
    )


class AllocationView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        schema = AllocationInput(data=request.data)
        schema.is_valid(raise_exception=True)
        values = schema.validated_data
        get_object_or_404(
            FinancialRecord,
            pk=values["payment_id"],
            project__in=access.projects_for(request.user),
        )
        item = allocate_payment(
            request.user, values["payment_id"], values["schedule_id"], values["amount"]
        )
        return Response({"id": item.id, "amount": str(item.amount)}, status=201)


class TargetInput(serializers.Serializer):
    profile_id = serializers.IntegerField(min_value=1)
    team_id = serializers.IntegerField(min_value=1)
    month = serializers.DateField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    currency = serializers.ChoiceField(choices=["KZT", "USD", "EUR", "RUB"])
    base_version = serializers.IntegerField(min_value=0)


class TargetView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        schema = TargetInput(data=request.data)
        schema.is_valid(raise_exception=True)
        values = schema.validated_data
        access.require_team_role(
            request.user, values["team_id"], ["finance", "team_lead"]
        )
        if values["month"].day != 1:
            raise ValidationError("Укажите первый день месяца.")
        with transaction.atomic():
            profile = get_object_or_404(
                UserProfile.objects.select_for_update(of=("self",)),
                pk=values["profile_id"],
                user_id__in=TeamMembership.objects.filter(
                    team_id=values["team_id"], status="active"
                ).values("user_id"),
            )
            old = (
                SalesTarget.objects.filter(
                    team_id=values["team_id"],
                    profile=profile,
                    month=values["month"],
                    currency=values["currency"],
                )
                .order_by("-version")
                .first()
            )
            version = old.version if old else 0
            if values["base_version"] != version:
                raise Conflict()
            SalesTarget.objects.filter(
                team_id=values["team_id"],
                profile=profile,
                month=values["month"],
                currency=values["currency"],
                is_active=True,
            ).update(is_active=False)
            item = SalesTarget.objects.create(
                team_id=values["team_id"],
                profile=profile,
                month=values["month"],
                amount=values["amount"],
                currency=values["currency"],
                version=version + 1,
                approved_by=request.user,
            )
            AuditEvent.objects.create(
                actor=request.user,
                target_type="SalesTarget",
                target_id=item.id,
                action="approve",
                before_after=json_value(values),
            )
        return Response({"id": item.id, "version": item.version}, status=201)


class ProjectHistoryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        p = get_object_or_404(access.projects_for(request.user), pk=pk)
        return Response(
            {
                "revisions": list(
                    p.revisions.order_by("-version").values(
                        "version", "snapshot", "approved_at", "approved_by_id", "source"
                    )
                ),
                "events": list(
                    AuditEvent.objects.filter(
                        target_type="Project", target_id=pk
                    ).values("id", "action", "created_at", "before_after")[:100]
                ),
            }
        )


class ExportInput(Filters):
    idempotency_key = serializers.CharField(max_length=64)


class ExportView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if access.is_client(request.user):
            raise PermissionDenied()
        schema = ExportInput(data=request.data)
        schema.is_valid(raise_exception=True)
        op = create_operation(request.user, schema.validated_data, "export")
        return Response({"operation_id": op.id, "status": op.status}, status=202)


class DirectoryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        teams = Team.objects.filter(is_active=True)
        if not request.user.is_superuser:
            teams = teams.filter(pk__in=access.team_ids(request.user))
        return Response(
            {
                "teams": list(teams.values("id", "name", "history_complete_from")),
                "profiles": list(
                    access.profiles_for(request.user).values(
                        "id", "user_id", "full_name"
                    )
                ),
                "projects": list(
                    access.projects_for(request.user)
                    .filter(is_verified=True)
                    .values("id", "name", "version", "currency", "team_id")[:500]
                ),
            }
        )


class AccessAdminView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.is_superuser:
            raise PermissionDenied()
        return Response(
            {
                "memberships": list(
                    TeamMembership.objects.values(
                        "id", "user_id", "user__username", "team_id", "role", "status"
                    )
                ),
                "clients": list(
                    ClientProjectAccess.objects.values(
                        "id", "user_id", "project_id", "status"
                    )
                ),
                "teams": list(Team.objects.values("id", "name")),
            }
        )

    def post(self, request):
        if not request.user.is_superuser:
            raise PermissionDenied()
        schema = AccessInput(data=request.data)
        schema.is_valid(raise_exception=True)
        values = schema.validated_data
        with transaction.atomic():
            user, _ = User.objects.get_or_create(username=values["phone"])
            if not user.has_usable_password():
                user.set_unusable_password()
                user.save(update_fields=["password"])
            UserProfile.objects.get_or_create(
                user=user,
                defaults={"phone": values["phone"], "full_name": values["name"]},
            )
            team = get_object_or_404(Team, pk=values["team_id"], is_active=True)
            grant, _ = TeamMembership.objects.update_or_create(
                user=user,
                team=team,
                role=values["role"],
                defaults={
                    "status": values["status"],
                    "invited_until": timezone.now() + timedelta(days=7),
                },
            )
            AuditEvent.objects.create(
                actor=request.user,
                target_type="TeamMembership",
                target_id=grant.id,
                action="access_change",
                before_after={"status": grant.status, "role": grant.role},
            )
        return Response({"id": grant.id, "user_id": user.id, "status": grant.status})


class AccessInput(serializers.Serializer):
    phone = serializers.RegexField(r"^[1-9][0-9]{9,14}$")
    name = serializers.CharField(max_length=255)
    team_id = serializers.IntegerField(min_value=1)
    role = serializers.ChoiceField(choices=["manager", "team_lead", "finance"])
    status = serializers.ChoiceField(choices=["invited", "revoked"])


class OperationsHealthView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not access.integration_allowed(request.user):
            raise PermissionDenied()
        oldest = (
            OutboxEvent.objects.filter(state__in=["pending", "enqueued"])
            .order_by("created_at")
            .first()
        )
        from .models import ProviderUsage, FactCandidate

        usage = ProviderUsage.objects.filter(
            created_at__gte=timezone.now() - timedelta(days=1)
        )
        stats = usage.aggregate(
            requests=Count("id"),
            cost_usd=Sum("cost_usd"),
            mean_duration_ms=Avg("duration_ms"),
        )
        stats["unknown_cost_requests"] = usage.filter(cost_usd__isnull=True).count()
        stats["failed_requests"] = usage.filter(succeeded=False).count()
        return Response(
            {
                "ai_last_24h": stats,
                "review": list(
                    FactCandidate.objects.values("fact_type", "status").annotate(
                        count=Count("id")
                    )
                ),
                "outbox": list(
                    OutboxEvent.objects.values("event_type", "state").annotate(
                        count=Count("id")
                    )
                ),
                "oldest_pending_seconds": (
                    timezone.now() - oldest.created_at
                ).total_seconds()
                if oldest
                else 0,
                "operations": list(
                    AsyncOperation.objects.values("status").annotate(count=Count("id"))
                ),
                "deliveries": list(
                    NotificationDelivery.objects.values("state").annotate(
                        count=Count("id")
                    )
                ),
                "errors": list(
                    OutboxEvent.objects.filter(state__in=["failed", "unknown"])
                    .order_by("-id")
                    .values("id", "event_type", "state", "error_code", "attempt_count")[:50]
                ),
            }
        )


class PaymentListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .datamart import period_bounds

        filters = filters_for(request)
        projects, currency = scoped_projects(request.user, filters)
        profile = UserProfile.objects.filter(user=request.user).first()
        start, end, today = period_bounds(
            filters["period"], profile.timezone if profile else "Asia/Almaty"
        )
        qs = FinancialRecord.objects.filter(
            project__in=projects,
            is_verified=True,
            status="received",
            currency=currency,
            payment_date__gte=start,
            payment_date__lt=min(end, today + timedelta(days=1)),
        ).order_by("payment_date", "id")
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
                    "candidate_id": p.candidate_id,
                    "reverses_id": p.reverses_id,
                    "credited_profile_id": p.credited_profile_id,
                }
                for p in page
            ]
        )


class CandidateDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        candidate = get_object_or_404(access.candidates_for(request.user), pk=pk)
        return Response(candidate_data(candidate, request.user))


class AssignmentInput(serializers.Serializer):
    manager_id = serializers.IntegerField(min_value=1)
    base_version = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(max_length=2000, allow_blank=False)
    transfer_open_commitments = serializers.BooleanField(default=False)


class AssignmentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        from .facts import record_revision, snapshot
        from django.db.models import F

        schema = AssignmentInput(data=request.data)
        schema.is_valid(raise_exception=True)
        values = schema.validated_data
        with transaction.atomic():
            project = get_object_or_404(
                access.projects_for(request.user).select_for_update(of=("self",)),
                pk=pk,
                is_verified=True,
            )
            access.require_team_role(request.user, project.team_id, ["team_lead"])
            if project.version != values["base_version"]:
                raise Conflict()
            manager = get_object_or_404(
                UserProfile,
                pk=values["manager_id"],
                user_id__in=TeamMembership.objects.filter(
                    team=project.team, status="active", role="manager"
                ).values("user_id"),
            )
            before = snapshot(project)
            old_manager = project.manager
            project.manager = manager
            project.version += 1
            project.save()
            if values["transfer_open_commitments"]:
                for commitment in Commitment.objects.select_for_update().filter(
                    project=project,
                    manager=old_manager,
                    status__in=["pending", "overdue"],
                ):
                    commitment.manager = manager
                    commitment.version += 1
                    commitment.save(update_fields=["manager", "version"])
                    AuditEvent.objects.create(
                        actor=request.user,
                        target_type="Commitment",
                        target_id=commitment.id,
                        action="reassign",
                        before_after={
                            "manager_id": manager.id,
                            "reason": values["reason"],
                        },
                    )
            record_revision(project, request.user, project.status)
            AuditEvent.objects.create(
                actor=request.user,
                target_type="Project",
                target_id=project.id,
                action="reassign",
                before_after={
                    "before": before,
                    "after": snapshot(project),
                    "reason": values["reason"],
                },
            )
        return Response({"id": project.id, "version": project.version})


class ManualProposalInput(serializers.Serializer):
    project_id = serializers.IntegerField(min_value=1)
    team_id = serializers.IntegerField(min_value=1, required=False)
    reason = serializers.CharField(max_length=2000)
    changes = serializers.DictField()


class ManualProposalView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .models import RawMessage, MessageProcessingTrace, FactEvidence
        from .facts import FactSchema
        import uuid

        schema = ManualProposalInput(data=request.data)
        schema.is_valid(raise_exception=True)
        values = schema.validated_data
        with transaction.atomic():
            project = get_object_or_404(
                access.projects_for(request.user).select_for_update(of=("self",)),
                pk=values["project_id"],
            )
            if not project.team_id:
                if not request.user.is_superuser:
                    raise PermissionDenied()
                project.team = get_object_or_404(
                    Team, pk=values.get("team_id"), is_active=True
                )
                project.save(update_fields=["team"])
                AuditEvent.objects.create(
                    actor=request.user,
                    target_type="Project",
                    target_id=project.id,
                    action="legacy_team_assignment",
                    before_after={
                        "team_id": project.team_id,
                        "reason": values["reason"],
                    },
                )
            payload = {
                **values["changes"],
                "object_name": project.name,
                "currency": project.currency,
                "evidence": values["reason"],
            }
            validation = FactSchema(data=payload)
            validation.is_valid(raise_exception=True)
            payload = json_value(validation.validated_data)
            raw = RawMessage.objects.create(
                source="manual",
                session_name="review",
                message_id=uuid.uuid4().hex,
                project=project,
                team=project.team,
                content=values["reason"],
                sender_name=request.user.username,
                timestamp=timezone.now(),
                sent_at_known=True,
                processed=True,
                processing_state="needs_review",
            )
            trace = MessageProcessingTrace.objects.create(
                raw_message=raw,
                project=project,
                whatsapp_message_id=raw.message_id,
                whatsapp_content=raw.content,
                pipeline_action="proposed_facts",
                prompt_version="manual-v1",
                status="warning",
            )
            candidate = FactCandidate.objects.create(
                trace=trace,
                project=project,
                team=project.team,
                manager=project.manager,
                fact_type=payload["fact_type"],
                proposed_changes=payload,
                base_project_version=project.version,
                source_key=f"manual:{raw.id}",
                uncertainties=["Ручной ввод; проверьте основание и значения."],
            )
            FactEvidence.objects.create(
                candidate=candidate, raw_message=raw, quote=raw.content
            )
            AuditEvent.objects.create(
                actor=request.user,
                target_type="FactCandidate",
                target_id=candidate.id,
                action="propose_manual",
                before_after={"source_id": raw.id},
            )
        return Response(candidate_data(candidate, request.user), status=201)


class RetryInput(serializers.Serializer):
    reason = serializers.CharField(max_length=2000)
    provider_confirmed_not_delivered = serializers.BooleanField(default=False)


class RetryOutboxView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not access.integration_allowed(request.user):
            raise PermissionDenied()
        schema = RetryInput(data=request.data)
        schema.is_valid(raise_exception=True)
        values = schema.validated_data
        with transaction.atomic():
            event = get_object_or_404(
                OutboxEvent.objects.select_for_update(),
                pk=pk,
                state__in=["failed", "unknown"],
            )
            if event.event_type in ("otp", "waha_control"):
                raise ValidationError("Создайте новый запрос этой операции.")
            if event.event_type == "notification":
                if (
                    event.state == "unknown"
                    and not values["provider_confirmed_not_delivered"]
                ):
                    raise ValidationError(
                        "Проверьте отсутствие доставки у поставщика перед повтором."
                    )
                old = NotificationDelivery.objects.select_for_update().get(
                    pk=event.payload["delivery_id"]
                )
                attempt = (
                    old.notification.deliveries.aggregate(n=Max("attempt_no"))["n"] or 0
                ) + 1
                delivery = NotificationDelivery.objects.create(
                    notification=old.notification, attempt_no=attempt
                )
                new = OutboxEvent.objects.create(
                    event_type="notification",
                    deduplication_key=f"notification:{old.notification_id}:attempt:{attempt}",
                    payload={**event.payload, "delivery_id": delivery.id},
                )
                event.state = "cancelled"
                event.save(update_fields=["state"])
            else:
                event.state = "pending"
                event.next_attempt_at = timezone.now()
                event.lease_until = None
                event.save(update_fields=["state", "next_attempt_at", "lease_until"])
                new = event
            AuditEvent.objects.create(
                actor=request.user,
                target_type="OutboxEvent",
                target_id=event.id,
                action="manual_retry",
                before_after={
                    "reason": values["reason"],
                    "new_event_id": new.id,
                    "provider_confirmed_not_delivered": values[
                        "provider_confirmed_not_delivered"
                    ],
                },
            )
        return Response({"id": new.id, "state": new.state}, status=202)


class LegacyProjectsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.is_superuser:
            raise PermissionDenied()
        rows = Project.objects.filter(
            version=0, is_verified=False, archived=False
        ).order_by("id")
        pagination = PageNumberPagination()
        page = pagination.paginate_queryset(rows, request)
        return pagination.get_paginated_response(
            [
                {
                    "id": p.id,
                    "name": p.name,
                    "team_id": p.team_id,
                    "contract_amount": str(p.contract_amount),
                    "legacy_paid_amount": str(p.paid_amount),
                    "currency": p.currency,
                }
                for p in page
            ]
        )
