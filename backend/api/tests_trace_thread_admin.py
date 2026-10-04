from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from decimal import Decimal

from api.admin import MessageProcessingTraceAdmin
from api.models import (
    Company,
    DialogueThread,
    FactCandidate,
    MessageProcessingTrace,
    Project,
    RawMessage,
    Team,
    ThreadMessage,
    WhatsAppConfig,
    Commitment,
    FinancialRecord,
)


class DummyAdminSite(AdminSite):
    pass


class MessageProcessingTraceAdminThreadTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Команда Продаж")
        self.user = User.objects.create_superuser("admin_user", "admin@example.com", "password")
        self.config = WhatsAppConfig.objects.create(
            team=self.team, group_jid="sales@g.us", name="Отдел продаж"
        )
        self.site = DummyAdminSite()
        self.admin = MessageProcessingTraceAdmin(MessageProcessingTrace, self.site)

    def test_admin_configuration(self):
        self.assertIn("thread_badge", self.admin.list_display)
        self.assertIn("dialogue_thread_hierarchy_card", self.admin.readonly_fields)
        
        # Verify stage 2 fieldset includes dialogue_thread_hierarchy_card
        stage_2_found = False
        for title, opts in self.admin.fieldsets:
            if "Этап 2" in title:
                self.assertIn("dialogue_thread_hierarchy_card", opts["fields"])
                self.assertIn("stage_2_earlier_messages_card", opts["fields"])
                stage_2_found = True
        self.assertTrue(stage_2_found, "Fieldset for Stage 2 not found")

    def test_thread_badge_no_raw_message(self):
        trace = MessageProcessingTrace.objects.create(
            status="success",
            whatsapp_content="Привет",
            whatsapp_sender_name="Алибек",
        )
        badge = self.admin.thread_badge(trace)
        self.assertIn("Без треда", badge)
        self.assertIn("mazory-admin-badge--neutral", badge)

    def test_thread_badge_unassigned_raw_message(self):
        raw = RawMessage.objects.create(
            config=self.config,
            team=self.team,
            message_id="msg_1",
            sender_phone="123",
            sender_name="Алибек",
            timestamp=timezone.now(),
            content="Тестовое сообщение",
        )
        trace = MessageProcessingTrace.objects.create(
            raw_message=raw,
            status="success",
            whatsapp_content="Тестовое сообщение",
        )
        badge = self.admin.thread_badge(trace)
        self.assertIn("Вне треда", badge)
        self.assertIn("mazory-admin-badge--neutral", badge)

    def test_thread_badge_with_thread_and_project(self):
        company = Company.objects.create(name="ТОО BI Group", bitrix_company_id="1042")
        project = Project.objects.create(
            name="ЖК Grand Park",
            company=company,
            team=self.team,
            bitrix_id="8920",
            contract_amount=Decimal("148500000.00"),
        )
        thread = DialogueThread.objects.create(
            team=self.team,
            config=self.config,
            source_key="sales@g.us",
            identity="thread_gp_1",
            project=project,
            topic="Поставка 4 БМК для ЖК Grand Park",
            state="open",
        )
        raw = RawMessage.objects.create(
            config=self.config,
            team=self.team,
            message_id="msg_2",
            sender_phone="123",
            sender_name="Бауржан Сагинтаев",
            timestamp=timezone.now(),
            content="По ЖК Grand Park проект согласован",
        )
        ThreadMessage.objects.create(
            thread=thread,
            raw_message=raw,
            thought_state="intermediate",
            relation="discusses",
            rationale="Согласование ТУ и графика финансирования",
        )
        trace = MessageProcessingTrace.objects.create(
            raw_message=raw,
            project=project,
            status="success",
            whatsapp_content="По ЖК Grand Park проект согласован",
        )

        badge = self.admin.thread_badge(trace)
        self.assertIn(f"Тред #{thread.id} (open)", badge)
        self.assertIn("ЖК Grand Park", badge)
        self.assertIn("mazory-admin-badge--warn", badge)

    def test_dialogue_thread_hierarchy_card_subtle_note_when_no_raw_message(self):
        trace = MessageProcessingTrace.objects.create(
            status="success",
            whatsapp_content="Автономное сообщение",
        )
        card = self.admin.dialogue_thread_hierarchy_card(trace)
        self.assertIn("Сообщение вне треда диалога", card)

    def test_dialogue_thread_hierarchy_card_subtle_note_when_unassigned(self):
        raw = RawMessage.objects.create(
            config=self.config,
            team=self.team,
            message_id="msg_3",
            sender_phone="123",
            timestamp=timezone.now(),
            content="Автономный вопрос",
        )
        trace = MessageProcessingTrace.objects.create(
            raw_message=raw,
            status="success",
            whatsapp_content="Автономный вопрос",
        )
        card = self.admin.dialogue_thread_hierarchy_card(trace)
        self.assertIn("Сообщение ещё не включено в тред диалога", card)

    def test_dialogue_thread_hierarchy_card_full_hierarchy(self):
        company = Company.objects.create(name="ТОО BI Group", bitrix_company_id="1042")
        project = Project.objects.create(
            name="ЖК Grand Park",
            company=company,
            team=self.team,
            bitrix_id="8920",
            contract_amount=Decimal("148500000.00"),
        )
        thread = DialogueThread.objects.create(
            team=self.team,
            config=self.config,
            source_key="sales@g.us",
            identity="thread_gp_full",
            project=project,
            topic="Поставка 4 БМК для ЖК Grand Park",
            state="open",
            summary="Идет обсуждение котельных",
        )
        raw = RawMessage.objects.create(
            config=self.config,
            team=self.team,
            message_id="msg_4",
            sender_phone="123",
            sender_name="Бауржан Сагинтаев",
            timestamp=timezone.now(),
            content="По ЖК Grand Park проект согласован. Нужно 4 БМК.",
        )
        ThreadMessage.objects.create(
            thread=thread,
            raw_message=raw,
            thought_state="intermediate",
            relation="discusses",
            rationale="Обсуждение схемы финансирования и ТУ",
        )
        trace = MessageProcessingTrace.objects.create(
            raw_message=raw,
            project=project,
            status="success",
            whatsapp_content="По ЖК Grand Park проект согласован. Нужно 4 БМК.",
            ai_confidence=0.94,
        )
        FactCandidate.objects.create(
            trace=trace,
            team=self.team,
            project=project,
            fact_type="project",
            proposed_changes={
                "name": "ЖК Grand Park",
                "contract_amount": 148500000.0,
                "stage": "КП",
            },
            status="approved",
            source_key="cand_1",
        )
        FactCandidate.objects.create(
            trace=trace,
            team=self.team,
            project=project,
            fact_type="commitment",
            proposed_changes={
                "commitment_text": "Предоставить ТУ до 10.10 12:00",
                "deadline": "10.10.2026 12:00",
            },
            status="pending",
            source_key="cand_2",
        )

        card = self.admin.dialogue_thread_hierarchy_card(trace)

        # 1. Company
        self.assertIn("ТОО BI Group", card)
        self.assertIn("#1042", card)

        # 2. Project
        self.assertIn("ЖК Grand Park", card)
        self.assertIn("8920", card)
        self.assertIn("148,500,000.00 ₸", card)

        # 3. Thread #ID and topic
        self.assertIn(f"Тред #{thread.id}", card)
        self.assertIn("Поставка 4 БМК для ЖК Grand Park", card)
        self.assertIn("open (мысль в процессе)", card)

        # 4. Message role and rationale
        self.assertIn("discusses", card)
        self.assertIn("Обсуждение схемы финансирования и ТУ", card)

        # 5. Extracted facts with ₸ amounts
        self.assertIn("148,500,000.00 ₸", card)
        self.assertIn("Предоставить ТУ до 10.10 12:00", card)

    def test_dialogue_thread_hierarchy_card_subdialogue(self):
        company = Company.objects.create(name="ТОО BAZIS-A", bitrix_company_id="2180")
        project = Project.objects.create(
            name="ЖК River Park",
            company=company,
            team=self.team,
            contract_amount=Decimal("19260000.00"),
        )
        parent_thread = DialogueThread.objects.create(
            team=self.team,
            config=self.config,
            source_key="sales@g.us",
            identity="thread_parent_rp",
            project=project,
            topic="БТП на ЖК River Park",
            state="ready",
        )
        child_thread = DialogueThread.objects.create(
            team=self.team,
            config=self.config,
            source_key="sales@g.us",
            identity="thread_child_rp",
            parent=parent_thread,
            project=project,
            topic="Согласование ТУ по газу и аванса",
            state="ready",
        )
        raw = RawMessage.objects.create(
            config=self.config,
            team=self.team,
            message_id="msg_5",
            sender_phone="456",
            timestamp=timezone.now(),
            content="Аванс 19 260 000 тенге перечислен",
        )
        ThreadMessage.objects.create(
            thread=child_thread,
            raw_message=raw,
            thought_state="final",
            relation="fulfills",
            rationale="Финальное подтверждение оплаты аванса",
        )
        trace = MessageProcessingTrace.objects.create(
            raw_message=raw,
            project=project,
            status="success",
            whatsapp_content="Аванс 19 260 000 тенге перечислен",
        )

        card = self.admin.dialogue_thread_hierarchy_card(trace)
        self.assertIn(f"Родительский тред #{parent_thread.id}", card)
        self.assertIn(f"Поддиалог #{child_thread.id}", card)
        self.assertIn("fulfills", card)
        self.assertIn("Финальное подтверждение оплаты аванса", card)

    def test_dialogue_thread_hierarchy_card_with_commitment_and_payment_records(self):
        company = Company.objects.create(name="ТОО BI Group")
        project = Project.objects.create(
            name="ЖК Отрар",
            company=company,
            team=self.team,
        )
        thread = DialogueThread.objects.create(
            team=self.team,
            config=self.config,
            source_key="sales@g.us",
            identity="thread_otrar",
            project=project,
            topic="ЖК Отрар согласование",
            state="ready",
        )
        raw = RawMessage.objects.create(
            config=self.config,
            team=self.team,
            message_id="msg_6",
            sender_phone="789",
            timestamp=timezone.now(),
            content="Оплата договора произведена",
        )
        ThreadMessage.objects.create(
            thread=thread,
            raw_message=raw,
            thought_state="final",
            relation="answers",
            rationale="Ответ на запрос бухгалтерии",
        )
        commitment = Commitment.objects.create(
            project=project,
            team=self.team,
            commitment_text="Подписать акт приемки",
            deadline=timezone.now().date(),
        )
        fin_record = FinancialRecord.objects.create(
            project=project,
            amount=Decimal("5500000.00"),
            payment_type="advance",
            payment_date=timezone.now().date(),
        )
        trace = MessageProcessingTrace.objects.create(
            raw_message=raw,
            project=project,
            commitment=commitment,
            financial_record=fin_record,
            status="success",
            whatsapp_content="Оплата договора произведена",
        )

        card = self.admin.dialogue_thread_hierarchy_card(trace)
        self.assertIn("Подписать акт приемки", card)
        self.assertIn("5,500,000.00 ₸", card)
