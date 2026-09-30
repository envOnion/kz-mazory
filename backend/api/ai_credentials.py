"""Authenticated storage codec for AI-provider credentials.

The provider key is never included in errors or logs.  The small, versioned
envelope binds every ciphertext to its intended channel so a chat credential
cannot accidentally be reused for embeddings (or vice versa).
"""

import json

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.views.decorators.debug import sensitive_variables

CIPHERTEXT_PREFIX = "fernet:v1:"
VALID_PURPOSES = frozenset({"chat", "embedding"})
OPENAI_COMPATIBLE = "openai_compatible"
ANTHROPIC_MESSAGES = "anthropic_messages"
VALID_CHAT_FORMATS = frozenset({OPENAI_COMPATIBLE, ANTHROPIC_MESSAGES})


class CredentialError(Exception):
    """Safe credential error whose message contains only a stable code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@sensitive_variables("key")
def _fernet(*, decrypting):
    key = getattr(settings, "AI_CREDENTIAL_ENCRYPTION_KEY", "")
    try:
        if not isinstance(key, str) or not key:
            raise ValueError
        return Fernet(key.encode("ascii"))
    except (UnicodeEncodeError, ValueError, TypeError):
        raise CredentialError(
            "credential_decryption_failed"
            if decrypting
            else "credential_encryption_unavailable"
        ) from None


def _valid_purpose(purpose):
    return isinstance(purpose, str) and purpose in VALID_PURPOSES


def _valid_api_format(purpose, api_format):
    if purpose == "embedding":
        return api_format == OPENAI_COMPATIBLE
    return purpose == "chat" and api_format in VALID_CHAT_FORMATS


@sensitive_variables("value", "payload", "token")
def encrypt_credential(value, *, purpose, api_format):
    """Return a purpose- and protocol-bound, versioned Fernet ciphertext."""
    if (
        not _valid_purpose(purpose)
        or not _valid_api_format(purpose, api_format)
        or not isinstance(value, str)
        or not value
    ):
        raise CredentialError("credential_encryption_failed")
    payload = json.dumps(
        {"api_format": api_format, "purpose": purpose, "value": value},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    token = _fernet(decrypting=False).encrypt(payload).decode("ascii")
    return f"{CIPHERTEXT_PREFIX}{token}"


@sensitive_variables("ciphertext", "token", "raw", "envelope")
def decrypt_credential(ciphertext, *, purpose, api_format):
    """Decrypt one credential, failing closed on every format mismatch."""
    if (
        not _valid_purpose(purpose)
        or not _valid_api_format(purpose, api_format)
        or not isinstance(ciphertext, str)
        or not ciphertext.startswith(CIPHERTEXT_PREFIX)
    ):
        raise CredentialError("credential_decryption_failed")
    token = ciphertext[len(CIPHERTEXT_PREFIX) :]
    if not token:
        raise CredentialError("credential_decryption_failed")
    try:
        raw = _fernet(decrypting=True).decrypt(token.encode("ascii"))
        envelope = json.loads(raw.decode("utf-8"))
    except (
        CredentialError,
        InvalidToken,
        UnicodeDecodeError,
        UnicodeEncodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        raise CredentialError("credential_decryption_failed") from None
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"api_format", "purpose", "value"}
        or envelope.get("api_format") != api_format
        or envelope.get("purpose") != purpose
        or not isinstance(envelope.get("value"), str)
        or not envelope["value"]
    ):
        raise CredentialError("credential_decryption_failed")
    return envelope["value"]
