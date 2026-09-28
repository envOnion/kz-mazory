from datetime import timedelta
from decimal import Decimal
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient
from api.models import (
    Team,
    TeamMembership,
    UserProfile,
    Project,
    ProjectRevision,
    WhatsAppConfig,
    ChatAccess,
    RawMessage,
    MessageProcessingTrace,
    FactCandidate,
    FactEvidence,
)
from api.authentication import create_session
from api.facts import snapshot


def setup_case(case):
    now = timezone.now()
    case.team = Team.objects.create(
        name="Team A", history_complete_from=(now - timedelta(days=1000)).date()
    )
    case.other_team = Team.objects.create(name="Team B")
    case.lead = User.objects.create_user(
        "77000000001", password="test-only-password", is_staff=True, is_superuser=True
    )
    case.manager = User.objects.create_user("77000000002")
    case.other = User.objects.create_user("77000000003")
    case.finance = User.objects.create_user("77000000004")
    for user, team, role in (
        (case.lead, case.team, "team_lead"),
        (case.manager, case.team, "manager"),
        (case.other, case.other_team, "manager"),
        (case.finance, case.team, "finance"),
    ):
        TeamMembership.objects.create(user=user, team=team, role=role, status="active")
        UserProfile.objects.create(
            user=user,
            full_name=user.username,
            phone=user.username,
            notification_preferences={"quiet_start": "00:00", "quiet_end": "00:00"},
        )
    case.project = Project.objects.create(
        name="Alpha",
        team=case.team,
        manager=case.manager.profile,
        contract_amount=Decimal("1000"),
        cost_amount=Decimal("700"),
        cost_confirmed=True,
        is_verified=True,
        version=1,
    )
    case.foreign = Project.objects.create(
        name="Private B",
        team=case.other_team,
        manager=case.other.profile,
        contract_amount=Decimal("2000"),
        is_verified=True,
        version=1,
    )
    for p in (case.project, case.foreign):
        ProjectRevision.objects.create(
            project=p, version=p.version, snapshot=snapshot(p), approved_by=case.lead
        )
    case.config = WhatsAppConfig.objects.create(team=case.team, group_jid="test@g.us")
    for user in (case.manager, case.finance):
        ChatAccess.objects.create(user=user, config=case.config)
    case.client = client_for(case.lead)


def client_for(user):
    client = APIClient()
    session, token, refresh = create_session(user)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    client.test_session = session
    client.test_access = token
    client.test_refresh = refresh
    return client


def candidate_for(case, fact_type="payment", data=None, key="one"):
    raw = RawMessage.objects.create(
        config=case.config,
        team=case.team,
        message_id=key,
        content="Оплата 100 KZT Alpha",
        timestamp=timezone.now(),
    )
    trace = MessageProcessingTrace.objects.create(
        raw_message=raw, whatsapp_message_id=key, whatsapp_content=raw.content
    )
    payload = {
        "fact_type": fact_type,
        "object_name": case.project.name,
        "amount": "100.00",
        "currency": "KZT",
        "payment_date": timezone.localdate().isoformat(),
        "payment_kind": "increment",
        "evidence": raw.content,
        "commitment_text": "Подготовить акт",
        **(data or {}),
    }
    c = FactCandidate.objects.create(
        trace=trace,
        project=case.project,
        team=case.team,
        manager=case.manager.profile,
        fact_type=fact_type,
        proposed_changes=payload,
        base_project_version=case.project.version,
        source_key=key,
    )
    FactEvidence.objects.create(
        candidate=c, raw_message=raw, quote=raw.content, field_name="source"
    )
    return c
