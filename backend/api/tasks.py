import re
import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

def clean_phone_number(phone: str) -> str:
    """Strip all non-digits from phone number."""
    digits = re.sub(r'\D', '', phone)
    # If Russian/Kazakhstan format starting with 8 and length 11, normalize to 7
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    return digits

def send_sms_verification_code_task(phone: str, code: str):
    """
    Background task running via Django Q & Redis to send SMS OTP code.
    Since external cloud APIs are prohibited, this writes to the worker log
    with a clear visual banner and simulates local delivery.
    """
    clean = clean_phone_number(phone)
    sms_text = f"Ваш код подтверждения для входа в Mazory: {code}"
    
    border = "=" * 64
    msg = (
        f"\n{border}\n"
        f"📱 [MAZORY SMS DISPATCHER — DJANGO Q TASK]\n"
        f"Recipient Phone : +{clean}\n"
        f"Verification OTP: {code}\n"
        f"Message Text   : {sms_text}\n"
        f"Status         : SENT (Local Delivery OK)\n"
        f"{border}\n"
    )
    print(msg, flush=True)
    logger.info("SMS OTP for %s delivered successfully: %s", clean, code)
    return {"status": "sent", "phone": clean, "code": code}

def send_waha_whatsapp_message_task(phone: str, text: str, session: str = "default"):
    """
    Background task to send a WhatsApp message via local WAHA container.
    """
    clean = clean_phone_number(phone)
    chat_id = f"{clean}@c.us"
    waha_url = f"{settings.WAHA_API_URL}/api/sendText"
    
    payload = {
        "chatId": chat_id,
        "text": text,
        "session": session
    }
    
    print(f"\n💬 [WAHA WHATSAPP DISPATCH] Sending to {chat_id}: '{text}'...", flush=True)
    
    headers = {}
    api_key = getattr(settings, "WAHA_API_KEY", "")
    if api_key:
        headers["X-Api-Key"] = api_key

    try:
        response = requests.post(waha_url, json=payload, headers=headers, timeout=10)
        
        # If session does not exist, start it automatically
        if response.status_code == 422 and "does not exist" in response.text:
            print(f"⚡ [WAHA] Session '{session}' does not exist, initiating auto-start...", flush=True)
            requests.post(
                f"{settings.WAHA_API_URL}/api/sessions/start",
                json={"name": session},
                headers=headers,
                timeout=5
            )
            # Re-try sending
            response = requests.post(waha_url, json=payload, headers=headers, timeout=10)

        if response.status_code in (200, 201):
            print(f"✅ [WAHA] Message sent successfully to {chat_id}: {response.json()}", flush=True)
            return {"status": "delivered", "response": response.json()}
        else:
            print(f"⚠️ [WAHA] WAHA returned HTTP {response.status_code}: {response.text}", flush=True)
            return {"status": "error", "code": response.status_code, "detail": response.text}
    except requests.exceptions.RequestException as exc:
        print(f"❌ [WAHA] Connection error to WAHA at {waha_url}: {exc}", flush=True)
        return {"status": "error", "detail": str(exc)}

def push_business_event_task(phone: str, title: str, message: str, notif_type: str = "info"):
    """
    Background worker task in Django Q that pushes a live event to Redis.
    """
    from .notifications import push_notification_to_redis
    clean = clean_phone_number(phone)
    item, unread = push_notification_to_redis(clean, title, message, notif_type)
    print(f"\n🔔 [DJANGO Q EVENT DISPATCHED] New event for +{clean}: '{title}' | Unread count: {unread}", flush=True)
    return item

def dispatch_targeted_notification_task(
    phones_list: list,
    title: str,
    message: str,
    notif_type: str = "info",
    send_whatsapp: bool = False,
    sender_phone: str = ""
):
    """
    Targeted business notification dispatcher running in Django Q worker.
    Iterates over specific target phones, saves real events to Redis per user,
    and optionally delivers via WhatsApp to each target's device via WAHA.
    """
    from .notifications import push_notification_to_redis
    from .models import UserProfile

    results = []
    border = "=" * 60
    print(f"\n{border}", flush=True)
    print(f"🎯 [DJANGO Q TARGETED DISPATCHER] Starting notification broadcast", flush=True)
    print(f"   Event Title : '{title}'", flush=True)
    print(f"   Sender      : +{sender_phone or 'SYSTEM'}", flush=True)
    print(f"   Targets     : {phones_list}", flush=True)
    print(f"   WhatsApp    : {'ENABLED' if send_whatsapp else 'OPTIONAL/DISABLED'}", flush=True)
    print(f"{border}", flush=True)

    for raw_phone in phones_list:
        clean = clean_phone_number(str(raw_phone))
        if not clean:
            continue
        
        # 1. Push real notification to recipient's individual Redis storage
        item, unread = push_notification_to_redis(clean, title, message, notif_type)
        print(f"  📥 [REDIS] Stored for +{clean} | Unread now: {unread}", flush=True)

        # 2. Check if WhatsApp should be sent (either explicitly requested or user has WA toggles on)
        should_send_wa = send_whatsapp
        if not should_send_wa:
            try:
                profile = UserProfile.objects.filter(phone__icontains=clean).first()
                if profile:
                    if notif_type in ('deal', 'warning') and profile.whatsapp_stalled_deals:
                        should_send_wa = True
                    elif notif_type == 'kpi' and profile.whatsapp_critical_kpi:
                        should_send_wa = True
            except Exception as e:
                logger.warning("Could not check UserProfile for WhatsApp settings: %s", e)

        wa_status = "skipped"
        if should_send_wa:
            wa_text = f"🔔 *Mazory AI Alert*\n\n*{title}*\n{message}"
            wa_res = send_waha_whatsapp_message_task(clean, wa_text)
            wa_status = wa_res.get("status", "error")

        results.append({
            "phone": clean,
            "redis_saved": True,
            "whatsapp_status": wa_status
        })

    print(f"✅ [DJANGO Q TARGETED DISPATCHER] Finished dispatch for {len(results)} recipients\n", flush=True)
    return results

def process_incoming_message_task(message_data: dict):
    """
    Фоновый воркер Django Q2 (Event: Новое сообщение):
    1. Сохраняет RawMessage в PostgreSQL.
    2. Векторизует и сохраняет точку в Qdrant.
    3. Выполняет структурированное извлечение бизнес-фактов (Проекты, Суммы, Обещания, Дедлайны).
    4. Записывает нормализованные данные в БД (Company, Project, Commitment, FinancialRecord).
    5. Обновляет статус сообщения processed=True.
    """
    from .models import RawMessage, Project, Commitment, FinancialRecord, UserProfile
    from .qdrant_service import qdrant_service
    from django.db.models import Q
    from django.utils import timezone
    from decimal import Decimal
    from datetime import timedelta

    message_id = message_data.get('message_id') or f"waha-{int(timezone.now().timestamp() * 1000)}"
    content = message_data.get('content', '').strip()
    sender_name = message_data.get('sender_name', 'Неизвестный')
    sender_phone = clean_phone_number(message_data.get('sender_phone', ''))
    chat_id = message_data.get('chat_id', 'aquakip-sales')
    
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

    # 2. Векторизация и отправка в Qdrant
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

    # 3. Извлечение структурированной бизнес-информации
    matched_manager = UserProfile.objects.filter(
        Q(full_name__icontains=sender_name) |
        Q(phone__icontains=sender_phone)
    ).first()

    # Поиск упоминания сумм (млн, тыс, тенге, ₸)
    amount_match = re.search(r'(\d+[\d\s.,]*)\s*(млн|млрд|тыс)?\s*(тенге|тг|₸|руб)?', content, re.IGNORECASE)
    extracted_amount = None
    if amount_match:
        val_str = amount_match.group(1).replace(' ', '').replace(',', '.')
        try:
            val = float(val_str)
            multiplier = 1
            unit = (amount_match.group(2) or '').lower()
            if 'млн' in unit:
                multiplier = 1_000_000
            elif 'млрд' in unit:
                multiplier = 1_000_000_000
            elif 'тыс' in unit:
                multiplier = 1_000
            if val > 0:
                extracted_amount = Decimal(str(int(val * multiplier)))
        except ValueError:
            pass

    # Поиск упоминания известных проектов
    for proj in Project.objects.all():
        if proj.name.lower() in content.lower():
            if extracted_amount and ('оплат' in content.lower() or 'поступил' in content.lower()):
                FinancialRecord.objects.create(
                    project=proj,
                    amount=extracted_amount,
                    payment_date=timezone.now().date(),
                    payment_type='milestone',
                    status='received',
                    notes=f"Автоматически извлечено из сообщения {message_id}"
                )
                proj.paid_amount += extracted_amount
                proj.due_amount = max(Decimal('0.00'), proj.contract_amount - proj.paid_amount)
                proj.save()
            break

    # Поиск обещаний и дедлайнов
    commitment_triggers = ['обещал', 'договорюсь', 'отправлю', 'подпишем', 'сделаем', 'завершим', 'дедлайн']
    if any(t in content.lower() for t in commitment_triggers):
        deadline = timezone.now().date() + timedelta(days=2)
        if 'завтра' in content.lower():
            deadline = timezone.now().date() + timedelta(days=1)
        elif 'понедельник' in content.lower():
            deadline = timezone.now().date() + timedelta(days=4)

        Commitment.objects.create(
            manager=matched_manager,
            source_message=raw_msg,
            commitment_text=content[:250],
            deadline=deadline,
            status='pending',
            severity='medium'
        )

    raw_msg.processed = True
    raw_msg.save()
    logger.info("Message %s processed: vector stored in Qdrant, entities extracted to Postgres", message_id)
    return {"status": "processed", "message_id": message_id, "qdrant_point": point_id}


