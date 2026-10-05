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


class McpTokenAuthentication(JWTAuthentication):
    """Authenticates agents via dedicated McpToken (Bearer mcp_... or X-API-Key)."""

    def authenticate(self, request):
        from rest_framework.authentication import get_authorization_header
        from .models import McpToken

        auth_header = get_authorization_header(request).split()
        token_str = None
        if auth_header and auth_header[0].lower() in (b"bearer", b"token"):
            if len(auth_header) == 2:
                try:
                    token_str = auth_header[1].decode()
                except UnicodeDecodeError:
                    return None
        if not token_str:
            token_str = request.META.get("HTTP_X_API_KEY")

        if not token_str:
            return None

        thash = token_hash(token_str)
        token_obj = (
            McpToken.objects.filter(token_hash=thash, is_active=True)
            .select_related("user")
            .first()
        )
        if token_obj is None:
            # Let SessionJWTAuthentication try if it's a JWT
            return None

        if token_obj.expires_at and token_obj.expires_at <= timezone.now():
            raise AuthenticationFailed("Срок действия токена MCP истёк.")

        if not has_access(token_obj.user):
            raise AuthenticationFailed("Доступ пользователя отозван.")

        token_obj.last_used_at = timezone.now()
        token_obj.save(update_fields=["last_used_at"])
        return (token_obj.user, token_obj)

    def authenticate_header(self, request):
        return 'Bearer realm="mcp"'


def generate_mcp_token(user, name="default", expires_at=None):
    import secrets
    from .models import McpToken

    raw_token = f"mcp_{secrets.token_urlsafe(32)}"
    token_obj = McpToken.objects.create(
        user=user,
        name=name,
        token_hash=token_hash(raw_token),
        expires_at=expires_at,
        is_active=True,
    )
    return token_obj, raw_token



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
