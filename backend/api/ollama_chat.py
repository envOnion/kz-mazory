"""Bounded native Ollama transport for verified Gemma requests."""

import hashlib
import json

from .providers import ProviderUnavailable


def native_request(payload, cfg, *, structured=True):
    from .context_tokens import GEMMA_MANIFEST, gemma_counter

    if (
        cfg.chat_model_name not in GEMMA_MANIFEST["models"]
        or cfg.tokenizer_id != GEMMA_MANIFEST["repo"]
        or cfg.tokenizer_revision != GEMMA_MANIFEST["revision"]
        or payload.get("model") != cfg.chat_model_name
        or type(payload.get("_ollama_num_ctx")) is not int
        or payload["_ollama_num_ctx"] != cfg.context_window_tokens
        or cfg.context_window_tokens > GEMMA_MANIFEST["ollama"]["max_context_tokens"]
        or type(payload.get("max_tokens")) is not int
        or not 1 <= payload["max_tokens"] <= cfg.max_completion_tokens
    ):
        raise ProviderUnavailable("context_configuration_changed")
    if structured and payload.get("response_format") != {"type": "json_object"}:
        raise ProviderUnavailable("provider_invalid_request")
    count = gemma_counter().count_payload(payload)
    if count + payload["max_tokens"] + cfg.context_safety_tokens > cfg.context_window_tokens:
        raise ProviderUnavailable("context_budget_exceeded")
    request = {
        "model": payload["model"], "messages": payload["messages"],
        "stream": False, "think": False,
        "options": {
            "num_ctx": cfg.context_window_tokens,
            "num_predict": payload["max_tokens"],
            "temperature": payload.get("temperature", 0),
        },
    }
    if structured:
        from .extraction_schema import extraction_schema
        request["format"] = extraction_schema()
    return request


def analytics_request(payload, cfg):
    from .context_tokens import GEMMA_MANIFEST, context_runtime

    if payload.get("model") != cfg.chat_model_name or cfg.chat_model_name not in GEMMA_MANIFEST["models"]:
        raise ProviderUnavailable("context_configuration_changed")
    context_runtime(cfg)
    # Keep the existing conservative bound for complete tool conversations.
    available = cfg.context_window_tokens - cfg.max_completion_tokens - cfg.context_safety_tokens
    if len(json.dumps(payload, ensure_ascii=False).encode()) + 4096 > available:
        raise ProviderUnavailable("context_budget_use_filters")
    messages, names = [], {}
    try:
        for message in payload["messages"]:
            converted = {"role": message["role"], "content": message.get("content") or ""}
            if message.get("tool_calls"):
                converted["tool_calls"] = []
                for call in message["tool_calls"]:
                    function = call["function"]
                    arguments = json.loads(function["arguments"])
                    if not isinstance(arguments, dict):
                        raise ValueError()
                    names[call["id"]] = function["name"]
                    converted["tool_calls"].append({"function": {"name": function["name"], "arguments": arguments}})
            if message["role"] == "tool":
                converted["tool_name"] = names[message["tool_call_id"]]
            messages.append(converted)
    except (KeyError, TypeError, ValueError):
        raise ProviderUnavailable("provider_invalid_request") from None
    return {
        "model": payload["model"], "messages": messages, "tools": payload.get("tools", []),
        "stream": False, "think": False,
        "options": {"num_ctx": cfg.context_window_tokens, "num_predict": cfg.max_completion_tokens, "temperature": 0},
    }


def normalize_response(data, *, allow_tools=False):
    if not isinstance(data, dict) or data.get("done") is not True:
        raise ProviderUnavailable("provider_invalid_response")
    message = data.get("message")
    if (
        not isinstance(message, dict) or message.get("role") != "assistant"
        or not isinstance(message.get("content"), str) or message.get("thinking")
        or (message.get("tool_calls") and not allow_tools)
        or data.get("done_reason") not in ("stop", "length")
        or any(type(data.get(key)) is not int or data[key] < 0 for key in ("prompt_eval_count", "eval_count"))
    ):
        raise ProviderUnavailable("provider_invalid_response")
    normalized = {"role": "assistant", "content": message["content"]}
    calls = message.get("tool_calls", [])
    if calls:
        if not isinstance(calls, list) or len(calls) > 16:
            raise ProviderUnavailable("provider_invalid_response")
        normalized["tool_calls"] = []
        for index, call in enumerate(calls):
            try:
                function = call["function"]
                if not isinstance(function["name"], str) or not function["name"] or not isinstance(function["arguments"], dict):
                    raise ValueError()
                arguments = json.dumps(function["arguments"], ensure_ascii=False, sort_keys=True)
                identity = hashlib.sha256((function["name"] + arguments).encode()).hexdigest()[:20]
                normalized["tool_calls"].append({"id": f"ollama_{index}_{identity}", "type": "function", "function": {"name": function["name"], "arguments": arguments}})
            except (KeyError, TypeError, ValueError):
                raise ProviderUnavailable("provider_invalid_response") from None
    return {
        "choices": [{"message": normalized, "finish_reason": "tool_calls" if calls and data["done_reason"] == "stop" else data["done_reason"]}],
        "usage": {"prompt_tokens": data["prompt_eval_count"], "completion_tokens": data["eval_count"]},
    }
