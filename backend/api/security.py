import logging
import re
import uuid
import ipaddress
from django.conf import settings
from django.http import JsonResponse
from rest_framework.exceptions import APIException


class Conflict(APIException):
    status_code = 409
    default_detail = "Данные изменились. Обновите карточку и повторите проверку."
    default_code = "version_conflict"


class Unavailable(APIException):
    status_code = 503
    default_detail = "Сервис временно недоступен."
    default_code = "unavailable"


def api_exception_handler(exc, context):
    from rest_framework.views import exception_handler

    response = exception_handler(exc, context)
    request = context.get("request")
    if response is None:
        logging.getLogger(__name__).error(
            "Unhandled API error type=%s correlation=%s",
            type(exc).__name__,
            getattr(request, "correlation_id", ""),
        )
        return __import__("rest_framework.response", fromlist=["Response"]).Response(
            {
                "error": "Не удалось выполнить запрос.",
                "code": "internal_error",
                "correlation_id": getattr(request, "correlation_id", ""),
            },
            status=500,
        )
    data = response.data
    detail = data.get("detail") if isinstance(data, dict) else None
    response.data = {
        "error": str(detail) if detail else "Проверьте введенные данные.",
        "code": getattr(exc, "default_code", "validation_error"),
        "correlation_id": getattr(request, "correlation_id", ""),
    }
    if detail is None:
        response.data["fields"] = data
    return response


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.correlation_id = uuid.uuid4().hex
        try:
            peer = ipaddress.ip_address(request.META.get("REMOTE_ADDR", ""))
            if any(
                peer in ipaddress.ip_network(network)
                for network in settings.TRUSTED_PROXY_CIDRS
            ):
                request.META["REMOTE_ADDR"] = str(
                    ipaddress.ip_address(request.META.get("HTTP_X_REAL_IP", ""))
                )
        except ValueError:
            pass
        limit = (
            settings.MAX_ATTACHMENT_SIZE + 65536
            if request.path == "/api/attachments/"
            else settings.DATA_UPLOAD_MAX_MEMORY_SIZE
        )
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            return JsonResponse({"error": "Некорректный размер запроса."}, status=400)
        if length > limit:
            return JsonResponse(
                {"error": "Запрос слишком большой.", "code": "body_too_large"},
                status=413,
            )
        response = self.get_response(request)
        response["X-Request-ID"] = request.correlation_id
        response["Referrer-Policy"] = "same-origin"
        response["X-Content-Type-Options"] = "nosniff"
        if settings.MAZORY_ENV == "production":
            # Unfold uses inline styles/scripts; external frames/objects are never allowed.
            response["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
            )
        return response


class RedactFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        message = re.sub(
            r"(?i)(bearer|x-api-key|authorization)([: =]+)[^\s,]+",
            r"\1\2[redacted]",
            message,
        )
        message = re.sub(r"/rest/\d+/[^/\s]+/", "/rest/[redacted]/", message)
        message = re.sub(r"(?<!\w)\+?\d{10,15}(?!\w)", "[phone]", message)
        record.msg, record.args = message, ()
        return True
