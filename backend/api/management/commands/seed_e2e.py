import os
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from api.models import (
    Team,
    TeamMembership,
    UserProfile,
    Project,
    ProjectRevision,
    SalesTarget,
    FinancialRecord,
    WhatsAppConfig,
    ChatAccess,
    AISettings,
    RawMessage,
    MessageProcessingTrace,
    Commitment,
    ClientProjectAccess,
)
from api.facts import snapshot


class Command(BaseCommand):
    help = (
        "Seed isolated E2E fixtures; refuses existing users or a production database."
    )

    def handle(self, *args, **options):
        if not settings.INTEGRATION_TEST_MODE or not str(
            settings.DATABASES["default"]["NAME"]
        ).endswith("_e2e"):
            raise CommandError(
                "Requires isolated _e2e database and test integration mode"
            )
        if User.objects.exists():
            raise CommandError(
                "Database must be empty; this command never cleans existing data"
            )
        now = timezone.now()
        team = Team.objects.create(
            name="E2E Team A", history_complete_from=(now - timedelta(days=800)).date()
        )
        team_b = Team.objects.create(
            name="E2E Team B", history_complete_from=now.date().replace(day=1)
        )
        lead = User.objects.create_user(
            "77000000001",
            password=os.getenv("E2E_ADMIN_PASSWORD", "test-only-admin-password"),
            is_staff=True,
            is_superuser=True,
        )
        manager = User.objects.create_user("77000000002")
        stranger = User.objects.create_user("77000000003")
        finance = User.objects.create_user("77000000004")
        client = User.objects.create_user("77000000005")
        for user, role, unit, name in (
            (lead, "team_lead", team, "Руководитель E2E"),
            (manager, "manager", team, "Менеджер A"),
            (stranger, "manager", team_b, "Менеджер B"),
            (finance, "finance", team, "Финансист E2E"),
        ):
            TeamMembership.objects.create(
                user=user, team=unit, role=role, status="active"
            )
            UserProfile.objects.create(
                user=user,
                full_name=name,
                phone=user.username,
                notification_preferences={"quiet_start": "00:00", "quiet_end": "00:00"},
            )
        TeamMembership.objects.create(
            user=lead, team=team, role="finance", status="active"
        )
        UserProfile.objects.create(
            user=client, full_name="Клиент E2E", phone=client.username
        )
        p = Project.objects.create(
            name="E2E Alpha",
            team=team,
            manager=manager.profile,
            contract_amount=Decimal("1000000"),
            cost_amount=Decimal("700000"),
            cost_confirmed=True,
            is_verified=True,
            version=1,
            status="in_execution",
        )
        b = Project.objects.create(
            name="E2E Private B",
            team=team_b,
            manager=stranger.profile,
            contract_amount=Decimal("999999"),
            is_verified=True,
            version=1,
        )
        for project in (p, b):
            ProjectRevision.objects.create(
                project=project,
                version=1,
                snapshot=snapshot(project),
                source="test_fixture",
                approved_by=lead,
            )
        FinancialRecord.objects.create(
            project=p,
            amount=Decimal("100000"),
            payment_date=now.date(),
            source_key="e2e:initial",
            credited_profile=manager.profile,
            is_verified=True,
        )
        SalesTarget.objects.create(
            team=team,
            profile=manager.profile,
            month=now.date().replace(day=1),
            amount=Decimal("500000"),
            approved_by=lead,
        )
        cfg = WhatsAppConfig.objects.create(
            name="E2E source", team=team, group_jid="e2e@g.us", session_name="default"
        )
        for user in (manager, lead, finance):
            ChatAccess.objects.create(user=user, config=cfg)
        ai_settings = AISettings.objects.create(
            name="E2E provider",
            chat_provider_url="http://test-provider:9000/v1",
            embedding_provider_url="http://test-provider:9000/v1",
            embedding_dimension=8,
        )
        ai_settings.set_chat_api_key("isolated-test-provider")
        ai_settings.set_embedding_api_key("isolated-test-embedding")
        ai_settings.save(
            update_fields=(
                "chat_api_key",
                "chat_api_key_encrypted",
                "embedding_api_key",
                "embedding_api_key_encrypted",
            )
        )
        raw = RawMessage.objects.create(
            config=cfg,
            team=team,
            message_id="e2e-html",
            source_revision="fixture",
            timestamp=now,
            sender_name='<img src=x onerror="window.__xss=1">',
            content="<script>window.__xss=1</script> E2E source",
            processed=True,
        )
        MessageProcessingTrace.objects.create(
            raw_message=raw,
            project=p,
            whatsapp_message_id=raw.message_id,
            whatsapp_content=raw.content,
            whatsapp_sender_name=raw.sender_name,
            whatsapp_raw_payload={"content": raw.content},
            earlier_messages_context=[
                {"content": raw.content, "sender_name": raw.sender_name}
            ],
            earlier_messages_count=1,
            bitrix_deal_title=raw.content,
            bitrix_raw_deal={"TITLE": raw.content},
            ai_extracted_facts={"text": raw.content},
            result_summary=raw.content,
        )
        Commitment.objects.create(
            project=p,
            manager=manager.profile,
            commitment_text="E2E подготовить акт",
            deadline_at=now + timedelta(hours=2),
            original_deadline_at=now + timedelta(hours=2),
            deadline_precision="datetime",
            is_verified=True,
        )
        ClientProjectAccess.objects.create(user=client, project=p, status="active")
        self.stdout.write("Isolated E2E fixtures created")
