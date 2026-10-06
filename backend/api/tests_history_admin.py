from django.contrib import admin
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from .job_admin import WhatsAppHistoryRunAdmin
from .models import (
    Company,
    DialogueThread,
    FactCandidate,
    MessageProcessingTrace,
    Project,
    RawMessage,
    Team,
    ThreadMessage,
    ThreadRevision,
    WhatsAppConfig,
    WhatsAppHistoryJob,
    WhatsAppHistoryRun,
)


class HistoryRunAdminTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Team Admin")
        self.user = User.objects.create_superuser("admin_user", "admin@example.com", "password")
        self.config = WhatsAppConfig.objects.create(
            team=self.team, group_jid="history@g.us", name="Отдел продаж"
        )
        self.job = WhatsAppHistoryJob.objects.create(config=self.config)
        self.run = WhatsAppHistoryRun.objects.create(
            job=self.job,
            state="completed",
            fetched_count=10,
            imported_count=5,
            existing_count=5,
        )
        self.site = admin.site
        self.admin = WhatsAppHistoryRunAdmin(WhatsAppHistoryRun, self.site)
        self.factory = RequestFactory()

    def test_admin_configuration_fields(self):
        self.assertIn("thematic_summary", self.admin.list_display)
        self.assertIn("thematic_tree_card", self.admin.fields)
        self.assertIn("thematic_tree_card", self.admin.readonly_fields)

    def test_cancel_button_is_available_while_analyzing(self):
        self.client.force_login(self.user)
        self.run.state = "analyzing"
        self.run.save()
        url = reverse("admin:api_whatsapphistoryrun_change", args=[self.run.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_cancel_import"])
        self.assertContains(response, "Отменить запуск")
        self.run.state = "cancelled"
        self.run.save()
        response = self.client.get(url)
        self.assertFalse(response.context["can_cancel_import"])

    def test_thematic_summary_and_tree_card_empty(self):
        self.assertEqual(self.admin.thematic_summary(self.run), "0 тредов")
        html = self.admin.thematic_tree_card(self.run)
        self.assertIn("0 диалогов", html)
        self.assertIn("Иерархический разбор импорта", html)

    def test_thematic_summary_and_tree_card_hierarchy(self):
        company = Company.objects.create(name="ТОО BI Group", bitrix_company_id="1042")
        project = Project.objects.create(
            name="ЖК Grand Park",
            team=self.team,
            company=company,
            bitrix_id="8920",
            contract_amount=148500000,
        )

        msg1 = RawMessage.objects.create(
            config=self.config,
            message_id="msg-1",
            sender_phone="+77011112233",
            content="Обсуждаем ЖК Grand Park",
            timestamp=timezone.now(),
        )
        msg2 = RawMessage.objects.create(
            config=self.config,
            message_id="msg-2",
            sender_phone="+77011112233",
            content="Согласовали ТУ",
            timestamp=timezone.now(),
        )
        msg3 = RawMessage.objects.create(
            config=self.config,
            message_id="msg-3",
            sender_phone="+77011112233",
            content="Еще реплика",
            timestamp=timezone.now(),
        )
        self.run.messages.add(msg1, msg2, msg3)

        # Root thread
        root_thread = DialogueThread.objects.create(
            team=self.team,
            config=self.config,
            source_key="src1",
            identity="ident-root",
            project=project,
            topic="Поставка 4 БМК для ЖК Grand Park",
            state="open",
            version=1,
        )
        ThreadMessage.objects.create(
            thread=root_thread, raw_message=msg1, thought_state="intermediate"
        )
        ThreadMessage.objects.create(
            thread=root_thread, raw_message=msg2, thought_state="intermediate"
        )

        # Child thread
        child_thread = DialogueThread.objects.create(
            team=self.team,
            config=self.config,
            parent=root_thread,
            source_key="src1",
            identity="ident-child",
            project=project,
            topic="Согласование ТУ по газу и аванса",
            state="ready",
            version=1,
        )
        ThreadMessage.objects.create(
            thread=child_thread, raw_message=msg3, thought_state="final"
        )

        trace = MessageProcessingTrace.objects.create(
            raw_message=msg1,
            whatsapp_message_id="msg-1",
            status="success",
        )

        # Revisions and candidates
        root_rev = ThreadRevision.objects.create(
            thread=root_thread,
            version=1,
            state="open",
            topic="Поставка 4 БМК для ЖК Grand Park",
        )
        FactCandidate.objects.create(
            thread_revision=root_rev,
            trace=trace,
            team=self.team,
            project=project,
            fact_type="project",
            status="pending",
            crm_match_state="matched",
            source_key="cand-1",
            proposed_changes={
                "object_name": "ЖК Grand Park",
                "contract_amount": "148500000",
                "stage_id": "КП",
            },
        )
        FactCandidate.objects.create(
            thread_revision=root_rev,
            trace=trace,
            team=self.team,
            project=project,
            fact_type="commitment",
            status="pending",
            source_key="cand-2",
            proposed_changes={
                "commitment_text": "ТУ до 10.10 12:00",
                "assignee": "Нурлан Касымов",
            },
        )

        child_rev = ThreadRevision.objects.create(
            thread=child_thread,
            version=1,
            state="ready",
            topic="Согласование ТУ по газу и аванса",
        )
        FactCandidate.objects.create(
            thread_revision=child_rev,
            trace=trace,
            team=self.team,
            project=project,
            fact_type="commitment",
            status="approved",
            source_key="cand-3",
            proposed_changes={
                "commitment_text": "Предоставить схему подключения",
                "assignee": "Алексей",
            },
        )

        # Verify thematic_summary: 2 threads (1 open / 1 ready) · 2 обязательства
        summary = self.admin.thematic_summary(self.run)
        self.assertEqual(summary, "2 треда (1 open / 1 ready) · 2 обязательства")

        # Verify thematic_tree_card
        html = self.admin.thematic_tree_card(self.run)
        self.assertIn("ТОО BI Group", html)
        self.assertIn("#1042", html)
        self.assertIn("ЖК Grand Park", html)
        self.assertIn("148 500 000 ₸", html)
        self.assertIn("Поставка 4 БМК для ЖК Grand Park", html)
        self.assertIn("open (мысль в процессе)", html)
        self.assertIn("Согласование ТУ по газу и аванса", html)
        self.assertIn("ready (закончен)", html)
        self.assertIn("ТУ до 10.10 12:00", html)
        self.assertIn("Нурлан Касымов", html)
        self.assertIn("Сопоставлено Bitrix", html)
        self.assertIn("badge-good", html)
        self.assertIn("badge-warn", html)

    def test_change_view_renders_thematic_tree_section(self):
        request = self.factory.get(f"/admin/api/whatsapphistoryrun/{self.run.id}/change/")
        request.user = self.user
        response = self.admin.change_view(request, str(self.run.id))
        self.assertEqual(response.status_code, 200)
        content = response.rendered_content
        self.assertIn('data-testid="thematic-tree-section"', content)
        self.assertIn("Иерархический разбор импорта", content)
