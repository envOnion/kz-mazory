import logging
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.utils import timezone
from api.models import (
    RawMessage, Project, Commitment, FinancialRecord,
    MessageProcessingTrace
)
from api.qdrant_service import qdrant_service

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Ретроспективное построение сквозных трассировок пайплайна (WhatsApp -> Сообщения ранее -> Bitrix24 -> Итог)"

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Принудительно перезаписать существующие трассировки',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=100,
            help='Лимит обрабатываемых сообщений (по умолчанию 100)',
        )

    def handle(self, *args, **options):
        force = options['force']
        limit = options['limit']

        raw_messages = RawMessage.objects.all().order_by('-timestamp')[:limit]
        self.stdout.write(f"Найдено сообщений для анализа трассировки: {len(raw_messages)}")

        created_count = 0
        updated_count = 0

        for raw_msg in raw_messages:
            existing = MessageProcessingTrace.objects.filter(whatsapp_message_id=raw_msg.message_id).first()
            if existing and not force:
                continue

            # 1. Входные данные WhatsApp
            content = raw_msg.content or ""
            sender_name = raw_msg.sender_name or "Коллега"
            sender_phone = raw_msg.sender_phone or ""
            chat_id = raw_msg.chat_id or "aquakip-sales"

            # 2. Зависимые сообщения ранее (Qdrant RAG + сообщения из того же чата до этого timestamp)
            earlier_context = []
            try:
                similar = qdrant_service.search_similar(content, limit=4)
                for s in similar:
                    # Исключаем само текущее сообщение
                    if s.get("message_id") == raw_msg.message_id:
                        continue
                    earlier_context.append({
                        "message_id": s.get("message_id") or s.get("id") or "",
                        "sender_name": s.get("sender_name", ""),
                        "sender_phone": s.get("sender_phone", ""),
                        "timestamp": str(s.get("timestamp") or ""),
                        "content": s.get("content", ""),
                        "score": round(float(s.get("score", 0.0)), 4)
                    })
            except Exception as e:
                logger.debug("Qdrant lookup skipped in backfill: %s", e)

            # Если Qdrant вернул пусто, подтягиваем хронологически предшествующие сообщения из RawMessage
            if not earlier_context:
                prior_msgs = RawMessage.objects.filter(
                    timestamp__lt=raw_msg.timestamp
                ).exclude(id=raw_msg.id).order_by('-timestamp')[:3]
                for pm in prior_msgs:
                    earlier_context.append({
                        "message_id": pm.message_id,
                        "sender_name": pm.sender_name,
                        "sender_phone": pm.sender_phone,
                        "timestamp": pm.timestamp.isoformat() if pm.timestamp else "",
                        "content": pm.content[:200],
                        "score": 0.72  # Хронологический контекст
                    })

            # 3. Зависимые данные из Bitrix24 и привязанные сущности
            linked_commitment = Commitment.objects.filter(source_message=raw_msg).first()
            project = None
            if linked_commitment and linked_commitment.project:
                project = linked_commitment.project
            else:
                # Поиск проекта по совпадению названия в тексте
                for p in Project.objects.all()[:50]:
                    if p.name and len(p.name) > 3 and p.name.lower() in content.lower():
                        project = p
                        break

            linked_financial = None
            if project:
                linked_financial = FinancialRecord.objects.filter(project=project).order_by('-created_at').first()

            bitrix_matched_deal_id = ""
            bitrix_deal_title = ""
            bitrix_deal_stage = ""
            bitrix_deal_opportunity = None
            bitrix_company_data = {}
            bitrix_raw_deal = {}

            if project:
                if project.bitrix_id:
                    bitrix_matched_deal_id = str(project.bitrix_id)
                    bitrix_deal_title = project.name
                    bitrix_deal_stage = project.status
                    bitrix_deal_opportunity = project.contract_amount
                    if project.company:
                        bitrix_company_data = {
                            "company_name": project.company.name,
                            "bitrix_company_id": project.company.bitrix_company_id
                        }
                    bitrix_raw_deal = {
                        "ID": project.bitrix_id,
                        "TITLE": project.name,
                        "STAGE_ID": project.status,
                        "OPPORTUNITY": float(project.contract_amount) if project.contract_amount else 0.0
                    }

            # 4. Определение действия и итогового резюме
            pipeline_action = "non_commercial"
            if project and project.source == 'bitrix_crm':
                pipeline_action = "matched_bitrix_imported"
            elif project and raw_msg.created_at and project.created_at and abs((project.created_at - raw_msg.created_at).total_seconds()) < 60:
                pipeline_action = "created_deal"
            elif project:
                pipeline_action = "updated_deal"
            elif linked_commitment:
                pipeline_action = "commitment_created"
            elif linked_financial:
                pipeline_action = "financial_record_created"

            summary_lines = [
                f"1. Входные данные WhatsApp: сообщение от {sender_name} ({sender_phone or 'номер не указан'}).",
                f"2. Сообщения ранее: найдено {len(earlier_context)} зависимых сообщений из контекста переписки.",
                f"3. Bitrix24 CRM: {'найдена сделка #' + bitrix_matched_deal_id + ' «' + bitrix_deal_title + '»' if bitrix_matched_deal_id else 'прямых совпадений по номеру сделки в CRM не зафиксировано'}.",
                f"4. Итоговая запись: {'создана/обновлена сделка «' + project.name + '»' if project else ('зафиксировано обязательство' if linked_commitment else 'информационное сообщение')}."
            ]
            result_summary = "\n".join(summary_lines)

            facts = {
                "object_name": project.name if project else None,
                "contract_amount": float(project.contract_amount) if project and project.contract_amount else None,
                "stage": project.status if project else None,
                "confidence": 0.88 if project else 0.5,
                "next_action": linked_commitment.commitment_text if linked_commitment else None,
                "is_deal_fact": bool(project)
            }

            trace_obj, was_created = MessageProcessingTrace.objects.update_or_create(
                whatsapp_message_id=raw_msg.message_id,
                defaults={
                    "raw_message": raw_msg,
                    "project": project,
                    "commitment": linked_commitment,
                    "financial_record": linked_financial,
                    "whatsapp_chat_id": chat_id,
                    "whatsapp_sender_phone": sender_phone,
                    "whatsapp_sender_name": sender_name,
                    "whatsapp_timestamp": raw_msg.timestamp,
                    "whatsapp_content": content,
                    "whatsapp_raw_payload": raw_msg.raw_payload or {},
                    "earlier_messages_context": earlier_context,
                    "earlier_messages_count": len(earlier_context),
                    "bitrix_matched_deal_id": bitrix_matched_deal_id,
                    "bitrix_deal_title": bitrix_deal_title,
                    "bitrix_deal_stage": bitrix_deal_stage,
                    "bitrix_deal_opportunity": bitrix_deal_opportunity,
                    "bitrix_search_query": project.name if project else "",
                    "bitrix_company_data": bitrix_company_data,
                    "bitrix_raw_deal": bitrix_raw_deal,
                    "bitrix_known_deals_summary": f"Сделка: {project.name}" if project else "",
                    "ai_extracted_facts": facts,
                    "ai_confidence": 0.88 if project else 0.5,
                    "pipeline_action": pipeline_action,
                    "status": "success",
                    "result_summary": result_summary
                }
            )

            if was_created:
                created_count += 1
            else:
                updated_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"Успешно обработано: создано {created_count} новых трассировок, обновлено {updated_count}."
        ))
