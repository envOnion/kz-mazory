"""Protocol adapters for the supported chat-provider wire formats.

The application keeps one internal, OpenAI-shaped response contract.  This
module contains the small amount of protocol translation needed at the HTTP
boundary; it deliberately does not read credentials or make network calls.
"""

from .providers import ProviderUnavailable

OPENAI_COMPATIBLE = "openai_compatible"
ANTHROPIC_MESSAGES = "anthropic_messages"
ANTHROPIC_VERSION = "2023-06-01"


def chat_api_format(cfg):
    """Return a validated format, defaulting old configuration rows to OpenAI."""
    value = getattr(cfg, "chat_api_format", OPENAI_COMPATIBLE)
    if value not in (OPENAI_COMPATIBLE, ANTHROPIC_MESSAGES):
        raise ProviderUnavailable("provider_configuration_invalid")
    return value


def anthropic_headers(api_key):
    return {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def _text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        text = []
        for block in value:
            if (
                not isinstance(block, dict)
                or block.get("type") != "text"
                or not isinstance(block.get("text"), str)
            ):
                raise ProviderUnavailable("provider_invalid_request")
            text.append(block["text"])
        return "".join(text)
    raise ProviderUnavailable("provider_invalid_request")


def anthropic_message_payload(payload, *, default_max_tokens):
    """Translate a canonical chat request to the native Messages envelope."""
    if not isinstance(payload, dict):
        raise ProviderUnavailable("provider_invalid_request")
    model = payload.get("model")
    messages = payload.get("messages")
    if not isinstance(model, str) or not model or not isinstance(messages, list):
        raise ProviderUnavailable("provider_invalid_request")

    system_parts = []
    if "system" in payload:
        system_parts.append(_text_content(payload["system"]))
    native_messages = []
    for message in messages:
        if not isinstance(message, dict):
            raise ProviderUnavailable("provider_invalid_request")
        role = message.get("role")
        content = _text_content(message.get("content"))
        if role == "system":
            system_parts.append(content)
        elif role in ("user", "assistant"):
            native_messages.append({"role": role, "content": content})
        else:
            raise ProviderUnavailable("provider_invalid_request")
    if not native_messages:
        raise ProviderUnavailable("provider_invalid_request")

    max_tokens = payload.get("max_tokens", default_max_tokens)
    if type(max_tokens) is not int or max_tokens <= 0:
        raise ProviderUnavailable("provider_invalid_request")
    result = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": native_messages,
    }
    if system_parts:
        result["system"] = "\n\n".join(system_parts)
    return result


def anthropic_count_payload(payload, *, default_max_tokens):
    """Token Counting accepts the Messages input but not generation controls."""
    result = anthropic_message_payload(
        payload, default_max_tokens=default_max_tokens
    )
    result.pop("max_tokens")
    return result


def validate_anthropic_count(data):
    value = data.get("input_tokens") if isinstance(data, dict) else None
    if type(value) is not int or value < 0:
        raise ProviderUnavailable("context_token_count_invalid")
    return value


def normalize_anthropic_usage(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    input_tokens = value.get("input_tokens")
    output_tokens = value.get("output_tokens")
    if type(input_tokens) is int and input_tokens >= 0:
        result["prompt_tokens"] = input_tokens
    if type(output_tokens) is int and output_tokens >= 0:
        result["completion_tokens"] = output_tokens
    # Some compatible gateways provide an authoritative cost.  Preserve it for
    # the common validation/storage path; never estimate a missing value.
    if "cost" in value:
        result["cost"] = value["cost"]
    return result


def normalize_anthropic_message(data):
    """Convert one native Messages response to the application's contract."""
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        raise ProviderUnavailable("provider_invalid_response")
    text_parts = []
    for block in data["content"]:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise ProviderUnavailable("provider_invalid_response")
        if block["type"] == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ProviderUnavailable("provider_invalid_response")
            text_parts.append(text)
    content = "".join(text_parts)
    stop_reason = data.get("stop_reason")
    if not isinstance(stop_reason, str):
        raise ProviderUnavailable("provider_invalid_response")
    finish_reason = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "model_context_window_exceeded": "length",
        "refusal": "refusal",
    }.get(stop_reason)
    if finish_reason is None:
        # tool_use and pause_turn require protocol continuations which Mazory
        # intentionally does not implement. Unknown reasons are not complete
        # assistant answers either.
        raise ProviderUnavailable("provider_invalid_response")
    usage = normalize_anthropic_usage(data.get("usage"))
    if finish_reason == "length":
        raise ProviderUnavailable(
            "provider_output_truncated",
            diagnostics={
                "finish_reason": finish_reason,
                "provider_stop_reason": stop_reason,
                "output_tokens": usage.get("completion_tokens"),
                "response_characters": len(content),
            },
        )
    if not content.strip():
        raise ProviderUnavailable("provider_invalid_response")
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
                "provider_stop_reason": stop_reason,
            }
        ],
        "usage": usage,
    }
