import datetime
from decimal import Decimal
import json
from pathlib import Path
from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone
from api.authentication import create_session
from api.ai_credentials import encrypt_credential
from api.models import (
    Team,
    TeamMembership,
    UserProfile,
    WhatsAppConfig,
    AISettings,
    BitrixSettings,
    SalesTarget,
)


def seed():
    if User.objects.filter(username='79990000001').exists():
        return
    team = Team.objects.create(
        name="Локальная команда",
        history_complete_from=datetime.date(2026, 1, 1),
    )
    user = User.objects.create_superuser("79990000001", password="local-e2e-only")
    UserProfile.objects.create(
        user=user, full_name="Проверяющий", phone="79990000001", bitrix_user_id="1"
    )
    TeamMembership.objects.create(
        user=user, team=team, role="team_lead", status="active"
    )
    manager_user = User.objects.create_user("79990000002", password="local-e2e-only")
    manager_profile = UserProfile.objects.create(
        user=manager_user,
        full_name="Боб Менеджер",
        phone="79990000002",
        bitrix_user_id="7",
    )
    TeamMembership.objects.create(
        user=manager_user, team=team, role="manager", status="active"
    )
    month = timezone.localdate().replace(day=1)
    SalesTarget.objects.create(
        team=team,
        profile=manager_profile,
        month=month,
        amount=Decimal("100000000.00"),
        currency="KZT",
        is_active=True,
    )
    config = WhatsAppConfig.objects.create(
        team=team,
        name="Локальная переписка",
        session_name="default",
        group_jid="fixture@g.us",
        snapshot={"timezone": "UTC+03:00"},
    )
    AISettings.objects.create(
        chat_model_name="local-fixture",
        chat_api_format="anthropic_messages",
        chat_provider_url="https://localhost",
        context_window_tokens=131072,
        max_completion_tokens=8192,
        context_safety_tokens=512,
        chat_api_key_encrypted=encrypt_credential(
            "fixture-key", purpose="chat", api_format="anthropic_messages"
        ),
    )
    BitrixSettings.objects.create(
        webhook_url="https://localhost/rest/1/fixture/",
        is_active=True,
        auto_import_deals=True,
        crm_matching_enabled=True,
    )
    _, access, refresh = create_session(user)
    data = {
        "access": access,
        "refresh": refresh,
        "cookie_name": settings.AUTH_REFRESH_COOKIE,
        "config_id": config.id,
    }
    target = Path(settings.E2E_DIR) / "session.json"
    target.write_text(json.dumps(data))
    target.chmod(0o644)
    seed_analytics()
    from .thread_history import seed as seed_thread_history
    seed_thread_history()


def seed_analytics():
    from api.models import Project, FinancialRecord, CrmProjectSnapshot, Commitment
    team = Team.objects.create(name='Аналитика E2E', history_complete_from=datetime.date(2026, 1, 1))
    user = User.objects.create_user('79990000100', password='local-e2e-only')
    profile = UserProfile.objects.create(user=user, full_name='Аналитик E2E', phone='79990000100')
    TeamMembership.objects.create(user=user, team=team, role='team_lead', status='active')
    projects = []
    for i, stage in enumerate(['Переговоры и согласование коммерческого предложения', 'Оплата по счетам', 'Переговоры и согласование коммерческого предложения']):
        p = Project.objects.create(name=f'Аналитический объект {i + 1}', team=team, manager=profile, identity_confirmed=True,
            is_verified=True, version=1, contract_known=True, contract_amount=Decimal('1000.00')*(i+1), currency='KZT')
        CrmProjectSnapshot.objects.create(project=p, external_stage_id=f'stage-{i % 2}', external_stage_name=stage,
            external_manager_id=f'manager-{i % 2}', external_manager_name='Алия' if i % 2 else 'Борис', opportunity=p.contract_amount, currency='KZT')
        projects.append(p)
    for i, (month, day, amount, project) in enumerate([(8, 2, '80.00', projects[0]), (8, 25, '20.00', projects[0]), (9, 2, '200.00', projects[1]), (9, 18, '-10.00', projects[1])]):
        FinancialRecord.objects.create(project=project, credited_profile=profile, payment_date=datetime.date(2026, month, day),
            amount=Decimal(amount), is_verified=True, status='received', currency='KZT', source_key=f'analytics-e2e-{i}')
    for month, amount in [(8, '150.00'), (9, '250.00')]:
        SalesTarget.objects.create(team=team, profile=profile, month=datetime.date(2026, month, 1), amount=Decimal(amount), currency='KZT', is_active=True)
    for index in range(50):
        Commitment.objects.create(team=team, project=projects[0], manager=profile,
            commitment_text=(f'Обещание {index}: ' + 'Подготовить документы по согласованному объекту. ' * 60)[:2000],
            deadline=datetime.date(2026, 1, 1), status='pending', is_verified=True)
    _, access_token, refresh = create_session(user)
    (Path(settings.E2E_DIR) / 'analytics-session.json').write_text(json.dumps({'access': access_token, 'refresh': refresh, 'cookie_name': settings.AUTH_REFRESH_COOKIE, 'user_id': user.id}))
