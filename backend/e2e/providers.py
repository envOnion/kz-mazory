"""Deterministic external HTTP fixtures; no application API is mocked."""

import json
from http.server import BaseHTTPRequestHandler

TOPICS = {
    "north": "Смета БЦ Север",
    "south": "Доставка БЦ Южный",
    "general": "Список объектов команды",
    "east": "Новый объект БЦ Восток",
}
ASSIGNMENTS = {
    "Новый объект БЦ Восток": ("east", "project"),
    "БЦ Север: подготовь смету": ("north", "request"),
    "Сделаешь завтра?": ("north", "request"),
    "БЦ Север: да, подготовлю смету завтра": ("north", "promise"),
    "БЦ Южный: проверь доставку": ("south", "request"),
    "БЦ Южный: проверю доставку в пятницу": ("south", "promise"),
    "Подготовь список объектов команды": ("general", "request"),
    "Да, подготовлю список объектов завтра": ("general", "promise"),
}


def classify(value):
    records = list(value.get("context", [])) + [
        {"raw_message_id": value["target_message_id"], "content": value["content"]}
    ]
    records.sort(key=lambda row: row["raw_message_id"])
    existing = {row["topic"]: row["id"] for row in value.get("known_threads", [])}
    groups = {}
    for row in records:
        key, role = ASSIGNMENTS.get(row["content"], ("unknown", "discusses"))
        groups.setdefault(key, []).append((row, role))
    themes, facts = [], []
    for key, members in groups.items():
        promise = next((row for row, role in members if role == "promise"), None)
        request = next(
            (
                row
                for row, role in members
                if role == "request" and not row["content"].endswith("?")
            ),
            None,
        )
        ready = bool(promise and request) or key == "east"
        topic = TOPICS.get(key, "Тема требует уточнения")
        themes.append(
            {
                "key": key,
                "thread_id": existing.get(topic),
                "topic": topic,
                "summary": topic,
                "state": "ready"
                if ready
                else "unknown"
                if key == "unknown"
                else "open",
                "completion_reason": "Конкретная просьба и явное обещание"
                if ready
                else "",
                "messages": [
                    {
                        "raw_message_id": row["raw_message_id"],
                        "thought_state": "final"
                        if ready and role in ("promise", "project")
                        else "intermediate",
                        "relation": "answers" if role == "promise" else "discusses",
                    }
                    for row, role in members
                ],
            }
        )
        if ready and key == "east":
            row = members[0][0]
            facts.append(
                {
                    "thread_key": key,
                    "evidence_message_id": row["raw_message_id"],
                    "fact_type": "project",
                    "object_name": "БЦ Восток",
                    "evidence": row["content"],
                    "confidence": 0.95,
                }
            )
        elif ready:
            fact = {
                "thread_key": key,
                "evidence_message_id": promise["raw_message_id"],
                "promise_message_id": promise["raw_message_id"],
                "fact_type": "commitment",
                "object_name": "БЦ Север"
                if key == "north"
                else "БЦ Южный"
                if key == "south"
                else "",
                "commitment_text": "Подготовить смету БЦ Север"
                if key == "north"
                else "Проверить доставку БЦ Южный"
                if key == "south"
                else "Подготовить список объектов команды",
                "responsible_name": "Боб",
                "evidence": promise["content"],
                "confidence": 0.95,
                "evidence_messages": [
                    {
                        "raw_message_id": request["raw_message_id"],
                        "quote": request["content"],
                        "role": "request",
                    },
                    {
                        "raw_message_id": promise["raw_message_id"],
                        "quote": promise["content"],
                        "role": "promise",
                    },
                ],
            }
            facts.append(fact)
    return {"threads": themes, "facts": facts}


class ProviderHandler(BaseHTTPRequestHandler):
    control = None

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.handle_request({})

    def do_POST(self):
        body = json.loads(
            self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}"
        )
        self.handle_request(body)

    def handle_request(self, body):
        if "/rest/" in self.path:
            state = json.loads(self.control.read_text())
            if state.get("crm_error"):
                return self.respond({"error": "CRM temporarily unavailable"}, 503)
            if self.path.endswith("crm.deal.list.json"):
                rows = [
                    {
                        "ID": "101",
                        "TITLE": "БЦ Север",
                        "ASSIGNED_BY_ID": "7",
                        "CURRENCY_ID": "KZT",
                    },
                    {
                        "ID": "102",
                        "TITLE": "БЦ Южный",
                        "ASSIGNED_BY_ID": "7",
                        "CURRENCY_ID": "KZT",
                    },
                ]
                filters = body.get("filter", {})
                if "=ORIGINATOR_ID" in filters:
                    rows = []
                for name, value in filters.items():
                    if "TITLE" in name:
                        rows = [
                            row
                            for row in rows
                            if str(value).casefold() in row["TITLE"].casefold()
                        ]
                offset = body.get("start", 0)
                return self.respond(
                    {
                        "result": rows[offset : offset + 1],
                        **({"next": offset + 1} if offset + 1 < len(rows) else {}),
                    }
                )
            if self.path.endswith("crm.deal.add.json"):
                state.setdefault("writes", []).append(body["fields"])
                self.control.write_text(json.dumps(state))
                return self.respond({"result": "103"})
            return self.respond({"result": True})
        if self.path.endswith("/messages/count_tokens"):
            return self.respond(
                {"input_tokens": max(1, len(json.dumps(body, ensure_ascii=False)) // 4)}
            )
        if self.path.endswith("/v1/messages"):
            value = json.loads(body["messages"][-1]["content"])
            result = classify(value)
            return self.respond(
                {
                    "id": "fixture-response",
                    "type": "message",
                    "role": "assistant",
                    "model": "local-fixture",
                    "content": [
                        {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                    ],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 100, "output_tokens": 100},
                }
            )
        return self.respond({"error": "Unsupported fixture request"}, 404)

    def respond(self, result, status=200):
        payload = json.dumps(result).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
