"""Optional, per-administrator TOTP with encrypted secrets and replay protection."""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, unquote, urlencode, urlsplit

import qrcode
from qrcode.image.svg import SvgPathFillImage
from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.shortcuts import render, redirect
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.utils.crypto import salted_hmac
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from .models import AdminMFA

SECURITY_URL = "/admin/security/"
CHALLENGE_URL = "/admin/security/mfa/"
INVALID_CODE = "Неверный или уже использованный код."
ATTEMPT_LIMIT = "Слишком много попыток. Повторите через пять минут."


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


def fingerprint(entry):
    return salted_hmac(
        "admin-mfa-session",
        f"{entry.user_id}:{entry.encrypted_secret}",
        algorithm="sha256",
    ).hexdigest()


def verified(request, entry):
    return secrets.compare_digest(
        request.session.get("admin_mfa", ""), fingerprint(entry)
    )


def safe_next(value):
    decoded = unquote(value or "")
    try:
        path = urlsplit(decoded).path
    except ValueError:
        return "/admin/"
    if (
        value
        and "\\" not in decoded
        and not any(part in (".", "..") for part in path.split("/"))
        and "\\" not in value
        and url_has_allowed_host_and_scheme(value, allowed_hosts=set())
        and urlsplit(value).path.startswith("/admin/")
        and urlsplit(value).path
        not in (CHALLENGE_URL, "/admin/login/", "/admin/logout/")
    ):
        return value
    return "/admin/"


def challenge_redirect(target):
    return redirect(f"{CHALLENGE_URL}?{urlencode({'next': safe_next(target)})}")


def attempt_allowed(user_id):
    # All code/password attempts share a limit, including disable and enrollment.
    key = f"admin-mfa:{user_id}"
    if cache.add(key, 1, 300):
        return True
    try:
        return cache.incr(key) <= 10
    except ValueError:  # The key may expire between add() and incr().
        return cache.add(key, 1, 300)


def matching_counter(entry, code):
    if len(code) != 6 or not code.isascii() or not code.isdecimal():
        return None
    secret = encryption().decrypt(entry.encrypted_secret.encode()).decode()
    now_counter = int(time.time()) // 30
    return next(
        (
            n
            for n in range(now_counter - 1, now_counter + 2)
            if n > entry.last_counter and hmac.compare_digest(totp(secret, n), code)
        ),
        None,
    )


def confirm_session(request, entry):
    request.session.cycle_key()
    request.session["admin_mfa"] = fingerprint(entry)
    request.session.pop("admin_mfa_pending", None)
    request.session.pop("mfa_user_id", None)


def render_page(request, **context):
    return render(
        request, "admin/mfa.html", {**admin.site.each_context(request), **context}
    )


class AdminMFAMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.path.startswith("/admin/")
            and request.user.is_authenticated
            and request.user.is_staff
            and request.path not in (CHALLENGE_URL, "/admin/logout/")
        ):
            entry = AdminMFA.objects.filter(
                user=request.user, confirmed_at__isnull=False
            ).first()
            if entry and not verified(request, entry):
                response = challenge_redirect(request.get_full_path())
                # The gate also protects direct access to the settings page.
                add_never_cache_headers(response)
                return response
        return self.get_response(request)


@never_cache
@sensitive_post_parameters("code", "password")
@staff_member_required
@require_http_methods(["GET", "POST"])
def security_view(request):
    error = ""
    if request.method == "POST":
        with transaction.atomic():
            # Serialize enrollment too, when the one-to-one MFA row does not exist yet.
            user = get_user_model().objects.select_for_update().get(pk=request.user.pk)
            entry = AdminMFA.objects.select_for_update().filter(user=user).first()
            if entry and entry.confirmed_at and not verified(request, entry):
                return challenge_redirect(SECURITY_URL)
            action = request.POST.get("action")
            pending = (
                entry
                and not entry.confirmed_at
                and request.session.get("admin_mfa_pending") == fingerprint(entry)
            )
            if action == "start" and not (entry and entry.confirmed_at):
                entry, _ = AdminMFA.objects.update_or_create(
                    user=user,
                    defaults={
                        "encrypted_secret": encryption()
                        .encrypt(base64.b32encode(secrets.token_bytes(20)))
                        .decode(),
                        "confirmed_at": None,
                        "last_counter": -1,
                    },
                )
                request.session["admin_mfa_pending"] = fingerprint(entry)
                return redirect(SECURITY_URL)
            if action == "cancel" and pending:
                entry.delete()
                request.session.pop("admin_mfa_pending", None)
                return redirect(SECURITY_URL)
            if action == "confirm" and pending:
                if not attempt_allowed(user.pk):
                    error = ATTEMPT_LIMIT
                else:
                    counter = matching_counter(entry, request.POST.get("code", ""))
                    if counter is not None:
                        entry.confirmed_at = timezone.now()
                        entry.last_counter = counter
                        entry.save(update_fields=["confirmed_at", "last_counter"])
                        confirm_session(request, entry)
                        messages.success(
                            request,
                            "MFA включена. При следующем входе потребуется код из приложения.",
                        )
                        return redirect(SECURITY_URL)
                    error = INVALID_CODE
            elif action == "disable" and entry and entry.confirmed_at:
                if not attempt_allowed(user.pk):
                    error = ATTEMPT_LIMIT
                else:
                    counter = matching_counter(entry, request.POST.get("code", ""))
                    if (
                        user.check_password(request.POST.get("password", ""))
                        and counter is not None
                    ):
                        entry.delete()
                        request.session.pop("admin_mfa", None)
                        request.session.pop("admin_mfa_pending", None)
                        request.session.cycle_key()
                        messages.success(request, "MFA отключена.")
                        return redirect(SECURITY_URL)
                    error = "Неверный пароль или неверный / уже использованный код."
            else:
                error = (
                    error
                    or "Настройка изменилась. Обновите страницу и повторите действие."
                )

    entry = AdminMFA.objects.filter(user=request.user).first()
    enabled = bool(entry and entry.confirmed_at)
    if enabled and not verified(request, entry):
        return challenge_redirect(SECURITY_URL)
    # Only the session which explicitly started setup may see its pending secret.
    pending = (
        entry
        and not enabled
        and request.session.get("admin_mfa_pending") == fingerprint(entry)
    )
    context = {"title": "Безопасность", "enabled": enabled, "error": error}
    if pending:
        secret = encryption().decrypt(entry.encrypted_secret.encode()).decode()
        uri = f"otpauth://totp/{quote('Mazory:' + request.user.username, safe='')}?{urlencode({'secret': secret, 'issuer': 'Mazory'})}"
        qr = qrcode.make(uri, image_factory=SvgPathFillImage).to_string()
        context.update(
            secret=secret,
            qr_image="data:image/svg+xml;base64," + base64.b64encode(qr).decode(),
        )
    return render_page(request, **context)


@never_cache
@sensitive_post_parameters("code")
@staff_member_required
@require_http_methods(["GET", "POST"])
def mfa_view(request):
    target = safe_next(request.POST.get("next") or request.GET.get("next"))
    error = ""
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        entry = AdminMFA.objects.select_for_update().filter(user=request.user).first()
        if not entry or not entry.confirmed_at or verified(request, entry):
            return redirect(target)
        if request.method == "POST":
            if not attempt_allowed(request.user.pk):
                error = ATTEMPT_LIMIT
            else:
                counter = matching_counter(entry, request.POST.get("code", ""))
                if counter is not None:
                    entry.last_counter = counter
                    entry.save(update_fields=["last_counter"])
                    confirm_session(request, entry)
                    return redirect(target)
                error = INVALID_CODE
    return render_page(
        request, title="Подтверждение входа", challenge=True, next=target, error=error
    )
