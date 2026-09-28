"""Isolated integration fixture. Never included in production routes or real delivery."""

import hashlib
import json
import re
import threading
import time
from urllib.parse import unquote, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

messages = []
waha_sessions = {}
waha_requests = []
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, data):
        encoded = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path == "/test/waha":
            with lock:
                return self.reply(200, {"requests": list(waha_requests)})
        if self.waha_request("GET"):
            return
        if self.path == "/test/messages":
            with lock:
                return self.reply(200, {"messages": list(messages)})
        if self.path.startswith("/api/"):
            return self.reply(200, {"status": "WORKING", "value": "test-qr"})
        self.reply(200, {"status": "test-fixture"})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path in ("/ocr", "/transcribe"):
            return self.reply(
                200, {"text": "Изолированный тестовый документ без финансовых фактов."}
            )
        data = json.loads(raw or b"{}")
        if self.path == "/test/waha":
            with lock:
                waha_sessions[data["name"]] = {
                    "status": "STOPPED",
                    "authenticated": True,
                    **data,
                }
            return self.reply(200, {"status": "configured"})
        if self.waha_request("POST"):
            return
        if self.path == "/api/sendText":
            with lock:
                item = {**data, "id": f"test-message-{len(messages) + 1}"}
                messages.append(item)
            return self.reply(200, {"id": item["id"]})
        if self.path.endswith("/embeddings"):
            digest = hashlib.sha256(data["input"].encode()).digest()
            return self.reply(
                200, {"data": [{"embedding": [(n - 128) / 128 for n in digest[:8]]}]}
            )
        if self.path.endswith("/chat/completions"):
            value = json.loads(data["messages"][-1]["content"])
            content = value.get("content", value.get("question", ""))
            if "SIMULATE_AI_FAILURE" in content:
                return self.reply(503, {"error": "fixture_unavailable"})
            if "content" not in value:
                return self.reply(
                    200,
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": "Ответ по разрешённым источникам тестового провайдера."
                                }
                            }
                        ]
                    },
                )
            facts = []
            if content.startswith("E2E payment:"):
                amount = re.search(r"amount=([0-9.]+)", content).group(1)
                day = re.search(r"date=([0-9-]+)", content).group(1)
                facts = [
                    {
                        "fact_type": "payment",
                        "object_name": "E2E Alpha",
                        "currency": "KZT",
                        "amount": amount,
                        "payment_date": day,
                        "payment_kind": "increment",
                        "evidence": content,
                        "confidence": 0.98,
                        "uncertainties": [],
                    }
                ]
            return self.reply(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"facts": facts}, ensure_ascii=False
                                )
                            }
                        }
                    ]
                },
            )
        self.reply(404, {"error": "Unknown test route"})

    def waha_request(self, method):
        path = urlsplit(self.path).path
        session_route = re.fullmatch(
            r"/api/sessions/([^/]+)(?:/(start|restart|stop|logout))?", path
        )
        qr_route = re.fullmatch(r"/api/([^/]+)/auth/qr", path)
        if not session_route and not qr_route:
            return False
        name = unquote((session_route or qr_route).group(1))
        action = (session_route.group(2) or "status") if session_route else "qr"
        if self.headers.get("X-Api-Key") != "isolated-test-provider":
            self.reply(403, {"error": "bad_test_key"})
            return True
        with lock:
            waha_requests.append(
                {"method": method, "path": self.path, "name": name, "action": action}
            )
            session = waha_sessions.get(name)
            if session is None:
                self.reply(404, {"error": "session_missing"})
                return True
            fault = session.get("faults", {}).get(action, {})
            if fault.get("http_status"):
                self.reply(fault["http_status"], {"error": "fixture_rejection"})
                return True
            if method == "POST" and action in ("start", "restart", "stop", "logout"):
                if action == "logout":
                    session["authenticated"] = False
                session["status"] = (
                    ("WORKING" if session["authenticated"] else "SCAN_QR_CODE")
                    if action in ("start", "restart")
                    else "STOPPED"
                )
            result = {
                "name": name,
                "status": session["status"],
                "me": {"id": "fixture@c.us", "pushName": "E2E WhatsApp"}
                if session["status"] == "WORKING"
                else None,
            }
            if action == "qr":
                result = {"value": "isolated-whatsapp-qr:" + name}
        if fault.get("disconnect"):
            self.close_connection = True
            return True
        time.sleep(fault.get("delay", 0))
        try:
            self.reply(200, result)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
