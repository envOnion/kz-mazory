import base64
import time
import requests
from django.conf import settings
from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework import status

WAHA_HEADERS = {"X-Api-Key": getattr(settings, "WAHA_API_KEY", "")}
WAHA_BASE = getattr(settings, "WAHA_API_URL", "http://waha:3000")

class WhatsAppStatusView(APIView):
    """
    Returns live connection status of local WAHA session.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            res = requests.get(f"{WAHA_BASE}/api/sessions/default", headers=WAHA_HEADERS, timeout=5)
            if res.ok:
                default_session = res.json()
                return Response({
                    "status": default_session.get("status", "STOPPED"),
                    "me": default_session.get("me"),
                    "engine": default_session.get("engine", {}).get("engine", "NOWEB"),
                })
            return Response({"status": "STOPPED", "me": None})
        except Exception as e:
            return Response({"status": "OFFLINE", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class WhatsAppQrView(APIView):
    """
    Returns live QR code image for scanning in WhatsApp app.
    Auto-restarts session if it ended with timeout (FAILED/STOPPED).
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        format_type = request.query_params.get("format", "image")
        try:
            # 1. Check current status
            res = requests.get(f"{WAHA_BASE}/api/sessions/default", headers=WAHA_HEADERS, timeout=5)
            cur_status = "STOPPED"
            if res.ok:
                cur_status = res.json().get("status", "STOPPED")
            elif res.status_code == 404:
                requests.post(f"{WAHA_BASE}/api/sessions", json={"name": "default"}, headers=WAHA_HEADERS, timeout=5)
            
            # If session died/failed/stopped, restart it automatically!
            if cur_status in ("FAILED", "STOPPED"):
                requests.post(f"{WAHA_BASE}/api/sessions/default/restart", headers=WAHA_HEADERS, timeout=5)
                # wait briefly for session to enter SCAN_QR_CODE
                for _ in range(6):
                    time.sleep(1)
                    s_res = requests.get(f"{WAHA_BASE}/api/sessions/default", headers=WAHA_HEADERS, timeout=3)
                    if s_res.ok and s_res.json().get("status") == "SCAN_QR_CODE":
                        break

            # 2. Fetch QR
            qr_res = requests.get(f"{WAHA_BASE}/api/default/auth/qr", headers=WAHA_HEADERS, timeout=10)
            if qr_res.ok:
                if format_type == "json":
                    b64 = base64.b64encode(qr_res.content).decode('utf-8')
                    return Response({
                        "status": "SCAN_QR_CODE",
                        "qr_base64": f"data:image/png;base64,{b64}"
                    })
                return HttpResponse(qr_res.content, content_type="image/png")
            else:
                return Response(
                    {"status": "PENDING", "message": "Сессия инициализируется, повторите через 2 секунды"},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE
                )
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class WhatsAppRestartView(APIView):
    """
    Manually restarts WAHA default session.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request):
        try:
            res = requests.post(f"{WAHA_BASE}/api/sessions/default/restart", headers=WAHA_HEADERS, timeout=5)
            return Response({
                "status": "restarting",
                "detail": res.json() if res.ok else res.text
            })
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
