"""Free Router has no fixed tokenizer or provider before a request is served."""

import json

from .providers import ProviderUnavailable

MODEL = "openrouter/free"
PROFILE = "router-utf8-bound-v1"


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
    if schema is not None:
        result["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "extraction", "strict": True, "schema": schema},
        }
    # The router chooses the model and provider. Never inherit the former local
    # server's options or force a model-specific reasoning implementation.
    for key in ("reasoning", "reasoning_effort", "chat_template_kwargs", "_ollama_num_ctx"):
        result.pop(key, None)
    result["plugins"] = [{"id": "context-compression", "enabled": False}]
    return result
