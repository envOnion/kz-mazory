"""Deterministic external HTTP fixtures; no application API is mocked."""

import datetime
import json
import uuid
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler
from django.utils import timezone

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
    "БЦ Север: оплата 50 000 000 ₸ поступила сегодня": ("north", "payment"),
    "БЦ Южный: оплата 20 000 000 ₸ поступила сегодня": ("south", "payment"),
    "БЦ Южный: проверь доставку": ("south", "request"),
    "БЦ Южный: проверю доставку в пятницу": ("south", "promise"),
    "БЦ Южный: согласуем график платежей": ("south", "request"),
    "БЦ Южный: оплатим 30 000 000 ₸ до 15 октября": ("south", "promise"),
    "Подготовь список объектов команды": ("general", "request"),
    "Да, подготовлю список объектов завтра": ("general", "promise"),
}


def classify(value):
    records = list(value.get("context", [])) + [
        {"raw_message_id": value["target_message_id"], "content": value["content"], "timestamp": value["sent_at"]}
    ]
    records.sort(key=lambda row: row["raw_message_id"])
    existing = {row["topic"]: row["id"] for row in value.get("known_threads", [])}
    groups = {}
    for row in records:
        key, role = ASSIGNMENTS.get(row["content"], ("unknown", "discusses"))
        groups.setdefault(key, []).append((row, role))
    themes, facts = [], []
    for key, members in groups.items():
        promise = next((row for row, role in reversed(members) if role == "promise"), None)
        request = next(
            (
                row
                for row, role in reversed(members)
                if role == "request" and not row["content"].endswith("?")
            ),
            None,
        )
        has_payment = any(role == "payment" for _, role in members)
        ready = bool(promise and request) or key == "east" or has_payment
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
                        if ready and role in ("promise", "project", "payment")
                        else "intermediate",
                        "relation": "answers" if role == "promise" else "discusses",
                    }
                    for row, role in members
                ],
            }
        )
        for row, role in members:
            if role == "payment":
                facts.append(
                    {
                        "thread_key": key,
                        "evidence_message_id": row["raw_message_id"],
                        "fact_type": "payment",
                        "object_name": "БЦ Север" if key == "north" else "БЦ Южный",
                        "amount": "50000000.00" if key == "north" else "20000000.00",
                        "currency": "KZT",
                        "payment_date": datetime.datetime.fromisoformat(row["timestamp"]).date().isoformat(),
                        "payment_kind": "increment",
                        "evidence": row["content"],
                        "confidence": 0.98,
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
        elif ready and promise and request:
            if key == "south" and "30 000 000" in promise["content"]:
                facts.append(
                    {
                        "thread_key": key,
                        "evidence_message_id": promise["raw_message_id"],
                        "promise_message_id": promise["raw_message_id"],
                        "fact_type": "commitment",
                        "object_name": "БЦ Южный",
                        "commitment_text": "Оплатить 30 000 000 ₸ БЦ Южный",
                        "responsible_name": "Боб",
                        "amount": "30000000.00",
                        "currency": "KZT",
                        "deadline_at": (timezone.now() + datetime.timedelta(days=10)).isoformat(),
                        "deadline_precision": "date",
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
                )
            elif key == "south":
                facts.append(
                    {
                        "thread_key": key,
                        "evidence_message_id": promise["raw_message_id"],
                        "promise_message_id": promise["raw_message_id"],
                        "fact_type": "commitment",
                        "object_name": "БЦ Южный",
                        "commitment_text": "Проверить доставку БЦ Южный",
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
                )
            else:
                facts.append(
                    {
                        "thread_key": key,
                        "evidence_message_id": promise["raw_message_id"],
                        "promise_message_id": promise["raw_message_id"],
                        "fact_type": "commitment",
                        "object_name": "БЦ Север"
                        if key == "north"
                        else "",
                        "commitment_text": "Подготовить смету БЦ Север"
                        if key == "north"
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
                )
    return {"threads": themes, "facts": facts}


class ProviderHandler(BaseHTTPRequestHandler):
    control = None

    def save_control(self, state):
        temporary = self.control.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(state))
        temporary.replace(self.control)

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
        if self.path == "/health":
            return self.respond({"status": "ok"})
        if self.path.endswith('/participants/v2'):
            return self.respond([
                {'id': '10001@lid', 'pn': '79990000001@c.us'},
                {'id': '10002@lid', 'pn': '79990000002@c.us'},
                {'id': '10003@lid', 'pn': '79990000003@c.us'},
            ])
        if self.path.startswith('/api/sessions/'):
            return self.respond({'me': {'id': '79990000001@c.us', 'lid': '10001@lid', 'pushName': 'Проверяющий'}})
        if self.path.startswith('/api/contacts?'):
            jid = parse_qs(urlsplit(self.path).query)['contactId'][0]
            names = {'10001@lid': 'Проверяющий', '10002@lid': 'Боб', '10003@lid': 'Тихий участник', '79990000002@c.us': 'Боб'}
            return self.respond({'id': jid, 'pushname': names.get(jid, '')})
        if "/rest/" in self.path:
            state = json.loads(self.control.read_text())
            if state.get("crm_error"):
                return self.respond({"error": "CRM temporarily unavailable"}, 503)
            if self.path.endswith("crm.deal.get.json"):
                return self.respond({"result": {"ID": body["id"], "TITLE": "БЦ Север" if str(body["id"]) == "101" else "БЦ Южный"}})
            if self.path.endswith("crm.timeline.comment.list.json"):
                return self.respond({"result": [row for row in state.get("comments", []) if str(row["ENTITY_ID"]) == str(body["filter"]["ENTITY_ID"])]})
            if self.path.endswith("crm.timeline.comment.add.json"):
                comment = {**body["fields"], "ID": str(1000 + len(state.get("comments", [])))}
                state.setdefault("comments", []).append(comment)
                self.save_control(state)
                return self.respond({"result": comment["ID"]})
            if self.path.endswith("tasks.task.list.json"):
                return self.respond({"result": {"tasks": []}})
            if self.path.endswith("tasks.task.add.json"):
                return self.respond({"result": {"task": {"id": "2000"}}})
            if self.path.endswith("crm.company.list.json"):
                rows = [
                    {"ID": "201", "TITLE": "ТОО Север Холдинг"},
                    {"ID": "202", "TITLE": "ТОО Юг Групп"},
                ]
                return self.respond({"result": rows})
            if self.path.endswith("crm.deal.list.json"):
                rows = [
                    {
                        "ID": "101",
                        "TITLE": "БЦ Север",
                        "COMPANY_ID": "201",
                        "ASSIGNED_BY_ID": "7",
                        "CURRENCY_ID": "KZT",
                    },
                    {
                        "ID": "102",
                        "TITLE": "БЦ Южный",
                        "COMPANY_ID": "202",
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
                self.save_control(state)
                return self.respond({"result": "103"})
            return self.respond({"result": True})
        if self.path.endswith("/messages/count_tokens"):
            state = json.loads(self.control.read_text())
            if state.get('analytics_force_compaction') and any(
                len(json.loads(block['content']).get('rows', [])) > 5
                for message in body.get('messages', []) if isinstance(message['content'], list)
                for block in message['content'] if block.get('type') == 'tool_result'
            ):
                return self.respond({'input_tokens': 200000})
            return self.respond(
                {"input_tokens": max(1, len(json.dumps(body, ensure_ascii=False)) // 4)}
            )
        if self.path.endswith("/v1/messages") and any(t.get("name") == "query_dataset" for t in body.get("tools", [])):
            import time
            state = json.loads(self.control.read_text())
            if state.get('analytics_delay'):
                time.sleep(state['analytics_delay'])
            if state.get('analytics_narrative_error') and any(
                json.loads(block['content']).get('presentation_created')
                for message in body['messages'] if isinstance(message['content'], list)
                for block in message['content'] if block.get('type') == 'tool_result'
            ):
                return self.respond({'error': {'type': 'overloaded_error', 'message': 'Fixture narrative outage'}}, 503)
            return self.respond(analytical_response(body))
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


def analytical_response(body):
    """Native tool turns over real query results, no frontend/API mock."""
    messages = body['messages']
    tools = {b['id']: b for m in messages for b in (m['content'] if isinstance(m['content'], list) else []) if b.get('type') == 'tool_use'}
    results = [(tools.get(b['tool_use_id'], {}).get('name'), json.loads(b['content'])) for m in messages for b in (m['content'] if isinstance(m['content'], list) else []) if b.get('type') == 'tool_result']
    prompts = [m['content'] for m in messages if m['role'] == 'user' and isinstance(m['content'], str)]
    prompt = prompts[-1].lower() if prompts else ''
    last_name, last = results[-1] if results else (None, {})
    def response(text=None, name=None, arguments=None):
        content = [{'type': 'text', 'text': text}] if text is not None else [{'type': 'tool_use', 'id': 'fixture-' + uuid.uuid4().hex, 'name': name, 'input': arguments}]
        return {'id': 'fixture-response', 'type': 'message', 'role': 'assistant', 'model': 'local-fixture', 'content': content,
                'stop_reason': 'end_turn' if text is not None else 'tool_use', 'usage': {'input_tokens': 100, 'output_tokens': 100}}
    if 'придумай сам' in prompt and not any(name == 'query_dataset' for name, _ in results):
        return response(text='Вот график: {"version":"1.0","blocks":[{"id":"status_distribution","kind":"bar","dataset_id":"crm_projects","encoding":{"category":"status","value":"project_count"}}]}')
    if prompt.startswith('json графика') and last_name == 'describe_schema':
        return response(name='query_dataset', arguments={'dataset': 'crm_projects', 'dimensions': ['status'], 'measures': ['project_count']})
    if 'форматирование' in prompt:
        return response(text='**Проверено** <img src=x onerror=window.__xss=1><script>window.__xss=1</script> [ссылка](javascript:alert(1))')
    if prompt.strip() in ['йо', 'привет']:
        return response(text='Привет! **Помогу с аналитикой.** Что посмотрим: поступления, сделки или задачи?')
    if 'истори' in prompt or ('crm' in prompt and 'дат' in prompt):
        return response(text='CRM содержит текущие стадии сделок без истории переходов. Для динамики нужна история изменений; сейчас можно сравнить текущие стадии или ответственных.')
    datasets = [data for name, data in results if name == 'query_dataset' and 'dataset_id' in data]
    if 'проверь поля crm' in prompt and not datasets:
        if last_name == 'query_dataset' and last.get('validation_errors'):
            detail = last['validation_errors'][0]
            spec = {'dataset': 'crm_projects', 'dimensions': ['status'], 'measures': ['project_count']}
            if 'dimensions must use' in detail:
                spec['date_range'] = {'start': '2026-01-01', 'end_exclusive': '2027-01-01'}
            elif 'current snapshot' not in detail:
                return response(text='Не удалось проверить запрос.')
            return response(name='query_dataset', arguments=spec)
        return response(name='query_dataset', arguments={'dataset': 'crm_projects', 'dimensions': ['external_stage_name'], 'measures': ['deal_count'], 'date_range': {'start': '2026-01-01', 'end_exclusive': '2027-01-01'}})
    if last_name == 'build_presentation':
        if 'fallback' in prompt:
            # Valid model text with invented financial values must trigger the grounded fallback.
            return response(text='Итого 999999 KZT и 777 сделок.')
        facts = last.get('grounded_facts', {})
        ratio = next((key for key, fact in facts.items() if fact['formula'] == 'ratio_percent'), None)
        if ratio:
            return response(text=f'Выполнение плана — {{{{fact:{ratio}}}}}.')
        total = next((key for key, fact in facts.items() if fact['formula'] == 'sum'), None)
        return response(text=f'По выбранным условиям итог — {{{{fact:{total}}}}}. Подробности доступны на графике и в таблице.' if total else 'Данные подготовлены.')
    if last_name == 'read_records' and 'dataset_id' in last:
        block = {'id': 'overdue', 'kind': 'bar', 'dataset_id': last['dataset_id'], 'encoding': {'category': 'responsible', 'value': 'commitment_count'}} if last['normalized_query'].get('group_by') else {'id': 'overdue', 'kind': 'table', 'dataset_id': last['dataset_id'], 'columns': ['text', 'responsible', 'deadline', 'status']}
        return response(name='build_presentation', arguments={'version': '1.0', 'title': 'Просроченные обещания', 'blocks': [block]})
    if 'обещани' in prompt or prompt.strip() == 'построй график':
        return response(name='read_records', arguments={'overdue_only': True, **({'group_by': 'responsible'} if 'график' in prompt else {'limit': 50})})
    if last_name == 'derived_facts':
        last = next(data for name, data in reversed(results) if name == 'combine_datasets' and 'dataset_id' in data)
    if last_name in ['query_dataset', 'combine_datasets', 'derived_facts'] and 'dataset_id' in last:
        if 'объедини категории' in prompt and last_name == 'query_dataset':
            return response(name='combine_datasets', arguments={'mode': 'categories', 'dataset_ids': [last['dataset_id']], 'category': 'status', 'groups': {row['status']: 'Общая группа' for row in last['rows']}})
        if 'объедини август и сентябрь' in prompt and last_name == 'query_dataset':
            if len(datasets) == 1:
                return response(name='query_dataset', arguments={**last['normalized_query'], 'date_range': {'start': '2026-09-01', 'end_exclusive': '2026-10-01'}})
            return response(name='combine_datasets', arguments={'mode': 'append', 'dataset_ids': [ds['dataset_id'] for ds in datasets]})
        if 'процент выполнения' in prompt and last_name == 'combine_datasets':
            return response(name='derived_facts', arguments={'operation': 'ratio_percent', 'left': f'{last["dataset_id"]}:series0:total', 'right': f'{last["dataset_id"]}:series1:total', 'label': 'Выполнение плана'})
        if 'два графика' in prompt:
            if len(datasets) == 1:
                return response(name='query_dataset', arguments={'dataset': 'crm_projects', 'dimensions': ['status'], 'measures': ['project_count']})
            return response(name='build_presentation', arguments={'version': '1.0', 'title': 'Поступления и сделки', 'blocks': [
                {'id': 'payments', 'title': 'Поступления', 'kind': 'bar', 'dataset_id': datasets[0]['dataset_id'], 'encoding': {'category': 'payment_month', 'value': 'received_amount'}},
                {'id': 'deals', 'title': 'Сделки CRM', 'kind': 'bar', 'dataset_id': datasets[1]['dataset_id'], 'encoding': {'category': 'status', 'value': 'project_count'}}]})
        if 'план' in prompt and len(datasets) == 1:
            base = last['normalized_query']
            if 'payment_month' not in base.get('dimensions', []):
                return response(text='План утверждён по месяцам. Перейти к месяцам для сравнения плана и факта?')
            return response(name='query_dataset', arguments={'dataset': 'targets', 'dimensions': ['month'], 'measures': ['target_amount'], 'date_range': base['date_range']})
        if 'план' in prompt and last_name == 'query_dataset' and len(datasets) == 2:
            return response(name='combine_datasets', arguments={'mode': 'series', 'dataset_ids': [ds['dataset_id'] for ds in datasets], 'keys': ['payment_month'], 'labels': ['Поступления', 'План']})
        if 'договор' in prompt and len(datasets) == 1:
            return response(name='query_dataset', arguments={'dataset': 'projects', 'dimensions': ['project_id'], 'measures': ['contract_amount']})
        if 'договор' in prompt and last_name == 'query_dataset' and len(datasets) == 2:
            return response(name='combine_datasets', arguments={'mode': 'join', 'dataset_ids': [ds['dataset_id'] for ds in datasets], 'keys': ['project_id'], 'join': 'full'})
        columns = last['columns']; dimension = next(c['name'] for c in columns if c['type'] in ['text', 'date', 'id'])
        measure = next(c['name'] for c in columns if c['type'] in ['count', 'money'])
        title = 'Сделки по стадиям CRM' if last['normalized_query']['dataset'] == 'crm_projects' or last['normalized_query'].get('mode') == 'categories' else 'Поступления'
        if last['normalized_query'].get('mode') == 'join':
            block = {'id': 'chart', 'kind': 'table', 'dataset_id': last['dataset_id'], 'columns': [c['name'] for c in columns]}
        else:
            encoding = {'category': dimension, 'value': measure}
            if last['normalized_query'].get('mode') == 'series': encoding['series'] = 'series'
            block = {'id': 'chart', 'kind': 'bar', 'dataset_id': last['dataset_id'], 'encoding': encoding}
        return response(name='build_presentation', arguments={'version': '1.0', 'title': title, 'blocks': [block]})
    if any(word in prompt for word in ['crm', 'сделк', 'стади', 'объедини категории']) and 'два графика' not in prompt:
        spec = {'dataset': 'crm_projects', 'dimensions': ['crm_manager'] if 'ответствен' in prompt else ['status'], 'measures': ['project_count']}
    else:
        spec = {'dataset': 'payments', 'dimensions': ['project_id'] if 'договор' in prompt else ['payment_week'] if 'недел' in prompt else ['payment_month'], 'measures': ['received_amount'], 'date_range': {'start': '2026-08-01', 'end_exclusive': '2026-10-01'}}
        if 'kpi' in prompt:
            spec.pop('date_range')
        if 'объедини август и сентябрь' in prompt:
            spec['date_range'] = {'start': '2026-08-01', 'end_exclusive': '2026-09-01'}
        # Refinement context is read from the server's persisted normalized plan.
        for previous in reversed(prompts[:-1]):
            if previous.startswith('Контекст выбранного результата'):
                plan = json.loads(previous.split(': ', 1)[1]); base = plan['blocks'][0]['query']
                if base.get('dataset') == 'payments':
                    spec['date_range'] = base['date_range']
                    spec['filters'] = base.get('filters', [])
                    spec['currency'] = base.get('currency', 'KZT')
                    if not any(word in prompt for word in ['недел', 'месяц', 'договор']):
                        spec['dimensions'] = base['dimensions']
                        spec['order_by'] = base.get('order_by', [])
                break
    if 'топ' in prompt:
        spec.update({'limit': 1, 'top_n': True, 'order_by': [{'field': spec['measures'][0], 'direction': 'desc'}]})
    return response(name='query_dataset', arguments=spec)
