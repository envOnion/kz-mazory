"""Free Router has no fixed tokenizer or provider before a request is served."""

import json
from collections import Counter

from .providers import ProviderUnavailable

MODEL = "openrouter/free"
PROFILE = "router-utf8-bound-v1"
EXTRACTION_SCHEMA_NAME = "mazory_extraction"


def _schema_children(node, transform):
    """Visit schema positions only; property maps and enum values are data."""
    if not isinstance(node, dict):
        return node
    result = dict(node)
    for key in ("properties", "patternProperties", "dependentSchemas", "$defs", "definitions"):
        if isinstance(node.get(key), dict):
            result[key] = {name: transform(child) for name, child in node[key].items()}
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if isinstance(node.get(key), list):
            result[key] = [transform(child) for child in node[key]]
    for key in ("items", "additionalItems", "additionalProperties", "unevaluatedItems",
                "unevaluatedProperties", "contains", "propertyNames", "contentSchema",
                "not", "if", "then", "else"):
        if isinstance(node.get(key), (dict, bool)):
            result[key] = transform(node[key])
        elif key == "items" and isinstance(node.get(key), list):
            result[key] = [transform(child) for child in node[key]]
    return result


def compact_schema(schema):
    """Share identical schema nodes without changing their validation contract."""
    counts, nodes = Counter(), {}
    scoped = False

    def encoded(node):
        return json.dumps(node, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def collect(node):
        nonlocal scoped
        if isinstance(node, dict):
            # Moving schemas with their own reference scope is not equivalent.
            # Existing custom schemas and already compacted schemas stay intact.
            scoped |= any(key in node for key in ("$id", "$anchor", "$dynamicAnchor", "$ref", "$dynamicRef"))
            value = encoded(node)
            if len(value.encode("utf-8")) >= 128:
                counts[value] += 1
                nodes[value] = node
            _schema_children(node, collect)
        return node

    collect(schema)
    if scoped:
        return schema
    names = {value: f"mazory_{index}" for index, value in enumerate(sorted(
        value for value, count in counts.items() if count > 1
    ))}
    if not names:
        return schema
    if any(name in schema.get("$defs", {}) for name in names.values()):
        return schema

    def rewrite(node, skip=None):
        if not isinstance(node, dict):
            return node
        value = encoded(node)
        if value in names and value != skip:
            return {"$ref": f"#/$defs/{names[value]}"}
        return _schema_children(node, rewrite)

    result = rewrite(schema)
    definitions = {name: rewrite(nodes[value], skip=value) for value, name in names.items()}
    used = set()

    def references(node):
        if isinstance(node, dict):
            reference = node.get("$ref", "")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                name = reference.removeprefix("#/$defs/")
                if name in definitions and name not in used:
                    used.add(name)
                    references(definitions[name])
            _schema_children(node, references)
        return node

    references(result)
    if used:
        result["$defs"] = {**result.get("$defs", {}), **{
            name: definitions[name] for name in sorted(used)
        }}
    return result


def _extraction_input(payload):
    if payload.get("tools"):
        return None
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        return None
    user = next((row for row in reversed(messages)
                 if isinstance(row, dict) and row.get("role") == "user"), None)
    if user is None or not isinstance(user.get("content"), str):
        return None
    try:
        value = json.loads(user["content"])
    except ValueError:
        return None
    if (isinstance(value, dict) and isinstance(value.get("content"), str)
            and isinstance(value.get("context"), list)
            and isinstance(value.get("known_projects"), list)):
        return value
    return None


class RouterCounter:
    """Conservative text scheduling budget; never an exact model token count.

    Budget every UTF-8 byte plus framing/tool overhead. OpenRouter is the final
    authority on the selected model's context. Overflow remains an explicit
    failure; neither a tokenizer from another model nor compression is used.
    """

    strategy = PROFILE
    remote = False
    request_count = 0

    def count_text(self, text):
        return len(text.encode("utf-8"))

    def count_payload(self, payload):
        payload = request(payload, 0)
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ProviderUnavailable("context_token_count_unavailable")
        if any(not isinstance(row, dict) or not (
            isinstance(row.get("content"), str)
            or row.get("role") == "assistant" and row.get("tool_calls") and row.get("content") is None
        ) for row in messages):
            raise ProviderUnavailable("context_token_count_unavailable")
        value = {key: payload[key] for key in ("messages", "tools", "response_format") if key in payload}
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 4096 + 128 * len(messages)

    def offsets(self, text):
        return list(range(len(text)))


def validate_usage(counter, expected, actual):
    if type(actual) is not int or actual < 0 or (
        actual > expected if isinstance(counter, RouterCounter) else actual != expected
    ):
        raise ProviderUnavailable("context_token_count_invalid")


def runtime(cfg):
    from .ai_service import AIService

    if cfg.context_window_tokens > 200000 or cfg.tokenizer_id != MODEL or cfg.tokenizer_revision != PROFILE:
        raise ProviderUnavailable("context_tokenizer_unavailable")
    return RouterCounter(), {
        "tag": MODEL, "transport": "openrouter_free",
        "context_length": cfg.context_window_tokens,
        "supported_parameters": ["max_tokens", "response_format", "tools", "tool_choice"],
        "api_format": "openai_compatible",
        "effective_provider_url": AIService.effective_chat_provider_url(cfg),
    }


def request(payload, max_tokens):
    result = dict(payload)
    result["provider"] = {"require_parameters": True}
    result.setdefault("max_tokens", max_tokens)
    schema = result.pop("_ollama_format", None)
    schema_name = "extraction"
    response_format = result.get("response_format", {})
    json_schema = response_format.get("json_schema", {}) if isinstance(response_format, dict) else {}
    custom_schema = (isinstance(response_format, dict)
                     and response_format.get("type") == "json_schema"
                     and (not isinstance(json_schema, dict)
                          or json_schema.get("name") != EXTRACTION_SCHEMA_NAME))
    if schema is None and not custom_schema:
        inputs = _extraction_input(result)
        if inputs is not None:
            from .extraction_schema import extraction_schema
            # Focused passes edit the user JSON after extraction_payload().
            # Regenerate our schema from that final input on every preparation.
            schema = extraction_schema(inputs if type(inputs.get("target_message_id")) is int else None)
            schema_name = EXTRACTION_SCHEMA_NAME
    if schema is not None:
        result["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": compact_schema(schema)},
        }
    # The router chooses the model and provider. Never inherit the former local
    # server's options or force a model-specific reasoning implementation.
    for key in ("reasoning", "reasoning_effort", "chat_template_kwargs", "_ollama_num_ctx"):
        result.pop(key, None)
    result["plugins"] = [{"id": "context-compression", "enabled": False}]
    return result
