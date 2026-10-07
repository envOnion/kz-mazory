"""Streamable HTTP MCP Views and token management for Agent LLM."""

import asyncio
import datetime
import os
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import generate_mcp_token, McpTokenAuthentication, SessionJWTAuthentication
from .mcp_fact_review import create_fact_review_mcp
from .models import McpToken
from .mcp_tokens import McpCredentialError, ensure_portal_token, reveal, revoke_portal_token


def run_mcp_asgi(app, session_manager, request):
    """Bridges Django WSGI HttpRequest to MCP Starlette ASGI Application."""
    headers = []
    for k, v in request.META.items():
        if k.startswith("HTTP_"):
            header_name = k[5:].replace("_", "-").lower().encode("latin-1")
            headers.append((header_name, v.encode("latin-1")))
        elif k in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            header_name = k.replace("_", "-").lower().encode("latin-1")
            headers.append((header_name, v.encode("latin-1")))

    has_host = any(h[0] == b"host" for h in headers)
    if not has_host:
        host = request.get_host() if hasattr(request, "get_host") else "localhost"
        headers.append((b"host", host.encode("latin-1")))

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": request.method,
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": request.META.get("QUERY_STRING", "").encode("latin-1"),
        "headers": headers,
        "client": (request.META.get("REMOTE_ADDR", "127.0.0.1"), 0),
        "server": ("localhost", 8000),
        "scheme": "https" if request.is_secure() else "http",
    }

    body_bytes = request.body
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    response_start = None
    response_bodies = []

    async def send(message):
        nonlocal response_start
        if message["type"] == "http.response.start":
            response_start = message
        elif message["type"] == "http.response.body":
            response_bodies.append(message.get("body", b""))

    from django.db import connections

    # Capture open database connections from the synchronous request thread
    outer_conns = {alias: connections[alias] for alias in connections}

    async def run():
        # Inject the outer connections into the async task contextvars
        for alias, conn in outer_conns.items():
            connections[alias] = conn
        async with session_manager.run():
            await app(scope, receive, send)

    old_allow = os.environ.get("DJANGO_ALLOW_ASYNC_UNSAFE")
    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
    try:
        asyncio.run(run())
    finally:
        if old_allow is None:
            os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)
        else:
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = old_allow

    status_code = response_start["status"] if response_start else 200
    django_response = HttpResponse(b"".join(response_bodies), status=status_code)
    if response_start:
        for header_name, header_val in response_start.get("headers", []):
            name = header_name.decode("latin-1")
            val = header_val.decode("latin-1")
            if name.lower() not in ("content-length", "transfer-encoding"):
                django_response[name] = val

    return django_response


class FactReviewMcpView(APIView):
    """Streamable HTTP endpoint for Fact Review MCP protocol."""

    permission_classes = [IsAuthenticated]
    authentication_classes = [McpTokenAuthentication, SessionJWTAuthentication]

    def post(self, request, *args, **kwargs):
        mcp = create_fact_review_mcp(request.user)
        app = mcp.streamable_http_app()
        return run_mcp_asgi(app, mcp.session_manager, request)

    def get(self, request, *args, **kwargs):
        mcp = create_fact_review_mcp(request.user)
        app = mcp.streamable_http_app()
        return run_mcp_asgi(app, mcp.session_manager, request)


class CreateMcpTokenSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=128, required=False, default="Fact Review Agent")
    expires_in_days = serializers.IntegerField(min_value=1, max_value=365, required=False, allow_null=True)


class McpConnectionAction(serializers.Serializer):
    action = serializers.ChoiceField(choices=["create", "rotate", "revoke"])


class McpConnectionView(APIView):
    permission_classes = [IsAuthenticated]
    authentication_classes = [SessionJWTAuthentication]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response

    def get(self, request):
        token = McpToken.objects.filter(user=request.user, purpose="portal", is_active=True).first()
        if not token:
            return Response({"connection": None})
        try:
            raw = reveal(token)
        except McpCredentialError:
            return Response({"error": "Токен MCP недоступен. Перевыпустите его; при повторной ошибке обратитесь к администратору."}, status=503)
        return Response({"connection": {"id": token.id, "token": raw, "created_at": token.created_at.isoformat(), "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None, "expires_at": token.expires_at.isoformat() if token.expires_at else None}})

    def post(self, request):
        schema = McpConnectionAction(data=request.data)
        schema.is_valid(raise_exception=True)
        action = schema.validated_data["action"]
        if action == "revoke":
            revoke_portal_token(request.user)
        else:
            try:
                ensure_portal_token(request.user, rotate=action == "rotate")
            except McpCredentialError:
                return Response({"error": "Не удалось защитить токен MCP. Обратитесь к администратору."}, status=503)
        return self.get(request)


class McpTokenView(APIView):
    """Manage dedicated MCP tokens for external Agent LLMs."""

    permission_classes = [IsAuthenticated]
    authentication_classes = [SessionJWTAuthentication]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response

    def get(self, request):
        tokens = McpToken.objects.filter(user=request.user, is_active=True).order_by("-created_at")
        items = [
            {
                "id": t.id,
                "name": t.name,
                "created_at": t.created_at.isoformat(),
                "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
                "expires_at": t.expires_at.isoformat() if t.expires_at else None,
                "is_active": t.is_active,
            }
            for t in tokens
        ]
        return Response({"tokens": items})

    def post(self, request):
        serializer = CreateMcpTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        expires_at = None
        if data.get("expires_in_days"):
            expires_at = timezone.now() + datetime.timedelta(days=data["expires_in_days"])

        token_obj, raw_token = generate_mcp_token(
            user=request.user,
            name=data["name"],
            expires_at=expires_at,
        )

        return Response(
            {
                "id": token_obj.id,
                "name": token_obj.name,
                "token": raw_token,
                "created_at": token_obj.created_at.isoformat(),
                "expires_at": token_obj.expires_at.isoformat() if token_obj.expires_at else None,
                "hint": "Сохраните токен сейчас. В открытом виде он больше не будет показан.",
            },
            status=status.HTTP_201_CREATED,
        )

    def delete(self, request):
        token_id = request.data.get("token_id")
        if not token_id:
            return Response({"error": "token_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        token_obj = McpToken.objects.filter(id=token_id, user=request.user).first()
        if not token_obj:
            return Response({"error": "Token not found"}, status=status.HTTP_404_NOT_FOUND)
        token_obj.is_active = False
        token_obj.save(update_fields=["is_active"])
        return Response({"status": "revoked"})
