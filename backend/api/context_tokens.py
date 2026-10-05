"""Provider-specific token counting and verified routing for extraction."""

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import requests
from django.conf import settings
from django.core.cache import cache
from jinja2.sandbox import ImmutableSandboxedEnvironment
from tokenizers import Tokenizer

from .providers import ProviderUnavailable, checked_ai_url

ROOT = Path(__file__).resolve().parents[1] / "tokenizers"
MANIFEST = json.loads((ROOT / "manifest.json").read_text())
QWEN_MANIFEST = json.loads((ROOT / "qwen38_manifest.json").read_text())
MAX_REMOTE_COUNT_REQUESTS = 32


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def payload_hash(payload):
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _invalid_template(message):
    raise ValueError(message)


class NativeCounter:
    strategy = "nemotron_local_manifest"
    remote = False

    def __init__(self, manifest=MANIFEST):
        root = ROOT / manifest["revision"]
        for name, checksum in manifest["files"].items():
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != checksum:
                raise ValueError("Invalid tokenizer assets")
        self.tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
        env.filters["tojson"] = lambda value, **kwargs: json.dumps(
            value, ensure_ascii=False, **kwargs
        )
        env.globals["raise_exception"] = _invalid_template
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

    @property
    def request_count(self):
        return 0


class QwenCounter(NativeCounter):
    strategy = "qwen38_ollama_local_manifest"

    def __init__(self):
        super().__init__(QWEN_MANIFEST)

    def count_payload(self, payload):
        # Only the extraction shape is supported. Reject unsupported modes rather
        # than undercounting tools, images, prefill or thinking instructions.
        messages = payload.get("messages", [])
        if (
            payload.get("reasoning_effort") != "none"
            or payload.get("tools")
            or [message.get("role") for message in messages] != ["system", "user"]
            or any(not isinstance(message.get("content"), str) for message in messages)
        ):
            raise ProviderUnavailable("context_token_count_unavailable")
        rendered = self.template.render(
            messages=messages, add_generation_prompt=True, enable_thinking=False,
        )
        return self.count_text(rendered)


class AnthropicCounter:
    """Exact provider counts cached for one extraction job.

    ``count_text`` is deliberately only an inexpensive scheduling estimate. Every
    accepted request and every history boundary is still verified by the native
    count-tokens endpoint before generation.
    """

    strategy = "anthropic_count_tokens"
    remote = True

    def __init__(self):
        self._counts = {}
        self._request_count = 0

    def count_text(self, text):
        # This is not a compatibility tokenizer. UTF-8 size is merely a useful
        # signal for deciding when an exact, cached remote check is worthwhile.
        return (len(text.encode("utf-8")) + 2) // 3

    def count_payload(self, payload):
        key = payload_hash(payload)
        if key not in self._counts:
            if self._request_count >= MAX_REMOTE_COUNT_REQUESTS:
                raise ProviderUnavailable("context_token_count_unavailable")
            from .ai_service import AIService

            try:
                value = AIService.count_chat_tokens(payload)
            except ProviderUnavailable as exc:
                if str(exc) in {
                    "context_token_count_invalid",
                    "provider_invalid_request",
                    "provider_invalid_response",
                    "provider_response_error",
                }:
                    raise ProviderUnavailable(
                        "context_token_count_unavailable",
                        diagnostics=exc.diagnostics,
                    ) from None
                raise
            if type(value) is not int or value < 0:
                raise ProviderUnavailable("context_token_count_unavailable")
            self._counts[key] = value
            self._request_count += 1
        return self._counts[key]

    @property
    def request_count(self):
        return self._request_count


@lru_cache(maxsize=1)
def native_counter():
    try:
        return NativeCounter()
    except (OSError, ValueError, KeyError) as exc:
        raise ProviderUnavailable("context_tokenizer_unavailable") from exc


@lru_cache(maxsize=1)
def qwen_counter():
    try:
        return QwenCounter()
    except (OSError, ValueError, KeyError) as exc:
        raise ProviderUnavailable("context_tokenizer_unavailable") from exc


def ollama_context_runtime(cfg):
    """Verify the installed model in the worker before trusting local counts."""
    from .ai_service import AIService

    if (
        cfg.chat_model_name not in QWEN_MANIFEST["models"]
        or cfg.tokenizer_id != QWEN_MANIFEST["repo"]
        or cfg.tokenizer_revision != QWEN_MANIFEST["revision"]
    ):
        raise ProviderUnavailable("context_tokenizer_unavailable")
    base = AIService.effective_chat_provider_url(cfg)
    if not base.endswith("/v1"):
        raise ProviderUnavailable("context_provider_unsupported")
    api_key = AIService._credential(cfg)
    key = "ollama-context:" + payload_hash({
        "url": base, "model": cfg.chat_model_name,
        "credential": cfg.chat_api_key_encrypted,
        "profile": payload_hash(QWEN_MANIFEST),
    })
    endpoint = cache.get(key)
    if endpoint is None:
        version = AIService._post(
            base[:-3] + "/api/version", {"model": cfg.chat_model_name},
            (5, 15), api_key=api_key, operation="model_metadata", http_method="GET",
        )
        if not isinstance(version, dict) or version.get("version") != QWEN_MANIFEST["ollama"]["version"]:
            raise ProviderUnavailable("context_tokenizer_unavailable")
        data = AIService._post(
            base[:-3] + "/api/show", {"model": cfg.chat_model_name, "verbose": True},
            (5, 30), api_key=api_key, operation="model_metadata",
        )
        try:
            identity = {
                field: data["model_info"][field]
                for field in QWEN_MANIFEST["ollama"]["identity_fields"]
            }
            metadata = data["model_info"]
            if (
                payload_hash(identity) != QWEN_MANIFEST["ollama"]["vocabulary_sha256"]
                or metadata["general.architecture"] != QWEN_MANIFEST["ollama"]["architecture"]
                or not re.search(r"^RENDERER qwen3\.8$", data["modelfile"], re.MULTILINE)
                or data["template"] != "{{ .Prompt }}"
            ):
                raise ProviderUnavailable("context_tokenizer_unavailable")
            window = metadata["qwen35.context_length"]
            configured_window = re.search(
                r"^num_ctx\s+(\d+)\s*$", data["parameters"], re.MULTILINE,
            )
            if type(window) is not int or not configured_window:
                raise ProviderUnavailable("context_model_window_unavailable")
            window = min(window, int(configured_window.group(1)))
        except (TypeError, KeyError, ValueError, AttributeError) as exc:
            raise ProviderUnavailable("context_model_metadata_unavailable") from exc
        endpoint = {
            "tag": "ollama", "transport": "ollama", "context_length": window,
            "supported_parameters": ["max_tokens", "response_format", "reasoning_effort"],
            "api_format": "openai_compatible", "effective_provider_url": base,
        }
        cache.set(key, endpoint, timeout=300)
    if endpoint["context_length"] < cfg.context_window_tokens:
        raise ProviderUnavailable("context_model_window_unavailable")
    return qwen_counter(), endpoint


def context_runtime(cfg):
    if (
        cfg.context_window_tokens <= 0
        or cfg.max_completion_tokens <= 0
        or cfg.max_completion_tokens + cfg.context_safety_tokens
        >= cfg.context_window_tokens
    ):
        raise ProviderUnavailable("context_invalid_budget")
    api_format = getattr(cfg, "chat_api_format", "openai_compatible")
    if api_format == "anthropic_messages":
        from .ai_service import AIService

        return AnthropicCounter(), {
            "tag": "anthropic_messages",
            "context_length": cfg.context_window_tokens,
            "supported_parameters": [],
            "api_format": api_format,
            "effective_provider_url": AIService.effective_chat_provider_url(cfg),
        }
    if api_format != "openai_compatible":
        raise ProviderUnavailable("context_provider_unsupported")
    if cfg.chat_model_name in QWEN_MANIFEST["models"] or cfg.tokenizer_id == QWEN_MANIFEST["repo"]:
        return ollama_context_runtime(cfg)
    if (
        cfg.chat_model_name not in MANIFEST["models"]
        or cfg.tokenizer_id != MANIFEST["repo"]
        or cfg.tokenizer_revision != MANIFEST["revision"]
    ):
        raise ProviderUnavailable("context_tokenizer_unavailable")
    # Metadata is public and contains no message text; read only from the configured provider.
    url = checked_ai_url(
        f"{cfg.chat_provider_url.rstrip('/')}/models/{quote(cfg.chat_model_name, safe='/')}/endpoints",
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
    endpoint = dict(min(eligible, key=lambda item: item["tag"]))
    endpoint.update(
        api_format=api_format,
        effective_provider_url=cfg.chat_provider_url.rstrip("/"),
    )
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
    cfg, endpoint, content, sender, context, known_projects, sent_at, source_timezone,
    source_metadata=None, current_time=None, target_message_id=None, known_threads=None, batch_message_ids=None,
):
    from .ai_service import WORKER_PROMPT

    system_prompt = WORKER_PROMPT
    if getattr(cfg, "autonomous_enabled", False):
        system_prompt += "\nАвтономный режим: точное время дедлайна не выдумывай. Только день/утро означает deadline_precision=date; искусственные 09:00/18:00 не являются сообщенным временем. Сервер задает техническую границу дня отдельно от точности источника."
        system_prompt += "\nСвязь с объектом доказывается цитатами, а не наличием ID в known_projects/known_threads. Если название находится в более раннем сообщении, добавь буквальную цитату этого сообщения в evidence_messages с role=identity. Пустое object_name не подтверждает предложенный project. Не выбирай между активным и архивным одноименным объектом только по статусу активности."
    user_content = extraction_input(
        {
            "content": content,
            "sender": sender,
            "sent_at": sent_at,
            "timezone": source_timezone,
            **({"source_metadata": source_metadata} if source_metadata is not None else {}),
            **({"current_time": current_time} if current_time is not None else {}),
            **({"target_message_id": target_message_id} if target_message_id is not None else {}),
            **({"batch_message_ids": batch_message_ids} if batch_message_ids else {}),
            "context": context,
            "known_projects": known_projects,
            **({"known_threads": known_threads} if known_threads is not None else {}),
        }
    )
    if getattr(cfg, "chat_api_format", "openai_compatible") == "anthropic_messages":
        return {
            "model": cfg.chat_model_name,
            "max_tokens": cfg.max_completion_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
        }
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
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    if endpoint.get("transport") == "ollama":
        payload.pop("provider")
        payload.pop("plugins")
        payload.pop("reasoning")
        payload["reasoning_effort"] = "none"
    if "response_format" in endpoint.get("supported_parameters", []):
        payload["response_format"] = {"type": "json_object"}
    return payload
