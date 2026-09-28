import re
from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.middleware.csrf import CsrfViewMiddleware, get_token
from django.utils import timezone
from rest_framework import serializers
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, Throttled
from . import otp
from .access import has_access, memberships, integration_allowed, is_client
from .authentication import create_session, rotate_refresh
from .models import (
    AuthSession,
    TeamMembership,
    ClientProjectAccess,
    UserProfile,
    OutboxEvent,
)
from .security import Unavailable

OTP_TTL, COOLDOWN_TTL, MAX_ATTEMPTS = otp.OTP_TTL, otp.COOLDOWN_TTL, otp.MAX_ATTEMPTS


class PhoneInput(serializers.Serializer):
    phone = serializers.CharField(max_length=32)

    def validate_phone(self, value):
        digits = re.sub(r"[^0-9]", "", value)
        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        if not re.fullmatch(r"[1-9][0-9]{9,14}", digits):
            raise serializers.ValidationError("Укажите корректный номер телефона.")
        return digits


class VerifyInput(PhoneInput):
    code = serializers.RegexField(r"^[0-9]{4}$")


def user_data(user):
    profile = UserProfile.objects.filter(user=user).first()
    roles = list(memberships(user).values_list("role", flat=True).distinct())
    if user.is_superuser:
        roles = ["team_lead", "finance", "admin"]
    elif integration_allowed(user):
        roles.append("admin")
    if is_client(user):
        roles.append("client")
    return {
        "id": user.id,
        "username": user.username,
        "phone": user.username,
        "name": profile.full_name if profile else user.first_name or user.username,
        "roles": roles,
    }


def auth_response(request, session, access, refresh):
    response = Response(
        {
            "access": access,
            "user": user_data(session.user),
            "session_id": session.id,
            "csrf_token": get_token(request._request),
        }
    )
    response.set_cookie(
        settings.AUTH_REFRESH_COOKIE,
        refresh,
        max_age=30 * 86400,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite="Lax",
        path="/api/auth/",
    )
    return response


def require_csrf(request):
    raw = request._request
    reason = CsrfViewMiddleware(lambda r: None).process_view(
        raw, lambda r: None, (), {}
    )
    if reason:
        raise PermissionDenied("Проверка CSRF не пройдена.")


class SendVerificationCodeView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        data = PhoneInput(data=request.data)
        data.is_valid(raise_exception=True)
        phone = data.validated_data["phone"]
        try:
            delivery_id = otp.issue(phone, request.META.get("REMOTE_ADDR", "unknown"))
        except Exception:
            raise Unavailable("Отправка кода временно недоступна.")
        if not delivery_id:
            raise Throttled(wait=COOLDOWN_TTL)
        user = User.objects.filter(username=phone, is_active=True).first()
        if user and has_access(user, invited=True):
            OutboxEvent.objects.create(
                event_type="otp",
                deduplication_key=f"otp:{delivery_id}",
                payload={"user_id": user.id, "delivery_id": delivery_id},
            )
        return Response(
            {
                "message": "Если номеру предоставлен доступ, код будет отправлен.",
                "expires_in": OTP_TTL,
                "cooldown": COOLDOWN_TTL,
            }
        )


class PublicAuthView(APIView):
    def get_authenticate_header(self, request):
        return "Bearer"


class VerifyCodeView(PublicAuthView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        data = VerifyInput(data=request.data)
        data.is_valid(raise_exception=True)
        phone, code = data.validated_data["phone"], data.validated_data["code"]
        try:
            valid = otp.verify(phone, code)
        except Exception:
            raise Unavailable()
        user = User.objects.filter(username=phone, is_active=True).first()
        if not valid or not user or not has_access(user, invited=True):
            raise AuthenticationFailed("Код недействителен или доступ не предоставлен.")
        with transaction.atomic():
            TeamMembership.objects.filter(user=user, status="invited").filter(
                Q(invited_until__isnull=True) | Q(invited_until__gt=timezone.now())
            ).update(status="active")
            ClientProjectAccess.objects.filter(user=user, status="invited").update(
                status="active"
            )
            UserProfile.objects.get_or_create(
                user=user, defaults={"full_name": user.first_name, "phone": phone}
            )
            session, access, refresh = create_session(user, request)
        return auth_response(request, session, access, refresh)


class RefreshView(PublicAuthView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        return Response(
            {
                "csrf_token": get_token(request._request),
                "has_session": bool(request.COOKIES.get(settings.AUTH_REFRESH_COOKIE)),
            }
        )

    def post(self, request):
        require_csrf(request)
        encoded = request.COOKIES.get(settings.AUTH_REFRESH_COOKIE)
        if not encoded:
            raise AuthenticationFailed("Требуется вход.")
        session, access, refresh = rotate_refresh(encoded)
        return auth_response(request, session, access, refresh)


class LogoutView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        require_csrf(request)
        from rest_framework_simplejwt.tokens import RefreshToken

        try:
            token = RefreshToken(request.COOKIES.get(settings.AUTH_REFRESH_COOKIE, ""))
            AuthSession.objects.filter(
                id=token.get("sid"), user_id=token.get("user_id")
            ).update(revoked_at=timezone.now())
        except Exception:
            pass
        response = Response({"message": "Сессия завершена."})
        response.delete_cookie(settings.AUTH_REFRESH_COOKIE, path="/api/auth/")
        return response


class SessionsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            list(
                AuthSession.objects.filter(
                    user=request.user,
                    revoked_at__isnull=True,
                    expires_at__gt=timezone.now(),
                ).values("id", "device", "created_at", "last_used_at", "expires_at")
            )
        )

    def post(self, request, pk=None):
        qs = AuthSession.objects.filter(user=request.user)
        if pk is not None:
            qs = qs.filter(pk=pk)
        qs.update(revoked_at=timezone.now())
        return Response({"message": "Сессии завершены."})


class CurrentUserView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(user_data(request.user))


class SendWhatsAppView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .notification_views import dispatch

        return dispatch(request, force_whatsapp=True)
