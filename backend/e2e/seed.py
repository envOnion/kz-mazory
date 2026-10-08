import os
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
        autonomous_daily_token_limit=0,
        chat_api_key_encrypted=encrypt_credential(
            "fixture-key", purpose="chat", api_format="anthropic_messages"
        ),
    )
    if os.getenv('MAZORY_E2E_REAL_MODEL') == '1':
        from api.context_tokens import GEMMA_MANIFEST
        cfg=AISettings.objects.first()
        cfg.chat_api_format='openai_compatible'
        cfg.chat_provider_url='https://localhost/v1'
        cfg.chat_model_name='gemma4:e4b'
        cfg.tokenizer_id=GEMMA_MANIFEST['repo']; cfg.tokenizer_revision=GEMMA_MANIFEST['revision']
        cfg.context_window_tokens=32768; cfg.max_completion_tokens=2048
        cfg.set_chat_api_key('synthetic-e2e-only'); cfg.save()
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
    from .temporal import seed as seed_temporal
    seed_temporal()
    from .thread_history import seed as seed_thread_history
    seed_thread_history()
    seed_avatars()


def seed_avatars():
    from PIL import Image, ImageDraw
    directory = Path(settings.E2E_DIR)
    team = Team.objects.create(name="Фото профиля E2E")
    for suffix, name in [("200", "Антон E2E"), ("201", "Другой пользователь")]:
        user = User.objects.create_user(f"79990000{suffix}")
        profile = UserProfile.objects.create(user=user, full_name=name, phone=user.username)
        TeamMembership.objects.create(user=user, team=team, role="team_lead", status="active")
        _, access_token, refresh = create_session(user)
        (directory / f"avatar-session-{suffix}.json").write_text(json.dumps({
            "access": access_token, "refresh": refresh,
            "cookie_name": settings.AUTH_REFRESH_COOKIE, "profile_id": profile.id,
        }))
    image = Image.new("RGB", (900, 600), "#172554")
    draw = ImageDraw.Draw(image)
    draw.ellipse((345, 90, 555, 300), fill="#a5b4fc")
    draw.rounded_rectangle((265, 330, 635, 680), radius=120, fill="#a5b4fc")
    image.save(directory / "avatar.png")
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = "private-avatar-metadata"
    image.save(directory / "avatar.jpg", exif=exif)
    image.save(directory / "avatar.webp")
    Image.effect_noise((2200, 1500), 64).save(directory / "avatar-large-valid.png")
    image.save(directory / "avatar.gif")
    image.save(directory / "avatar-animated.png", save_all=True,
               append_images=[Image.new("RGB", image.size, "green")], duration=100)
    Image.new("RGB", (5000, 4001), "blue").save(directory / "avatar-too-many-pixels.png")


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
