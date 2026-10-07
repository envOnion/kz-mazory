"""Recoverable personal MCP credentials, disclosed only through the JWT cabinet."""

import secrets
from cryptography.fernet import InvalidToken
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from .authentication import token_hash
from .mfa import encryption
from .models import McpToken


class McpCredentialError(Exception):
    pass


def reveal(token):
    try:
        value = encryption().decrypt(token.token_encrypted.encode()).decode()
        if token_hash(value) != token.token_hash:
            raise McpCredentialError()
        return value
    except (InvalidToken, ValueError, UnicodeError):
        raise McpCredentialError() from None


@transaction.atomic
def ensure_portal_token(user, *, rotate=False):
    # User lock also serializes first issuance before a token row exists.
    User.objects.select_for_update().get(pk=user.pk)
    current = McpToken.objects.filter(user=user, purpose="portal", is_active=True).first()
    if current and not rotate and (current.expires_at is None or current.expires_at > timezone.now()):
        reveal(current)
        return current
    raw = "mcp_" + secrets.token_urlsafe(32)
    try:
        encrypted = encryption().encrypt(raw.encode()).decode()
    except (ValueError, UnicodeError):
        raise McpCredentialError() from None
    if current:
        current.is_active = False
        current.save(update_fields=["is_active"])
    return McpToken.objects.create(user=user, name="Проверка фактов", purpose="portal", token_hash=token_hash(raw), token_encrypted=encrypted)


@transaction.atomic
def revoke_portal_token(user):
    User.objects.select_for_update().get(pk=user.pk)
    McpToken.objects.filter(user=user, purpose="portal", is_active=True).update(is_active=False)
