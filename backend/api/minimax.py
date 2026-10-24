"""MiniMax Chat Completions transport; complete SSE before accepting JSON."""

import copy
import json
from urllib.parse import urlsplit

from .providers import ProviderUnavailable

MODEL = "MiniMax-M3.1-Flash"
POLICY = "minimax-tools-quotes-v1"
TOOL = "mazory_extraction"
HOSTS = {"api.minimax.io", "api.minimaxi.com"}


def quote_plan(payload):
    """Reuse the shared lossless codec; explicit auxiliary schemas stay exact."""
    response_format = payload.get("response_format")
    container = response_format.get("json_schema", {}) if isinstance(response_format, dict) else {}
    if not isinstance(container, dict):
        return None
    if container.get("name") not in (None, "grounded_whatsapp", "mazory_extraction"):
        return None
    users = [row for row in payload.get("messages", []) if row.get("role") == "user"]
    if len(users) != 1 or not isinstance(users[0].get("content"), str):
        return None
    try:
        inputs = json.loads(users[0]["content"])
    except (TypeError, ValueError):
        return None
    if (not isinstance(inputs, dict) or type(inputs.get("target_message_id")) is not int
            or not isinstance(inputs.get("content"), str) or "analysis_instructions" not in inputs
            or inputs.get("analysis_transport") != POLICY
            or inputs.get("analysis_operation") not in (None, "focused_fact_extraction")):
        return None
    from .extraction_schema import extraction_schema, NATIVE_STRING_POLICY
    from .quote_input_codec import project, POLICY as QUOTE_POLICY, QuoteCodecError
    canonical = copy.deepcopy(inputs)
    canonical.update(native_quote_policy=QUOTE_POLICY, native_string_policy=NATIVE_STRING_POLICY)
    schema = extraction_schema(canonical, fixed_slots=False)
    try:
        native, native_schema = project(canonical, schema)
    except QuoteCodecError:
        raise ProviderUnavailable("context_snapshot_mismatch") from None
    return canonical, schema, native, native_schema


def enabled(cfg):
    return (cfg.chat_model_name == MODEL and getattr(cfg, "chat_api_format", "openai_compatible") == "openai_compatible"
            and urlsplit(cfg.chat_provider_url).hostname in HOSTS)


def _native_quote_system_messages(messages):
    """Align trusted prompt quote wording with the native quote-ID schema."""
    replacements = (
        ("evidence — короткая точная непрерывная цитата оригинала этого ID.",
         "evidence — целочисленный quote_id из источников для точной цитаты оригинала этого ID."),
        ("evidence: короткая точная непрерывная цитата ИЗ evidence_message_id;",
         "evidence: целочисленный quote_id из источников для точной цитаты ИЗ evidence_message_id;"),
        ("quote: точная цитата,",
         "quote: целочисленный quote_id из источников для точной цитаты,"),
    )
    for message in messages:
        if message.get("role") != "system" or not isinstance(message.get("content"), str):
            continue
        for old, new in replacements:
            message["content"] = message["content"].replace(old, new)


def request(payload, max_tokens):
    wire = copy.deepcopy(payload)
    for key in ("provider", "plugins", "reasoning", "temperature", "top_p"):
        wire.pop(key, None)
    limit = wire.pop("max_tokens", wire.get("max_completion_tokens", max_tokens))
    wire.update(max_completion_tokens=min(limit, max_tokens),
                stream=True, stream_options={"include_usage": True})
    shape = wire.pop("response_format", {})
    schema = shape.get("json_schema", {}).get("schema") if isinstance(shape, dict) else None
    if not schema:
        # Keep short connection checks short; structured extraction uses the
        # provider's reasoning default unless the caller explicitly chose one.
        wire.setdefault("reasoning_effort", "low")
    if schema:
        plan = quote_plan(payload)
        quote_instruction = "Цитаты должны быть точными строками из источников, как требует схема."
        if plan:
            _, _, native, schema = plan
            user = next(row for row in wire["messages"] if row.get("role") == "user")
            user["content"] = json.dumps(native, ensure_ascii=False, separators=(",", ":"))
            if "quote_catalog_count" in native:
                _native_quote_system_messages(wire["messages"])
                quote_instruction = ("В evidence и quote выбирай целочисленные quote_id из источников, "
                                     "как требует схема; никаких строк и придуманных ID. Не повторяй текст источников.")
        contract_name = str(shape.get("json_schema", {}).get("name", ""))
        entity_contract = contract_name.startswith("mazory_entity_") or contract_name == "mazory_batch_screen"
        if entity_contract:
            quote_instruction = "В evidence и quote выбирай числовой quote_id из source_quotes; текст цитат не переписывай."
        if contract_name == "mazory_batch_screen":
            quote_instruction = "Верни ровно одно решение для каждого raw_message_id пакета. Не извлекай факты и темы."
        wire["messages"].insert(0, {"role": "system", "content": (
            "Верни весь результат единственным вызовом mazory_extraction строго по JSON Schema. "
            "Соблюдай типы, required, enum и additionalProperties=false. "
            "Не добавляй Markdown или пояснения за пределами схемы. " + quote_instruction
        ) if entity_contract else (
            "Верни весь результат единственным вызовом mazory_extraction строго по его JSON Schema. "
            "Не добавляй Markdown, пояснения и поля, которых нет в схеме; соблюдай каждый required, "
            "тип, enum и additionalProperties=false. Для каждого ключа cN из target_classifications "
            "заполни все обязательные поля их допустимыми значениями и правильными типами. "
            "ID сообщений — JSON-числа, строго из integer enum, без строк и префикса m. "
            "Для новой темы thread_id=null, никогда не пустая строка. parent_key у корневой темы — null. "
            "thread_key — только разрешённый tN; для одиночной target_classification и cN, где raw_message_id=target_message_id, обязательно t0. "
            "Числовые поля — JSON-числа либо null, когда схема допускает null; не пустые строки. "
            "Создавай только реально обнаруженные темы, без пустых тем-заглушек. "
            "Для каждого целевого ID обязательна классификация. evidence_messages всегда массив объектов. "
            + quote_instruction)})
        wire["tools"] = [{"type": "function", "function": {
            "name": TOOL, "description": "Return the complete validated Mazory analysis for every target message.",
            "parameters": schema}}]
        wire["tool_choice"] = {"type": "function", "function": {"name": TOOL}}
    return wire


def parse_stream(body):
    """No partial stream, missing terminator or mixed call is a success."""
    try:
        content, reasoning, calls = [], [], {}
        usage, finish, done, model = {}, None, False, MODEL
        frames = body.decode("utf-8").replace("\r\n", "\n").split("\n\n")
        for frame in frames:
            lines = [line[5:].lstrip(" ") for line in frame.splitlines() if line.startswith("data:")]
            if not lines:
                continue
            raw = "\n".join(lines)
            if raw == "[DONE]":
                done = True
                continue
            if done:
                raise ValueError()
            item = json.loads(raw)
            if item.get("error"):
                return {"error": item["error"], "usage": usage}
            if isinstance(item.get("usage"), dict):
                usage = item["usage"]
            model = item.get("model") or model
            for choice in item.get("choices", []):
                if choice.get("index", 0) != 0:
                    raise ValueError()
                finish = choice.get("finish_reason") or finish
                delta = choice.get("delta") or {}
                for key, target in (("content", content), ("reasoning_content", reasoning)):
                    value = delta.get(key)
                    if value is not None:
                        if not isinstance(value, str):
                            raise ValueError()
                        target.append(value)
                for call in delta.get("tool_calls", []):
                    index = call["index"]
                    if type(index) is not int or index < 0:
                        raise ValueError()
                    saved = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if call.get("id"):
                        saved["id"] = call["id"]
                    for key in ("name", "arguments"):
                        value = call.get("function", {}).get(key)
                        if value is not None:
                            if not isinstance(value, str):
                                raise ValueError()
                            saved["function"][key] += value
        if not done or not finish:
            raise ValueError()
        message = {"role": "assistant", "content": "".join(content), "reasoning_content": "".join(reasoning)}
        if calls:
            message["tool_calls"] = [calls[key] for key in sorted(calls)]
        return normalize({"model": model, "usage": usage, "choices": [{"index": 0, "finish_reason": finish, "message": message}]})
    except (TypeError, KeyError, ValueError, AttributeError, UnicodeError):
        raise ProviderUnavailable("provider_invalid_response") from None


def normalize(data):
    if not isinstance(data, dict):
        raise ProviderUnavailable("provider_invalid_response")
    if data.get("error"):
        return data
    choices = data.get("choices") or []
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ProviderUnavailable("provider_invalid_response")
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        raise ProviderUnavailable("provider_invalid_response")
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("function"), dict)
        for row in calls
    ):
        raise ProviderUnavailable("provider_invalid_response")
    own = [row for row in calls if row.get("function", {}).get("name") == TOOL]
    if own:
        text = message.get("content")
        if len(calls) != 1 or (text is not None and (not isinstance(text, str) or text.strip())):
            raise ProviderUnavailable("provider_invalid_response")
        arguments = own[0]["function"].get("arguments")
        if not isinstance(arguments, str):
            raise ProviderUnavailable("provider_invalid_response")
        message["content"] = arguments
        message.pop("tool_calls", None)
        if choices[0].get("finish_reason") == "tool_calls":
            choices[0]["finish_reason"] = "stop"
    return data


def restore_response(data, canonical_payload):
    if data.get("error") or not data.get("choices") or data["choices"][0].get("finish_reason") != "stop":
        return data
    plan = quote_plan(canonical_payload)
    if plan is None:
        return data
    canonical, schema, _, native_schema = plan
    from .ai_service import extraction_schema_diagnostics
    from .quote_input_codec import restore, QuoteCodecError
    try:
        result = json.loads(data["choices"][0]["message"]["content"])
    except (TypeError, ValueError):
        raise ProviderUnavailable("extraction_json_parse") from None
    errors = extraction_schema_diagnostics(native_schema, result)
    if errors:
        raise ProviderUnavailable("invalid_extraction_schema", diagnostics=errors)
    try:
        restored = restore(result, canonical, schema)
    except QuoteCodecError:
        raise ProviderUnavailable("invalid_extraction_schema", diagnostics={"quote_codec_error": "invalid_quote_id_result"}) from None
    data["choices"][0]["message"]["content"] = json.dumps(restored, ensure_ascii=False, separators=(",", ":"))
    return data


class Counter:
    """Safe UTF-8 upper bound includes native tool schema and stream options."""

    strategy = "minimax-utf8-upper-bound-v1"
    remote = False
    request_count = 0

    @staticmethod
    def count_text(text):
        return len(text.encode("utf-8"))

    def count_payload(self, payload):
        wire = copy.deepcopy(payload)
        if "response_format" not in wire and "tools" not in wire:
            from .extraction_schema import compatible_extraction_schema
            try:
                inputs = json.loads(next(row["content"] for row in reversed(wire["messages"]) if row.get("role") == "user"))
            except (KeyError, StopIteration, TypeError, ValueError):
                inputs = None
            if isinstance(inputs, dict) and "analysis_instructions" in inputs:
                wire["response_format"] = {"type": "json_schema", "json_schema": {"schema": compatible_extraction_schema(inputs, fixed_slots=False)}}
        wire = request(wire, wire.get("max_tokens", wire.get("max_completion_tokens", 524288)))
        return len(json.dumps(wire, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) + 1024

    @staticmethod
    def offsets(text):
        return list(range(len(text)))
