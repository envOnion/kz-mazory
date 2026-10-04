import json
from pathlib import Path
from django.conf import settings
from django.contrib.auth.models import User
from api.authentication import create_session
from api.ai_credentials import encrypt_credential
from api.models import (
    Team,
    TeamMembership,
    UserProfile,
    WhatsAppConfig,
    AISettings,
    BitrixSettings,
)


def seed():
    team = Team.objects.create(id=1, name="Локальная команда")
    user = User.objects.create_superuser("79990000001", password="local-e2e-only")
    UserProfile.objects.create(
        user=user, full_name="Проверяющий", phone="79990000001", bitrix_user_id="7"
    )
    TeamMembership.objects.create(
        user=user, team=team, role="team_lead", status="active"
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
        context_window_tokens=32768,
        max_completion_tokens=8192,
        context_safety_tokens=512,
        message_processing_paused=False,
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
    target.chmod(0o600)
