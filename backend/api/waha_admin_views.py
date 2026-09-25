import base64
import requests
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render, redirect
from django.contrib import messages
from django.views.decorators.http import require_POST
from api.models import WhatsAppConfig

def get_waha_headers(config):
    headers = {"Content-Type": "application/json"}
    if config.waha_api_key:
        headers["X-Api-Key"] = config.waha_api_key
    return headers

@staff_member_required
def waha_dashboard_view(request):
    config = WhatsAppConfig.get_active()
    headers = get_waha_headers(config)
    session = config.session_name or "default"
    base_url = config.waha_api_url.rstrip("/")
    
    status = "STOPPED"
    qr_image = None
    me = None
    chats = []
    
    try:
        # Check session status
        res = requests.get(f"{base_url}/api/sessions/{session}", headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            status = data.get("status", "STOPPED")
            config.status = status
            config.save(update_fields=["status"])
        elif res.status_code == 404:
            status = "STOPPED"
        else:
            status = f"HTTP_{res.status_code}"
    except Exception as e:
        status = "CONNECTION_ERROR"
    
    # If waiting for QR, fetch QR
    if status == "SCAN_QR_CODE":
        try:
            qr_res = requests.get(f"{base_url}/api/{session}/auth/qr", headers=headers, timeout=5)
            if qr_res.status_code == 200:
                # Can be raw image PNG or JSON with qr
                content_type = qr_res.headers.get("content-type", "")
                if "image" in content_type:
                    b64 = base64.b64encode(qr_res.content).decode("utf-8")
                    qr_image = f"data:image/png;base64,{b64}"
                else:
                    qr_data = qr_res.json()
                    raw = qr_data.get("qr") or qr_data.get("data")
                    if raw:
                        if raw.startswith("data:"):
                            qr_image = raw
                        else:
                            qr_image = f"data:image/png;base64,{raw}"
        except Exception:
            pass

    # If working, fetch Me and Chats
    if status == "WORKING":
        try:
            me_res = requests.get(f"{base_url}/api/{session}/me", headers=headers, timeout=5)
            if me_res.status_code == 200:
                me = me_res.json()
        except Exception:
            pass

        try:
            chats_res = requests.get(f"{base_url}/api/{session}/chats", headers=headers, timeout=5)
            if chats_res.status_code == 200:
                chats = chats_res.json()
                if isinstance(chats, list):
                    # Sort groups first
                    chats.sort(key=lambda c: (not str(c.get("id", "")).endswith("@g.us"), c.get("name", "")))
        except Exception:
            pass

    return render(request, "admin/waha_dashboard.html", {
        "config": config,
        "status": status,
        "qr_image": qr_image,
        "me": me,
        "chats": chats[:30],
    })

@staff_member_required
@require_POST
def waha_action_view(request, action):
    config = WhatsAppConfig.get_active()
    headers = get_waha_headers(config)
    session = config.session_name or "default"
    base_url = config.waha_api_url.rstrip("/")

    url_map = {
        "start": f"{base_url}/api/sessions/start",
        "stop": f"{base_url}/api/sessions/stop",
        "restart": f"{base_url}/api/sessions/restart",
    }

    target_url = url_map.get(action)
    if not target_url:
        messages.error(request, f"Неизвестное действие: {action}")
        return redirect("admin:waha_dashboard")

    try:
        res = requests.post(target_url, json={"name": session}, headers=headers, timeout=10)
        if res.status_code in (200, 201):
            messages.success(request, f"Команда '{action}' успешно отправлена в WAHA.")
        else:
            messages.error(request, f"Ошибка WAHA ({res.status_code}): {res.text}")
    except Exception as e:
        messages.error(request, f"Не удалось связаться с WAHA: {e}")

    return redirect("admin:waha_dashboard")
