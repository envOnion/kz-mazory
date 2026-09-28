"""Isolated integration fixture. Never included in production routes or real delivery."""

import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone

messages = []
ai_requests = {"embeddings": [], "chats": []}
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


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
