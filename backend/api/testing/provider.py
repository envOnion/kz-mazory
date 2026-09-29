"""Isolated integration fixture. Never included in production routes or real delivery."""

import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

messages = []
waha_sessions = {}
waha_requests = []
crm_requests = []
ai_requests = {"embeddings": [], "chats": [], "payloads": []}
context_options = {}
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
        if self.path == "/test/crm":
            with lock:
                return self.reply(200, {"requests": list(crm_requests)})
        if self.path.endswith("/endpoints"):
            return self.reply(
                200,
                {
                    "data": {
                        "endpoints": [
                            {
                                "tag": "e2e-native",
                                "context_length": context_options.get(
                                    "context_length", 1000000
                                ),
                                "max_completion_tokens": 65536,
                                "max_prompt_tokens": None,
                                "supported_parameters": [
                                    "max_tokens",
                                    "temperature",
                                    "reasoning",
                                    "response_format",
                                ],
                            }
                        ]
                    }
                },
            )
        if self.path == "/test/waha":
            with lock:
                return self.reply(200, {"requests": list(waha_requests)})
        if self.waha_request("GET"):
            return
        if self.path == "/test/messages":
            with lock:
                return self.reply(200, {"messages": list(messages)})
        if self.path == "/test/ai-requests":
            with lock:
                return self.reply(200, ai_requests)
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
        crm_route = re.fullmatch(
            r"/rest/1/(e2e-[A-Za-z0-9-]+)/crm\.deal\.get\.json", self.path
        )
        if crm_route:
            with lock:
                crm_requests.append(
                    {"credential": crm_route.group(1), "deal_id": data["id"]}
                )
            return self.reply(
                200,
                {
                    "result": {
                        "ID": data["id"],
                        "TITLE": "E2E CRM " + data["id"],
                        "OPPORTUNITY": "1000",
                        "CURRENCY_ID": "KZT",
                        "STAGE_ID": "NEW",
                    }
                },
            )
        if self.path == "/test/context-options":
            with lock:
                context_options.clear()
                context_options.update(data)
            return self.reply(200, {"configured": True})
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
            with lock:
                ai_requests["embeddings"].append(data["input"])
            digest = hashlib.sha256(data["input"].encode()).digest()
            return self.reply(
                200, {"data": [{"embedding": [(n - 128) / 128 for n in digest[:8]]}]}
            )
        if self.path.endswith("/chat/completions"):
            value = json.loads(data["messages"][-1]["content"])
            with lock:
                ai_requests["chats"].append(value)
                ai_requests["payloads"].append(data)
            content = value.get("content", value.get("question", ""))
            if content.startswith("E2E extraction:upstream_overloaded"):
                with lock:
                    attempts = sum(
                        item.get("content") == content for item in ai_requests["chats"]
                    )
                if attempts == 1:
                    return self.reply(
                        200,
                        {
                            "error": {
                                "code": 503,
                                "message": "Synthetic upstream overload",
                            }
                        },
                    )
            if "SIMULATE_CONTEXT_OVERFLOW" in content:
                return self.reply(400, {"error": "Maximum context length exceeded"})
            if context_options.get("delay_seconds"):
                time.sleep(min(context_options["delay_seconds"], 30))
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
            finish_reason = "stop"
            if content.startswith("E2E extraction:"):
                facts = [
                    {
                        "fact_type": "project",
                        "object_name": "<img src=x onerror=window.__xss=1>",
                        "evidence": content,
                        "contract_amount": None,
                        "stage": None,
                        "company_name": None,
                        "confidence": None,
                    }
                ]
                if content.startswith("E2E extraction:wrong_evidence"):
                    facts[0]["evidence"] = value["context"][-1]["content"]
                elif content.startswith("E2E extraction:missing_amount"):
                    facts[0].update(fact_type="payment", amount=None)
                elif content.startswith("E2E extraction:truncated"):
                    finish_reason = "length"
                elif any(
                    content.startswith("E2E extraction:" + kind)
                    for kind in (
                        "whitespace_evidence",
                        "ambiguous_evidence",
                        "changed_evidence",
                    )
                ):
                    facts[0]["evidence"] = "Объект «Алматы»: оплачено 125 000 ₸."
                    if content.startswith("E2E extraction:changed_evidence"):
                        facts[0]["evidence"] = "Объект «Алматы»: оплачено 126 000 ₸."
            response_content = json.dumps({"facts": facts}, ensure_ascii=False)
            if content.startswith("E2E extraction:fenced"):
                response_content = "```json\n" + response_content + "\n```"
            elif content.startswith("E2E extraction:invalid_json"):
                response_content = "[]"
            from api.context_tokens import native_counter

            tokens = native_counter().count_payload(data)
            return self.reply(
                200,
                {
                    "usage": {"prompt_tokens": tokens, "completion_tokens": 32},
                    "choices": [
                        {
                            "finish_reason": finish_reason,
                            "message": {"content": response_content},
                        }
                    ],
                },
            )
        self.reply(404, {"error": "Unknown test route"})

    def waha_request(self, method):
        path = urlsplit(self.path).path
        session_route = re.fullmatch(
            r"/api/sessions/([^/]+)(?:/(start|restart|stop|logout))?", path
        )
        qr_route = re.fullmatch(r"/api/([^/]+)/auth/qr", path)
        history_route = re.fullmatch(r"/api/([^/]+)/chats/([^/]+)/messages", path)
        group_route = re.fullmatch(r"/api/([^/]+)/groups/([^/]+)", path)
        groups_route = re.fullmatch(r"/api/([^/]+)/groups", path)
        if (
            not session_route
            and not qr_route
            and not history_route
            and not group_route
            and not groups_route
        ):
            return False
        name = unquote(
            (
                session_route
                or qr_route
                or history_route
                or group_route
                or groups_route
            ).group(1)
        )
        action = (
            (session_route.group(2) or "status")
            if session_route
            else (
                "messages"
                if history_route
                else "group"
                if group_route
                else "groups"
                if groups_route
                else "qr"
            )
        )
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
                "me": {
                    "id": session.get("account_id", "fixture@c.us"),
                    "pushName": "E2E WhatsApp",
                }
                if session["status"] == "WORKING"
                else None,
                "config": session.get(
                    "config", {"noweb": {"store": {"enabled": True, "fullSync": True}}}
                ),
            }
            if action == "qr":
                result = {"value": "isolated-whatsapp-qr:" + name}
            elif action == "groups":
                query = parse_qs(urlsplit(self.path).query)
                offset = int(query.get("offset", [0])[0])
                limit = min(
                    int(query.get("limit", [10])[0]), fault.get("page_cap", 100)
                )
                if fault.get("repeated_page"):
                    offset = 0
                values = sorted(
                    session.get("groups", []), key=lambda group: group["id"]
                )
                result = values[offset : offset + limit]
                if session.get("groups_format") == "map":
                    result = {group["id"]: group for group in result}
                if fault.get("invalid"):
                    result = {"error": "invalid_fixture"}
            elif action == "group":
                result = {
                    "id": unquote(group_route.group(2)),
                    "subject": session.get("group_title", "E2E history group"),
                    "participants": [],
                }
            elif action == "messages":
                query = parse_qs(urlsplit(self.path).query)
                offset = int(query.get("offset", [0])[0])
                limit = int(query.get("limit", [10])[0])
                limit = min(limit, fault.get("page_cap", limit))
                cutoff = int(query.get("filter.timestamp.lte", [9999999999])[0])
                if fault.get("repeated_page"):
                    offset = 0
                values = [
                    m for m in session.get("messages", []) if m["timestamp"] <= cutoff
                ]
                values.sort(key=lambda m: (m["timestamp"], m["id"]))
                result = values[offset : offset + limit]
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
