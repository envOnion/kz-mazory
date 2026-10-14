"""The verified text-only Qwen 27B deployment and evidence contract."""

import copy
import hashlib
from django.conf import settings

MODEL = "qwen3.8-27b"
BUILD = "b11561-e937b17a2"
TEMPLATE_SHA256 = "c3cf9e34abf4f9e36c2d72165aa9c132d3e2a725b6c2586aaa3a8af9d7a81041"
CONTEXT = 49152
PROFILE = "qwen27-medium-source-refs-v12" if settings.QWEN_COMPACT_EVIDENCE else "qwen27-medium-full-source-v10"

EVIDENCE_RULES = """
Правила доказательств применяются ко всем фактам и пояснениям. Сумма из вопроса/просьбы не становится подтверждённой суммой в последующем ответе только о статусе. Числовая сумма требует денежного утверждения; без него contract_amount/cost_amount/amount не возвращай, amount_precision=unknown. Подписание договора и выставление счёта сохраняй отдельно от подтверждённого поступления денег. Переговоры для решения проблемы не доказывают успешное решение. Кандидат справочника CRM не подтверждает связь: нужна доказанная идентичность без противоречия контрагенту.
Принятое обещание конкретного действия обязательно представляется отдельным commitment, не только project/summary/current_action. Повтор и позднее исполнение — то же обязательство с первоначальным promise_message_id и исходным сроком. Pending сохраняется до явного подтверждения выполнения именно обещанного действия; последующее финансирование/покупка — другой статус. Запланированная встреча не состоявшаяся: поздний контакт или другое действие не подтверждают её выполнение. Запрос руководителя и ожидание действий третьих лиц не являются обещаниями автора отчёта. Имена исполнителей копируй из оригиналов без перевода; несколько людей не один responsible_name.
Project — конкретный идентифицируемый бизнес-объект. Статистика/портфель/таблица/файл/сводная сумма/название события не отдельный проект или оплата. Обещание передать таблицу может быть commitment. Сохраняй узнаваемое имя объекта, а неизвестное оставляй пустым; названия оборудования и событий не заменяют имя проекта.
object_name во всех типах фактов — именно имя реального бизнес-объекта. Для общей задачи отправить файл/таблицу без названного бизнес-объекта object_name=""; предмет файла сохраняется в commitment_text. Заголовок темы про таблицу не делает таблицу проектом. Если проверяемое изменение относится лишь к ссылке/файлу/отчёту и не содержит изменения конкретного бизнеса, project не возвращай, facts=[] допустимо. Повторное подтверждение сохраняет исходное действие и его идентичность, не переименовывает предмет ради нового обязательства.
Вопрос, просьба уточнить статус и напоминание о недостающем отчёте не являются изменением проекта: сохраняй тему, но не создавай project-факт из запроса обратной связи. Несколько разных бизнес-объектов не объединяй в одно object_name через список.
Полный источник: evidence_message_id выбирает оригинальное сообщение, действительно подтверждающее факт, evidence — ВСЁ его исходное content, с точными пробелами и переводами строк. Аналогично каждый evidence_messages.raw_message_id связан с ВСЕМ оригиналом в quote; роль описывает его вклад. Native schema связывает ID и полный текст. Не цитируй один заголовок, не склеивай/нормализуй цитаты. При исполнении сохраняй исходное обещание и отдельное доказательство позднего выполнения.
Для commitment evidence_message_id = promise_message_id, первичная evidence — оригинал исходного обещания. Первый evidence_messages обязательно этот же оригинал с role=promise; поздний запрос/срок/выполнение идут отдельными элементами после него.
"""
if settings.QWEN_COMPACT_EVIDENCE:
    EVIDENCE_RULES += '\nРазделяй компанию и объект во всех типах фактов, включая payment: если оригинал явно называет компанию, сохраняй её буквальное название в company_name. Поле company_name нельзя оставлять пустым, когда плательщик явно назван. Например «компания Top Build ... поступила оплата 117 млн» требует company_name="Top Build". Это не доказывает название бизнес-объекта: когда конкретный объект не назван, object_name="". Не переносить компанию в object_name вместо company_name.\n'
    EVIDENCE_RULES = EVIDENCE_RULES.replace(
        'Полный источник: evidence_message_id выбирает оригинальное сообщение, действительно подтверждающее факт, evidence — ВСЁ его исходное content, с точными пробелами и переводами строк. Аналогично каждый evidence_messages.raw_message_id связан с ВСЕМ оригиналом в quote; роль описывает его вклад. Native schema связывает ID и полный текст. Не цитируй один заголовок, не склеивай/нормализуй цитаты. При исполнении сохраняй исходное обещание и отдельное доказательство позднего выполнения.',
        'Полный источник: evidence_message_id выбирает оригинальное сообщение, действительно подтверждающее факт. Чтобы не переписывать один оригинал много раз, evidence содержит только строку source:ID (например source:12 для evidence_message_id=12). Аналогично evidence_messages.quote = source:ID для того же raw_message_id. Native schema связывает ID и ссылку строго; backend восстановит ВСЁ исходное content из действительно прочитанного полного оригинала, с точными пробелами и переводами строк. Ссылки не заменяют чтение и проверку смысла. При исполнении сохраняй исходное обещание и отдельное доказательство позднего выполнения.')
    EVIDENCE_RULES = EVIDENCE_RULES.replace('первичная evidence — оригинал исходного обещания.', 'первичная evidence — ссылка source:ID на оригинал исходного обещания.')


def enabled(cfg):
    return cfg.chat_model_name == MODEL


def parameters(payload):
    result = copy.deepcopy(payload)
    for key in ("provider", "plugins", "reasoning"):
        result.pop(key, None)
    result.update(temperature=0, top_p=0.95, top_k=20, min_p=0,
                  repeat_penalty=1, presence_penalty=0, reasoning_effort="medium",
                  chat_template_kwargs={"enable_thinking": True, "reasoning_effort": "medium"})
    return result


def originals(inputs):
    sources = {}
    for row in [*inputs.get("context", []), *inputs.get("target_messages", [])]:
        pk, content = row.get("raw_message_id"), row.get("content")
        if not row.get("partial") and not row.get('evidence_excerpt') and type(pk) is int and isinstance(content, str) and 0 < len(content) <= 32000:
            if pk in sources and sources[pk] != content:
                raise ValueError("Conflicting immutable originals")
            sources[pk] = content
    pk, content = inputs.get("target_message_id"), inputs.get("content")
    if pk not in sources and inputs.get('target_original_complete', True) and type(pk) is int and isinstance(content, str) and 0 < len(content) <= 32000 and inputs.get("analysis_operation") not in ('commitment_resolution','promise_reconciliation'):
        sources[pk] = content
    return sources


def grounded_schema(schema, inputs):
    """Bind each evidence pair atomically; never infer sources from CRM/summary."""
    from .providers import ProviderUnavailable

    sources = originals(inputs)
    if not sources:
        raise ProviderUnavailable("context_originals_unavailable")
    def quote(pk):
        return f'source:{pk}' if settings.QWEN_COMPACT_EVIDENCE else sources[pk]

    def bind(node):
        if isinstance(node, list):
            return [bind(item) for item in node]
        if not isinstance(node, dict):
            return node
        result = {key: bind(value) for key, value in node.items()}
        properties = result.get("properties", {})
        pair = ("evidence_message_id", "evidence") if {"evidence_message_id", "evidence"} <= properties.keys() else ("raw_message_id", "quote") if {"raw_message_id", "quote"} <= properties.keys() else None
        if pair:
            id_field, text_field = pair
            allowed = properties[id_field].get("enum", sorted(sources))
            variants = []
            for pk in sorted(sources):
                if pk not in allowed:
                    continue
                item = copy.deepcopy(result)
                if properties.get('fact_type', {}).get('enum') == ['commitment']:
                    promise_ids = properties['promise_message_id'].get('enum', sorted(sources))
                    if pk not in promise_ids:
                        continue
                    item['properties']['promise_message_id'] = {'type': 'integer', 'const': pk}
                    item['properties']['evidence_messages']['prefixItems'] = [{
                        'type': 'object', 'additionalProperties': False,
                        'required': ['raw_message_id', 'quote', 'role'],
                        'properties': {'raw_message_id': {'type': 'integer', 'const': pk},
                                       'quote': {'type': 'string', 'const': quote(pk)},
                                       'role': {'type': 'string', 'const': 'promise'}}}]
                item["properties"][id_field] = {"type": "integer", "const": pk}
                item["properties"][text_field] = {"type": "string", "const": quote(pk)}
                item["required"] = sorted(set(item.get("required", [])) | {id_field, text_field})
                variants.append(item)
            if not variants:
                raise ProviderUnavailable("context_originals_unavailable")
            return {"anyOf": variants}
        return result

    return bind(schema)


def restore_evidence(result, inputs):
    """Expand only atomic references to complete originals in this frozen request."""
    if not settings.QWEN_COMPACT_EVIDENCE:
        return result
    sources = originals(inputs)
    def expand(node):
        if isinstance(node, list):
            return [expand(item) for item in node]
        if not isinstance(node, dict):
            return node
        value = {key: expand(item) for key, item in node.items()}
        for id_field, quote_field in [('evidence_message_id', 'evidence'), ('raw_message_id', 'quote')]:
            pk = value.get(id_field)
            if type(pk) is int and pk in sources and value.get(quote_field) == f'source:{pk}':
                value[quote_field] = sources[pk]
        return value
    return expand(result)


def attach_schema(payload, schema, inputs):
    result = copy.deepcopy(payload)
    result.pop("_ollama_format", None)
    result["response_format"] = {"type": "json_schema", "json_schema": {
        "name": "grounded_whatsapp", "strict": True,
        "schema": grounded_schema(schema, inputs)}}
    return result


def verify_metadata(props, models, configured_window):
    from .providers import ProviderUnavailable

    rows = models.get("data", []) if isinstance(models, dict) else []
    valid = (isinstance(props, dict) and props.get("build_info") == BUILD
             and props.get("model_alias") == MODEL and props.get("total_slots") == 1
             and props.get("model_path", "").endswith("/qwen3.8-27b.gguf")
             and hashlib.sha256(props.get("chat_template", "").encode()).hexdigest() == TEMPLATE_SHA256
             and any(row.get("id") == MODEL for row in rows))
    window = props.get("default_generation_settings", {}).get("n_ctx") if isinstance(props, dict) else None
    if not valid or type(window) is not int or window != CONTEXT or configured_window > window:
        raise ProviderUnavailable("context_model_metadata_unavailable")
    return window
