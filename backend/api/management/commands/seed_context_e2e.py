"""Synthetic histories for browser E2E; never runs against working data."""

import json
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import Permission, User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from api.models import (
    FactCandidate,
    FactEvidence,
    MessageProcessingTrace,
    Project,
    RawMessage,
    Team,
    TeamMembership,
    WhatsAppConfig,
)


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument(
            "scenario",
            choices=[
                "full",
                "overflow",
                "huge_target",
                "provider_overflow",
                "payment",
                "unknown_time",
            ],
        )
        parser.add_argument("--count", type=int, default=50)

    def handle(self, *args, **options):
        if not settings.INTEGRATION_TEST_MODE or not str(
            settings.DATABASES["default"]["NAME"]
        ).endswith("_e2e"):
            raise CommandError("Requires isolated integration database")
        token = uuid.uuid4().hex
        team = Team.objects.get(name="E2E Team A")
        cfg = WhatsAppConfig.objects.create(
            name=f"Context {token}", team=team, group_jid=f"{token}@g.us"
        )
        moment = timezone.now() - timedelta(minutes=10)
        count = options["count"]
        rows = []
        for index in range(count):
            content = f"{token}: История {index} Қазақша жеткізу №{index} — 125 000 ₸."
            if index == 0:
                content += " Полный текст сообщения: срок поставки согласован. " * 300
            if options["scenario"] == "overflow" and index == 0:
                content = "Қазақша поставка оборудования. " * 80000
            rows.append(
                RawMessage(
                    config=cfg,
                    team=team,
                    chat_id=cfg.group_jid,
                    message_id=f"{token}:{index}",
                    timestamp=moment + timedelta(seconds=index),
                    content=content,
                    sender_name="Автор истории",
                    processed=True,
                )
            )
        RawMessage.objects.bulk_create(rows)
        # Same-timestamp ordering and revision selection; the last old revision is replaced.
        revised = RawMessage.objects.create(
            config=cfg,
            team=team,
            chat_id=cfg.group_jid,
            message_id=rows[-1].message_id,
            source_revision="edited",
            timestamp=rows[-1].timestamp,
            content=f"{token}: Последняя редакция",
            processed=True,
        )
        empty = RawMessage.objects.create(
            config=cfg,
            team=team,
            chat_id=cfg.group_jid,
            message_id=f"{token}:empty",
            timestamp=moment,
            content="<script>malicious()</script>",
            processed=True,
        )
        RawMessage.objects.create(
            config=cfg,
            team=team,
            chat_id="other-chat@g.us",
            message_id=f"{token}:wrong-chat",
            timestamp=moment,
            content="OTHER_CHAT_MUST_NOT_LEAK",
            processed=True,
        )
        RawMessage.objects.create(
            config=cfg,
            team=team,
            session_name="other-session",
            chat_id=cfg.group_jid,
            message_id=f"{token}:wrong-session",
            timestamp=moment,
            content="OTHER_SESSION_MUST_NOT_LEAK",
            processed=True,
        )
        RawMessage.objects.create(
            config=cfg,
            team=Team.objects.get(name="E2E Team B"),
            chat_id=cfg.group_jid,
            message_id=f"{token}:wrong-team",
            timestamp=moment,
            content="OTHER_TEAM_MUST_NOT_LEAK",
            processed=True,
        )
        future = RawMessage.objects.create(
            config=cfg,
            team=team,
            chat_id=cfg.group_jid,
            message_id=f"{token}:future",
            timestamp=moment + timedelta(days=2),
            received_at=timezone.now() + timedelta(days=2),
            content="FUTURE_MUST_NOT_LEAK",
            processed=True,
        )
        target_content = f"{token}: Уточни сведения по предыдущей переписке."
        if options["scenario"] == "huge_target":
            target_content = "Қазақша поставка оборудования. " * 80000
        if options["scenario"] == "provider_overflow":
            target_content = f"{token} SIMULATE_CONTEXT_OVERFLOW"
        if options["scenario"] == "payment":
            target_content = f"E2E payment: amount=123456.00 date={timezone.now().date().isoformat()} {token}"
        raw = RawMessage.objects.create(
            config=cfg,
            team=team,
            chat_id=cfg.group_jid,
            message_id=f"{token}:target",
            timestamp=revised.timestamp,
            content=target_content,
            sender_name="Автор",
            processed=True,
            sent_at_known=options["scenario"] != "unknown_time",
        )
        if options["scenario"] == "unknown_time":
            # All eligible same-source histories use receipt chronology in this scenario.
            RawMessage.objects.filter(
                pk__in=[item.id for item in rows] + [revised.id, empty.id]
            ).update(sent_at_known=False)
        legacy = [
            {
                "message_id": item.message_id,
                "content": item.content[:200],
                "score": 0.72,
                "sender_name": item.sender_name,
            }
            for item in rows[:3]
        ]
        trace = MessageProcessingTrace.objects.create(
            raw_message=raw,
            whatsapp_message_id=raw.message_id,
            whatsapp_content=raw.content,
            earlier_messages_context=legacy,
            earlier_messages_count=len(legacy),
            result_summary="1. Входные данные WhatsApp: историческое заполнение.",
        )
        denied = User.objects.create_user(
            f"context-denied-{token}",
            password="test-only-admin-password",
            is_staff=True,
        )
        denied.user_permissions.add(
            Permission.objects.get(codename="view_messageprocessingtrace")
        )
        TeamMembership.objects.create(
            user=denied,
            team=Team.objects.get(name="E2E Team B"),
            role="manager",
            status="active",
        )
        if options["scenario"] == "provider_overflow":
            FactCandidate.objects.create(
                trace=trace,
                team=team,
                source_key=f"pending:{token}",
                fact_type="project",
                proposed_changes={
                    "fact_type": "project",
                    "object_name": "Сохранить предложение",
                },
            )
        if options["scenario"] == "payment":
            project = Project.objects.get(name="E2E Alpha")
            candidate = FactCandidate.objects.create(
                trace=trace,
                team=team,
                project=project,
                source_key=f"fixture:{token}",
                fact_type="payment",
                status="approved",
                proposed_changes={
                    "fact_type": "payment",
                    "object_name": "E2E Alpha",
                    "amount": "123456.00",
                    "currency": "KZT",
                    "payment_kind": "increment",
                    "payment_date": timezone.now().date().isoformat(),
                },
            )
            FactEvidence.objects.create(
                candidate=candidate, raw_message=raw, quote=target_content
            )
        self.stdout.write(
            json.dumps(
                {
                    "trace_id": trace.id,
                    "denied_username": denied.username,
                    "raw_id": raw.id,
                    "chat_id": cfg.group_jid,
                    "count": count,
                    "message_id": raw.message_id,
                    "token": token,
                    "target": target_content
                    if options["scenario"] != "huge_target"
                    else "",
                    "expected_ids": [item.id for item in rows[:-1]] + [revised.id],
                    "first_content": rows[0].content
                    if options["scenario"] != "overflow"
                    else "",
                    "future_id": future.id,
                },
                ensure_ascii=False,
            )
        )
