import hashlib
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import RefreshToken
from .models import AuthSession
from .access import has_access


def token_hash(token):
    return hashlib.sha256(str(token).encode()).hexdigest()


def session_tokens(user, session):
    refresh = RefreshToken.for_user(user)
    refresh["sid"] = session.id
    refresh["user_id"] = user.id
    refresh.set_exp(lifetime=session.expires_at - timezone.now())
    session.refresh_jti_hash = token_hash(refresh["jti"])
    session.last_used_at = timezone.now()
    session.save(update_fields=["refresh_jti_hash", "last_used_at"])
    return str(refresh.access_token), str(refresh)


def create_session(user, request=None):
    if not has_access(user):
        raise AuthenticationFailed("Доступ не предоставлен.")
    session = AuthSession.objects.create(
        user=user,
        expires_at=timezone.now() + settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"],
        device=(request.META.get("HTTP_USER_AGENT", "")[:256] if request else ""),
        ip_address=(request.META.get("REMOTE_ADDR") or None) if request else None,
    )
    access, refresh = session_tokens(user, session)
    return session, access, refresh


class SessionJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        sid = validated_token.get("sid")
        if not isinstance(sid, int) or isinstance(sid, bool):
            raise AuthenticationFailed("Требуется повторный вход.")
        session = AuthSession.objects.filter(
            id=sid, user=user, revoked_at__isnull=True, expires_at__gt=timezone.now()
        ).first()
        if session is None or not has_access(user):
            raise AuthenticationFailed("Сессия завершена или доступ отозван.")
        return user


def rotate_refresh(encoded):
    try:
        token = RefreshToken(encoded)
        sid = token.get("sid")
    except Exception:
        raise AuthenticationFailed("Требуется повторный вход.")
    if not isinstance(sid, int):
        raise AuthenticationFailed("Требуется повторный вход.")
    invalid = False
    result = None
    with transaction.atomic():
        session = (
            AuthSession.objects.select_for_update()
            .select_related("user")
            .filter(id=sid)
            .first()
        )
        if (
            not session
            or session.revoked_at
            or session.expires_at <= timezone.now()
            or not has_access(session.user)
            or token.get("user_id") != session.user_id
        ):
            invalid = True
        elif session.refresh_jti_hash != token_hash(token["jti"]):
            session.revoked_at = timezone.now()
            session.save(update_fields=["revoked_at"])
            invalid = True
        else:
            result = (session, *session_tokens(session.user, session))
    if invalid:
        raise AuthenticationFailed("Сессия завершена.")
    return result
