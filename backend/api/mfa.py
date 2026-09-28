"""TOTP gate for Django Admin; RFC 6238, encrypted at rest, counter replay protection."""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote
from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.core.cache import cache
from django.db import transaction
from django.shortcuts import render, redirect
from django.utils import timezone
from .models import AdminMFA


def encryption():
    key = (
        settings.MFA_ENCRYPTION_KEY
        or base64.urlsafe_b64encode(
            hashlib.sha256(settings.SECRET_KEY.encode()).digest()
        ).decode()
    )
    return Fernet(key.encode())


def totp(secret, counter):
    key = base64.b32decode(secret, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1000000:06d}"


class AdminMFAMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            settings.ADMIN_MFA_REQUIRED
            and request.path.startswith("/admin/")
            and request.user.is_authenticated
            and request.user.is_staff
            and request.path not in ("/admin/security/mfa/", "/admin/logout/")
            and request.session.get("mfa_user_id") != request.user.id
        ):
            return redirect("/admin/security/mfa/")
        return self.get_response(request)


@staff_member_required
def mfa_view(request):
    entry, _ = AdminMFA.objects.get_or_create(
        user=request.user,
        defaults={
            "encrypted_secret": encryption()
            .encrypt(base64.b32encode(secrets.token_bytes(20)))
            .decode()
        },
    )
    secret = encryption().decrypt(entry.encrypted_secret.encode()).decode()
    error = ""
    if request.method == "POST":
        key = f"admin-mfa:{request.user.id}"
        if not cache.add(key, 1, 300):
            attempts = cache.incr(key)
        else:
            attempts = 1
        if attempts > 10:
            error = "Слишком много попыток. Повторите через пять минут."
        else:
            code = request.POST.get("code", "")
            with transaction.atomic():
                entry = AdminMFA.objects.select_for_update().get(pk=entry.pk)
                now_counter = int(time.time()) // 30
                matched = next(
                    (
                        n
                        for n in range(now_counter - 1, now_counter + 2)
                        if n > entry.last_counter
                        and hmac.compare_digest(totp(secret, n), code)
                    ),
                    None,
                )
                if matched is not None:
                    entry.last_counter = matched
                    entry.confirmed_at = entry.confirmed_at or timezone.now()
                    entry.save()
                    request.session.cycle_key()
                    request.session["mfa_user_id"] = request.user.id
                    return redirect("/admin/")
                error = "Неверный или уже использованный код."
    uri = f"otpauth://totp/{quote('Mazory:' + request.user.username)}?secret={secret}&issuer=Mazory"
    return render(
        request,
        "admin/mfa.html",
        {
            "secret": secret if not entry.confirmed_at else "",
            "uri": uri if not entry.confirmed_at else "",
            "error": error,
        },
    )
