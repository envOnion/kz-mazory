import re
import logging
import requests
from decimal import Decimal
from datetime import timedelta
from django.conf import settings
from django.utils import timezone
from django.db.models import Q

logger = logging.getLogger(__name__)

def clean_phone_number(phone: str) -> str:
    """Нормализация телефонного номера к цифрам без знаков."""
    digits = re.sub(r'\D', '', phone or '')
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    return digits

def send_waha_whatsapp_message_task(phone_or_group: str, text: str, session: str = "default"):
    """
    Отправка WhatsApp сообщения через локальный контейнер WAHA (devlikeapro/waha).
    """
    from .models import WhatsAppConfig
    cfg = WhatsAppConfig.get_active()
    
    clean_target = phone_or_group
    if not clean_target.endswith('@g.us') and not clean_target.endswith('@c.us'):
        clean_target = f"{clean_phone_number(phone_or_group)}@c.us"
        
    waha_url = f"{cfg.waha_api_url.rstrip('/')}/api/sendText"
    payload = {
        "chatId": clean_target,
        "text": text,
        "session": cfg.session_name or session or "default"
    }
    
    headers = {"Content-Type": "application/json"}
    if cfg.waha_api_key:
        headers["X-Api-Key"] = cfg.waha_api_key

    try:
        response = requests.post(waha_url, json=payload, headers=headers, timeout=10)
        if response.status_code in (200, 201):
            return {"status": "delivered", "response": response.json()}
        return {"status": "error", "code": response.status_code, "detail": response.text}
    except Exception as exc:
        logger.error("Failed to send WhatsApp message via WAHA: %s", exc)
        return {"status": "error", "detail": str(exc)}

def send_sms_verification_code_task(phone: str, code: str):
    """
    Фоновый воркер Django Q2: Отправка случайного 4-значного OTP-кода подтверждения через WhatsApp (WAHA).
    """
    clean = clean_phone_number(phone)
    text = f"Ваш код подтверждения для входа в Mazory AI: {code}\nКод действителен 5 минут."
    logger.info("Отправка OTP-кода подтверждения на номер %s через WAHA", clean)
    return send_waha_whatsapp_message_task(clean, text)

def process_incoming_message_task(message_data: dict):
    """
    Фоновый воркер Django Q2 (Event: Новое сообщение WhatsApp):
    1. Сохраняет RawMessage в PostgreSQL.
    2. Векторизует через embeddings-модель (OpenRouter LFM-2.5 1024d) и сохраняет в Qdrant.
    3. Выполняет семантический RAG-поиск в Qdrant по контексту прошлых сообщений.
    4. Запускает чат-модель (OpenRouter Nemotron-3-Ultra 550b) с полным контекстом.
    5. Квалифицирует сделку и при достаточности данных создает сделку в БД и Bitrix24 CRM.
    6. Обновляет обязательства (Commitment), платежи (FinancialRecord) и витрины данных.
    """
    from .models import (
        RawMessage, Project, Commitment, FinancialRecord,
        UserProfile, Company, BusinessEvent
    )
    from .qdrant_service import qdrant_service
    from .ai_service import AIService
    from .bitrix_service import BitrixService
    from .notifications import push_notification_to_redis

    message_id = message_data.get('message_id') or f"waha-{int(timezone.now().timestamp() * 1000)}"
    content = message_data.get('content', '').strip()
    sender_name = message_data.get('sender_name', 'Коллега')
    sender_phone = clean_phone_number(message_data.get('sender_phone', ''))
    chat_id = message_data.get('chat_id', 'aquakip-sales')

    if not content:
        return {"status": "empty_content"}

    # 1. Сохранение сырого сообщения
    raw_msg, _ = RawMessage.objects.get_or_create(
        message_id=message_id,
        defaults={
            "chat_id": chat_id,
            "sender_phone": sender_phone,
            "sender_name": sender_name,
            "timestamp": timezone.now(),
            "content": content,
            "raw_payload": message_data,
            "processed": False
        }
    )

    # 2. Векторизация и сохранение в Qdrant
    point_id = qdrant_service.upsert_message(
        message_id=message_id,
        content=content,
        payload={
            "sender_name": sender_name,
            "sender_phone": sender_phone,
            "chat_id": chat_id,
            "timestamp": timezone.now().isoformat()
        }
    )
    if point_id:
        raw_msg.qdrant_point_id = point_id

    # 3. Семантический RAG-поиск близких сообщений в Qdrant
    similar_messages = qdrant_service.search_similar(content, limit=5)
    
    # Сводка существующих сделок для контекста LLM
    existing_deals = list(Project.objects.values('name', 'status', 'contract_amount', 'company__name')[:30])
    known_deals_summary = "\n".join([
        f"- {d['name']} (Компания: {d['company__name'] or 'Не указана'}, Сумма: {d['contract_amount']} ₸, Статус: {d['status']})"
        for d in existing_deals
    ]) if existing_deals else "(База сделок пока пуста)"

    # 4. Анализ большой чат-моделью OpenRouter
    facts = AIService.analyze_message_with_context(
        content=content,
        sender_name=sender_name,
        context_messages=similar_messages,
        known_deals_summary=known_deals_summary
    )

    logger.info("AI Analysis for message %s: %s", message_id, facts)

    # 5. Обработка сущностей и обновление CRM
    object_name = facts.get("object_name")
    company_name = facts.get("company_name")
    responsible_name = facts.get("responsible_name") or sender_name

    # Менеджер
    manager = None
    if responsible_name:
        manager = UserProfile.objects.filter(
            Q(full_name__icontains=responsible_name) |
            Q(phone__icontains=sender_phone)
        ).first()

    # Компания
    company = None
    if company_name and company_name.strip():
        company, _ = Company.objects.get_or_create(
            name=company_name.strip(),
            defaults={"client_type": "private"}
        )

    # Проект / Сделка
    project = None
    if object_name and object_name.strip():
        clean_obj_name = object_name.strip()
        # Ищем проект по неточному совпадению или создаем новый
        project = Project.objects.filter(name__icontains=clean_obj_name).first()
        
        is_new_deal = (project is None)
        can_create = facts.get("can_create_deal") or (facts.get("confidence", 0) >= 0.7)

        if is_new_deal and can_create:
            project = Project.objects.create(
                name=clean_obj_name,
                company=company,
                manager=manager,
                equipment_type=facts.get("direction") or facts.get("equipment_type") or "БТП",
                contract_number=facts.get("contract_number") or "",
                deal_period=facts.get("deal_period") or "",
                status=facts.get("stage") or "qualification",
                contract_amount=Decimal(str(facts.get("contract_amount") or 0.0)),
                cost_amount=Decimal(str(facts.get("cost_amount") or 0.0)),
                paid_amount=Decimal(str(facts.get("paid_amount") or 0.0)),
                barter_amount=Decimal(str(facts.get("barter_amount") or 0.0)),
                guarantee_amount=Decimal(str(facts.get("guarantee_amount") or 0.0)),
                avr_status=facts.get("avr_status") or "Не закрыт",
                current_action=facts.get("current_action") or content[:200],
                next_action=facts.get("next_action") or "",
                decision_maker=facts.get("decision_maker") or "",
                blocker=facts.get("blocker") or "",
                priority=facts.get("priority") or "standard"
            )
            
            # Синхронизация в Bitrix24 CRM
            bitrix_id = BitrixService.create_deal({
                "name": project.name,
                "contract_amount": project.contract_amount,
                "direction": project.equipment_type,
                "deal_period": project.deal_period,
                "current_action": project.current_action,
                "next_action": project.next_action,
            })
            if bitrix_id:
                project.bitrix_id = bitrix_id
                project.save(update_fields=['bitrix_id'])

            # Бизнес-событие и уведомление
            event_title = f"Создана новая сделка: {project.name}"
            BusinessEvent.objects.create(
                event_type="new_deal",
                project=project,
                manager=manager,
                title=event_title,
                description=f"Сумма: {project.contract_amount:,.2f} ₸. Менеджер: {responsible_name}",
                severity="info"
            )
            push_notification_to_redis(sender_phone, event_title, f"Сделка {project.name} успешно зарегистрирована в системе и Bitrix24", "deal")

        elif project:
            # Обновление существующей сделки (не затираем существующие данные пустыми значениями)
            if facts.get("contract_amount"):
                project.contract_amount = Decimal(str(facts["contract_amount"]))
            if facts.get("cost_amount"):
                project.cost_amount = Decimal(str(facts["cost_amount"]))
            if facts.get("paid_amount"):
                project.paid_amount += Decimal(str(facts["paid_amount"]))
            if facts.get("stage"):
                project.status = facts["stage"]
            if facts.get("current_action"):
                project.current_action = facts["current_action"]
            if facts.get("next_action"):
                project.next_action = facts["next_action"]
            if facts.get("blocker"):
                project.blocker = facts["blocker"]
            if company and not project.company:
                project.company = company
            if manager and not project.manager:
                project.manager = manager
            project.save()

    # Обязательства / Обещания менеджеров (Commitment)
    next_action = facts.get("next_action")
    if next_action and manager:
        next_action_at = facts.get("next_action_at")
        deadline = None
        if next_action_at:
            try:
                deadline = timezone.datetime.fromisoformat(next_action_at).date()
            except Exception:
                deadline = timezone.now().date() + timedelta(days=2)
        else:
            deadline = timezone.now().date() + timedelta(days=2)

        Commitment.objects.create(
            project=project,
            manager=manager,
            source_message=raw_msg,
            commitment_text=next_action,
            deadline=deadline,
            status='pending',
            severity='critical' if ('срочно' in content.lower() or 'договор' in next_action.lower()) else 'medium'
        )

    # Финансовые записи (FinancialRecord)
    paid_amt = facts.get("paid_amount")
    if paid_amt and float(paid_amt) > 0 and project:
        FinancialRecord.objects.create(
            project=project,
            amount=Decimal(str(paid_amt)),
            payment_date=timezone.now().date(),
            payment_type='final' if 'окончательн' in content.lower() else 'milestone',
            status='received',
            notes=f"Извлечено из отчета {sender_name}: {content[:100]}"
        )

    raw_msg.processed = True
    raw_msg.save(update_fields=['processed'])
    
    return {
        "status": "processed",
        "message_id": message_id,
        "deal": project.name if project else None,
        "bitrix_id": project.bitrix_id if project else None
    }
