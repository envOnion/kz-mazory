"""Shared authorization for API, background jobs and retrieval."""

import hashlib
import json
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from .models import (
    TeamMembership,
    ClientProjectAccess,
    Project,
    UserProfile,
    WhatsAppConfig,
    FactCandidate,
    Commitment,
    RawMessage,
)


def memberships(user):
    if not user or not user.is_authenticated or not user.is_active:
        return TeamMembership.objects.none()
    return TeamMembership.objects.filter(
        user=user, status="active", team__is_active=True
    )


def has_access(user, invited=False):
    if not user or not user.is_active:
        return False
    if user.is_superuser:
        return True
    statuses = ["active", "invited"] if invited else ["active"]
    return (
        TeamMembership.objects.filter(
            user=user, status__in=statuses, team__is_active=True
        )
        .filter(
            Q(invited_until__isnull=True)
            | Q(invited_until__gt=timezone.now())
            | Q(status="active")
        )
        .exists()
        or ClientProjectAccess.objects.filter(user=user, status__in=statuses).exists()
        or (user.is_staff and user.has_perm("api.change_whatsappconfig"))
    )


def is_client(user):
    return (
        not user.is_superuser
        and not memberships(user).exists()
        and ClientProjectAccess.objects.filter(user=user, status="active").exists()
    )


def team_ids(user, roles=None):
    qs = memberships(user)
    if roles is not None:
        qs = qs.filter(role__in=roles)
    return qs.values_list("team_id", flat=True)


def projects_for(user, include_client=False):
    qs = Project.objects.filter(archived=False)
    if not user or not user.is_authenticated or not user.is_active:
        return qs.none()
    if user.is_superuser:
        return qs
    condition = Q(team_id__in=team_ids(user, ["team_lead", "finance"]))
    condition |= Q(team_id__in=team_ids(user, ["manager"]), manager__user=user)
    if include_client:
        condition |= Q(
            pk__in=ClientProjectAccess.objects.filter(
                user=user, status="active"
            ).values("project_id")
        )
    return qs.filter(condition)


def profiles_for(user):
    if user.is_superuser:
        return UserProfile.objects.all()
    return UserProfile.objects.filter(
        Q(user=user, user__memberships__status="active")
        | Q(
            user__memberships__team_id__in=team_ids(user, ["team_lead", "finance"]),
            user__memberships__status="active",
        )
    ).distinct()


def configs_for(user):
    qs = WhatsAppConfig.objects.filter(is_active=True, team__is_active=True)
    if user.is_superuser:
        return qs
    return qs.filter(
        Q(team_id__in=team_ids(user, ["team_lead"]))
        | Q(
            team_id__in=team_ids(user),
            access_grants__user=user,
            access_grants__is_active=True,
        )
    ).distinct()


def messages_for(user):
    if user.is_superuser:
        return RawMessage.objects.all()
    return RawMessage.objects.filter(
        Q(config__in=configs_for(user))
        | Q(
            source__in=["bitrix", "attachment", "manual"],
            project__in=projects_for(user),
        )
        | Q(
            source="bitrix",
            project__isnull=True,
            team_id__in=team_ids(user, ["team_lead", "finance"]),
        )
    ).distinct()


def candidates_for(user):
    if user.is_superuser:
        return FactCandidate.objects.all()
    return FactCandidate.objects.filter(
        Q(team_id__in=team_ids(user, ["team_lead", "finance"]))
        | Q(team_id__in=team_ids(user, ["manager"]), manager__user=user)
    )


def commitments_for(user):
    if user.is_superuser:
        return Commitment.objects.all()
    condition = Q(project__in=projects_for(user))
    if memberships(user).exists():
        condition |= Q(project__isnull=True, manager__user=user)
    return Commitment.objects.filter(condition)


def require_review(user, candidate):
    financial = candidate.fact_type == "payment" or (
        candidate.fact_type == "project"
        and any(
            key in candidate.proposed_changes
            for key in ("contract_amount", "cost_amount")
        )
    )
    role = "finance" if financial else "team_lead"
    if (
        not user.is_superuser
        and not memberships(user).filter(team_id=candidate.team_id, role=role).exists()
    ):
        raise PermissionDenied("Нет права подтверждать этот тип данных.")


def require_team_role(user, team_id, roles):
    if (
        not user.is_superuser
        and not memberships(user).filter(team_id=team_id, role__in=roles).exists()
    ):
        raise PermissionDenied("Недостаточно прав.")


def integration_allowed(user):
    return bool(
        user.is_active and user.is_staff and user.has_perm("api.change_whatsappconfig")
    )


def fingerprint(user):
    data = {
        "active": user.is_active,
        "superuser": user.is_superuser,
        "memberships": list(
            memberships(user).order_by("id").values_list("team_id", "role")
        ),
        "clients": list(
            ClientProjectAccess.objects.filter(user=user, status="active")
            .order_by("project_id")
            .values_list("project_id", flat=True)
        ),
        "chats": list(configs_for(user).order_by("id").values_list("id", flat=True)),
        "projects": list(
            projects_for(user, include_client=True)
            .order_by("id")
            .values_list("id", "manager_id", "team_id")
        ),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
