"""Native token counting and verified routing for extraction, without remote model code."""

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import requests
from django.conf import settings
from django.core.cache import cache
from jinja2.sandbox import ImmutableSandboxedEnvironment
from tokenizers import Tokenizer

from .providers import ProviderUnavailable, checked_url

ROOT = Path(__file__).resolve().parents[1] / "tokenizers"
MANIFEST = json.loads((ROOT / "manifest.json").read_text())


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def payload_hash(payload):
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


class NativeCounter:
    def __init__(self):
        root = ROOT / MANIFEST["revision"]
        for name, checksum in MANIFEST["files"].items():
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != checksum:
                raise ValueError("Invalid tokenizer assets")
        self.tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
        env.filters["tojson"] = lambda value, **kwargs: json.dumps(
            value, ensure_ascii=False, **kwargs
        )
        self.template = env.from_string((root / "chat_template.jinja").read_text())

    def count_text(self, text):
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)

    def count_payload(self, payload):
        rendered = self.template.render(
            messages=payload["messages"],
            add_generation_prompt=True,
            enable_thinking=payload.get("reasoning", {}).get("enabled", True),
        )
        return self.count_text(rendered)

    def offsets(self, text):
        return sorted(
            {
                start
                for start, end in self.tokenizer.encode(
                    text, add_special_tokens=False
                ).offsets
                if end > start
            }
        )


@lru_cache(maxsize=1)
def native_counter():
    try:
        return NativeCounter()
    except (OSError, ValueError, KeyError) as exc:
        raise ProviderUnavailable("context_tokenizer_unavailable") from exc


def context_runtime(cfg):
    if (
        cfg.chat_model_name not in MANIFEST["models"]
        or cfg.tokenizer_id != MANIFEST["repo"]
        or cfg.tokenizer_revision != MANIFEST["revision"]
    ):
        raise ProviderUnavailable("context_tokenizer_unavailable")
    if (
        cfg.context_window_tokens <= 0
        or cfg.max_completion_tokens <= 0
        or cfg.max_completion_tokens + cfg.context_safety_tokens
        >= cfg.context_window_tokens
    ):
        raise ProviderUnavailable("context_invalid_budget")
    # Metadata is public and contains no message text; read only from the configured provider.
    url = checked_url(
        f"{cfg.chat_provider_url.rstrip('/')}/models/{quote(cfg.chat_model_name, safe='/')}/endpoints"
    )
    key = "model-endpoints:" + hashlib.sha256(url.encode()).hexdigest()
    endpoints = cache.get(key)
    if endpoints is None:
        try:
            response = requests.get(url, timeout=(5, 15), allow_redirects=False)
            response.raise_for_status()
            endpoints = response.json()["data"]["endpoints"]
            if not isinstance(endpoints, list):
                raise TypeError()
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            raise ProviderUnavailable("context_model_metadata_unavailable") from exc
        if not settings.INTEGRATION_TEST_MODE:
            cache.set(key, endpoints, timeout=300)
    eligible = []
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        parameters = endpoint.get("supported_parameters")
        if not isinstance(parameters, list) or not isinstance(endpoint.get("tag"), str):
            continue
        window = endpoint.get("context_length")
        output = endpoint.get("max_completion_tokens")
        prompt = endpoint.get("max_prompt_tokens")
        if (
            type(window) is int
            and window >= cfg.context_window_tokens
            and type(output) is int
            and output >= cfg.max_completion_tokens
            and (
                prompt is None
                or (
                    type(prompt) is int
                    and prompt
                    >= cfg.context_window_tokens
                    - cfg.max_completion_tokens
                    - cfg.context_safety_tokens
                )
            )
            and endpoint.get("tag")
            and "max_tokens" in parameters
            and "reasoning" in parameters
        ):
            eligible.append(endpoint)
    if not eligible:
        raise ProviderUnavailable("context_model_window_unavailable")
    endpoint = min(eligible, key=lambda item: item["tag"])
    return native_counter(), endpoint


def extraction_input(value):
    """Keep the target adjacent to generation, after its reference history."""
    # PostgreSQL JSONB reorders nested keys. Canonicalize every value so a saved
    # request remains byte-identical on a delayed retry after reading it from DB.
    keys = sorted(key for key in value if key != "content") + ["content"]
    return (
        "{"
        + ",".join(
            canonical_json(key) + ":" + canonical_json(value[key]) for key in keys
        )
        + "}"
    )


def extraction_payload(
    cfg, endpoint, content, sender, context, known_projects, sent_at, source_timezone
):
    from .ai_service import WORKER_PROMPT

    payload = {
        "model": cfg.chat_model_name,
        "temperature": 0,
        "max_tokens": cfg.max_completion_tokens,
        "reasoning": {"enabled": False},
        "provider": {
            "only": [endpoint["tag"]],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "plugins": [{"id": "context-compression", "enabled": False}],
        "messages": [
            {"role": "system", "content": WORKER_PROMPT},
            {
                "role": "user",
                "content": extraction_input(
                    {
                        "content": content,
                        "sender": sender,
                        "sent_at": sent_at,
                        "timezone": source_timezone,
                        "context": context,
                        "known_projects": known_projects,
                    }
                ),
            },
        ],
    }
    if "response_format" in endpoint.get("supported_parameters", []):
        payload["response_format"] = {"type": "json_object"}
    return payload
