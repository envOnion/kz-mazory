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
        if response.status_code in (200, 201):
            print(f"✅ [WAHA] Message sent successfully to {chat_id}: {response.json()}", flush=True)
            return {"status": "delivered", "response": response.json()}
        else:
            print(f"⚠️ [WAHA] WAHA returned HTTP {response.status_code}: {response.text}", flush=True)
            return {"status": "error", "code": response.status_code, "detail": response.text}
    except requests.exceptions.RequestException as exc:
        print(f"❌ [WAHA] Connection error to WAHA at {waha_url}: {exc}", flush=True)
        return {"status": "error", "detail": str(exc)}
