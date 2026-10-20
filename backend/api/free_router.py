"""Free Router has no fixed tokenizer or provider before a request is served."""

import json
from collections import Counter

from .providers import ProviderUnavailable

MODEL = "openrouter/free"
PROFILE = "router-utf8-bound-v1"
EXTRACTION_SCHEMA_NAME = "mazory_extraction"
LITERAL_ENUM_POLICY = "shared-literal-enums-v1"


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


def _share_literal_enums(schema):
    """Share one profitable literal subset while retaining outer constraints."""
    if not isinstance(schema, dict):
        return schema
    def encoded(node):
        return json.dumps(node, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def literals(node):
        if (not isinstance(node, dict) or node.get('type') != 'string'
                or any(key in node for key in ('anyOf', '$ref', '$id', '$anchor', '$dynamicAnchor', '$dynamicRef'))):
            return None
        values = node.get('enum')
        if (not isinstance(values, list) or not values
                or any(not isinstance(value, str) for value in values)
                or len(set(values)) != len(values)):
            return None
        return frozenset(values)

    sets = set()

    def collect(node):
        values = literals(node)
        if values is not None:
            sets.add(values)
        return _schema_children(node, collect)

    collect(schema)
    subsets = sorted((tuple(sorted(values)) for values in sets
                      if any(values < other for other in sets)))
    best, best_bytes = schema, len(encoded(schema).encode('utf-8'))
    definitions = schema.get('$defs', {})
    for values in subsets:
        shared = frozenset(values)
        existing = [name for name, node in definitions.items()
                    if isinstance(node, dict) and set(node) == {'type', 'enum'}
                    and literals(node) == shared]
        if existing:
            name = min(existing, key=lambda value: (len(value.encode('utf-8')), value))
            base = definitions[name]
        else:
            name, suffix = 'mazory_literal_enum', 0
            while name in definitions:
                suffix += 1
                name = f'mazory_literal_enum_{suffix}'
            base = None
        pointer = name.replace('~', '~0').replace('/', '~1')
        reference = {'$ref': f'#/$defs/{pointer}'}

        def rewrite(node):
            # An existing unconstrained definition is the terminal reference;
            # never turn it into an alias to itself.
            if node is base:
                return node
            current = literals(node)
            if current is None or not shared <= current:
                return _schema_children(node, rewrite)
            outer = _schema_children({key: value for key, value in node.items() if key != 'enum'}, rewrite)
            remainder = sorted(current - shared)
            if not remainder and set(outer) == {'type'}:
                return dict(reference)
            outer['anyOf'] = [dict(reference)]
            if remainder:
                outer['anyOf'].append({'type': 'string', 'enum': remainder})
            return outer

        candidate = rewrite(schema)
        if base is None:
            # Append after rewriting: the shared enum remains a literal leaf.
            candidate['$defs'] = {**candidate.get('$defs', {}), name: {'type': 'string', 'enum': list(values)}}
        candidate_bytes = len(encoded(candidate).encode('utf-8'))
        if candidate_bytes < best_bytes:
            best, best_bytes = candidate, candidate_bytes
    return best


def compact_schema(schema, *, share_literal_enums=False):
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
        return _share_literal_enums(schema) if share_literal_enums else schema
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
    return _share_literal_enums(result) if share_literal_enums else result


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

    from .free_fallback import local_window_valid
    if ((cfg.context_window_tokens > 200000 and not local_window_valid(cfg))
            or cfg.tokenizer_id != MODEL or cfg.tokenizer_revision != PROFILE):
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
    inputs = _extraction_input(result)
    if schema is None and not custom_schema:
        if inputs is not None:
            from .extraction_schema import extraction_schema
            # Focused passes edit the user JSON after extraction_payload().
            # Regenerate our schema from that final input on every preparation.
            schema = extraction_schema(inputs if type(inputs.get("target_message_id")) is int else None)
            schema_name = EXTRACTION_SCHEMA_NAME
    if schema is not None:
        if (schema_name == EXTRACTION_SCHEMA_NAME and inputs is not None):
            from .quote_input_codec import POLICY, QuoteCodecError, project
            if inputs.get('native_quote_policy') == POLICY:
                try:
                    inputs, schema = project(inputs, schema)
                except QuoteCodecError:
                    raise ProviderUnavailable('context_snapshot_mismatch') from None
                result['messages'] = [dict(row) for row in result['messages']]
                user = next(row for row in reversed(result['messages']) if row.get('role') == 'user')
                user['content'] = json.dumps(inputs, ensure_ascii=False, separators=(',', ':'))
        result["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": compact_schema(schema,
                share_literal_enums=isinstance(inputs, dict) and inputs.get('native_enum_policy') == LITERAL_ENUM_POLICY)},
        }
    # The router chooses the model and provider. Never inherit the former local
    # server's options or force a model-specific reasoning implementation.
    for key in ("reasoning", "reasoning_effort", "chat_template_kwargs", "_ollama_num_ctx"):
        result.pop(key, None)
    result["plugins"] = [{"id": "context-compression", "enabled": False}]
    return result
