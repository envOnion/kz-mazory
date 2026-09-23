import re
import secrets
from django.core.cache import cache
from django.contrib.auth.models import User
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.tokens import RefreshToken
from django_q.tasks import async_task
from .tasks import clean_phone_number

OTP_TTL = 300  # 5 minutes
COOLDOWN_TTL = 45  # 45 seconds between sends
MAX_ATTEMPTS = 5

class SendVerificationCodeView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        raw_phone = request.data.get("phone", "").strip()
        clean = clean_phone_number(raw_phone)
        
        if len(clean) < 10:
            return Response(
                {"error": "Укажите корректный номер телефона (не менее 10 цифр)"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check cooldown to prevent spam
        cooldown_key = f"otp_cooldown:{clean}"
        if cache.get(cooldown_key):
            return Response(
                {"error": "Код уже отправлен. Пожалуйста, подождите перед повторной отправкой."},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        # Generate secure 4-digit numeric code
        code = f"{secrets.randbelow(9000) + 1000}"
        
        # Save to Redis
        cache.set(f"otp:{clean}", code, timeout=OTP_TTL)
        cache.set(cooldown_key, True, timeout=COOLDOWN_TTL)
        cache.set(f"otp_attempts:{clean}", 0, timeout=OTP_TTL)
        
        # Enqueue background task via Django Q and Redis
        async_task('api.tasks.send_sms_verification_code_task', clean, code)
        
        return Response({
            "status": "success",
            "message": f"Код подтверждения отправлен на номер +{clean}",
            "phone": clean,
            "expires_in": OTP_TTL,
            "cooldown": COOLDOWN_TTL
        })


class VerifyCodeView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        raw_phone = request.data.get("phone", "").strip()
        code = request.data.get("code", "").strip()
        clean = clean_phone_number(raw_phone)
        
        if not clean or not code:
            return Response(
                {"error": "Необходимо указать номер телефона и код"},
                status=status.HTTP_400_BAD_REQUEST
            )

        otp_key = f"otp:{clean}"
        attempts_key = f"otp_attempts:{clean}"
        saved_code = cache.get(otp_key)

        if not saved_code:
            return Response(
                {"error": "Срок действия кода истек или код не запрашивался. Запросите новый код."},
                status=status.HTTP_400_BAD_REQUEST
            )

        attempts = cache.get(attempts_key, 0)
        if attempts >= MAX_ATTEMPTS:
            cache.delete(otp_key)
            cache.delete(attempts_key)
            return Response(
                {"error": "Превышено количество попыток ввода. Запросите код заново."},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        if str(saved_code) != str(code):
            cache.set(attempts_key, attempts + 1, timeout=OTP_TTL)
            return Response(
                {"error": f"Неверный код. Осталось попыток: {MAX_ATTEMPTS - (attempts + 1)}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Code is valid, remove from Redis
        cache.delete(otp_key)
        cache.delete(attempts_key)

        # Get or create user in Django
        user, created = User.objects.get_or_create(username=clean)
        if created:
            user.first_name = "Сотрудник"
            user.save()

        # Issue JWT tokens
        refresh = RefreshToken.for_user(user)
        
        return Response({
            "status": "success",
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": {
                "id": user.id,
                "phone": clean,
                "name": user.first_name or "Сотрудник компании",
                "username": user.username
            }
        })


class CurrentUserView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        return Response({
            "id": user.id,
            "username": user.username,
            "phone": user.username,
            "name": user.first_name or "Сотрудник компании",
            "is_authenticated": True
        })


class SendWhatsAppView(APIView):
    """
    Triggers sending a WhatsApp message via WAHA background worker.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        phone = request.data.get("phone", "").strip()
        message = request.data.get("message", "").strip()

        if not phone or not message:
            return Response(
                {"error": "Поля phone и message обязательны"},
                status=status.HTTP_400_BAD_REQUEST
            )

        clean = clean_phone_number(phone)
        async_task('api.tasks.send_waha_whatsapp_message_task', clean, message)

        return Response({
            "status": "queued",
            "message": f"Сообщение для +{clean} отправлено в очередь WAHA",
            "phone": clean
        })
