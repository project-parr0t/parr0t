from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

from .errors import ConfigurationError, ProviderError, ProviderFailure
from .provider_policy import validate_provider_policy

_REVIEW_ISSUE_CODES = {
    "meaning",
    "omission",
    "addition",
    "voice",
    "register",
    "terminology",
    "consistency",
    "formatting",
    "fluency",
    "literalism",
    "reference_loss",
    "tone_rhythm",
    "function",
    "condition",
    "unit",
    "knowledge_order",
    "length_risk",
    "slot_composition",
}

_DEFAULT_FOCUSED_TEXT_INPUT_CHARS = 24_000
_FOCUSED_TEXT_SCHEMA_NAMES = (
    "game_translation",
    "game_translation_review_judge",
    "game_translation_review_rewrite",
)


def _parse_plain_review_verdict(value: str) -> dict[str, Any]:
    line = value.strip()
    if not line or "\n" in line or "\r" in line:
        raise ProviderFailure("malformed_response", retryable=True)
    head, _, tail = line.partition(" ")
    verdicts = {"OK": "ok", "REWRITE": "rewrite", "REFUSED": "refused", "ERROR": "error"}
    verdict = verdicts.get(head.upper())
    if verdict is None:
        raise ProviderFailure("malformed_response", retryable=True)
    issues = [item.strip().casefold() for item in tail.split(",") if item.strip()]
    if verdict != "rewrite" and issues:
        raise ProviderFailure("malformed_response", retryable=True)
    if any(item not in _REVIEW_ISSUE_CODES for item in issues):
        raise ProviderFailure("malformed_response", retryable=True)
    return {"verdict": verdict, "issues": list(dict.fromkeys(issues))}


def _normalized_usage(data: dict[str, Any]) -> dict[str, int]:
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        return {}
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "total_tokens": ("total_tokens",),
    }
    normalized: dict[str, int] = {}
    for target, names in aliases.items():
        for name in names:
            value = usage.get(name)
            if isinstance(value, int) and value >= 0:
                normalized[target] = value
                break
    details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    if isinstance(details, dict):
        reasoning = details.get("reasoning_tokens")
        if isinstance(reasoning, int) and reasoning >= 0:
            normalized["reasoning_tokens"] = reasoning
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached = usage.get("prompt_cache_hit_tokens")
    if not isinstance(cached, int) and isinstance(input_details, dict):
        cached = input_details.get("cached_tokens")
    if isinstance(cached, int) and cached >= 0:
        normalized["cached_input_tokens"] = cached
    cache_miss = usage.get("prompt_cache_miss_tokens")
    if isinstance(cache_miss, int) and cache_miss >= 0:
        normalized["cache_miss_input_tokens"] = cache_miss
    return normalized


def _refusal_payload(schema_name: str) -> dict[str, Any]:
    if schema_name == "game_translation_review_judge":
        return {"verdict": "refused", "issues": []}
    if schema_name == "game_translation_review_rewrite":
        return {"status": "refused", "text": ""}
    if "review" in schema_name:
        return {
            "decision": "refused",
            "text": "",
            "meaning_preserved": False,
            "issues": [],
            "reason": "provider_refusal",
        }
    if "context" in schema_name:
        return {
            "status": "refused",
            "reason": "provider_refusal",
            "story_summary": "",
            "global_style_rules": [],
            "scenes": [],
            "characters": [],
            "relationship_timeline": [],
            "reference_cues": [],
        }
    return {"status": "refused", "text": "", "reason": "provider_refusal"}


def _with_usage(payload: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    usage = _normalized_usage(response)
    if usage:
        result["_usage"] = usage
    return result


class ModelProvider(ABC):
    """Provider contract. Implementations must return a JSON object, never free-form text."""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def generate_json(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]:
        raise NotImplementedError

    def fingerprint(self) -> dict[str, Any]:
        return {"name": self.name, "type": type(self).__name__}

    def generate_review_verdict(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]:
        return self.generate_json(system=system, user=user, schema=schema, schema_name=schema_name)


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").casefold()
        if exc.code in {401, 403}:
            code, retryable = "authentication_error", False
        elif exc.code == 429 and any(token in detail for token in ("quota", "balance", "credit")):
            code, retryable = "quota_exhausted", False
        elif exc.code == 429:
            code, retryable = "rate_limited", True
        elif exc.code in {408, 504}:
            code, retryable = "timeout", True
        elif 500 <= exc.code < 600:
            code, retryable = "provider_error", True
        else:
            code, retryable = "provider_error", False
        raise ProviderFailure(code, retryable=retryable, status_code=exc.code) from exc
    except json.JSONDecodeError as exc:
        raise ProviderFailure("malformed_response", retryable=True) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ProviderFailure("timeout", retryable=True) from exc
    except urllib.error.URLError as exc:
        code = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "provider_error"
        raise ProviderFailure(code, retryable=True) from exc


class OpenAIResponsesProvider(ModelProvider):
    """OpenAI Responses provider using strict JSON, with optional compact plain judgments."""

    def __init__(
        self,
        name: str,
        model: str,
        *,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str = "https://api.openai.com/v1",
        timeout: int = 120,
        reasoning_effort: str | None = None,
        max_output_tokens: int | None = None,
        task_max_output_tokens: dict[str, int] | None = None,
        review_verdict_format: str = "json",
    ):
        super().__init__(name)
        self.model = model
        self.api_key_env = api_key_env
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.task_max_output_tokens = {
            str(key): int(value) for key, value in (task_max_output_tokens or {}).items()
        }
        if review_verdict_format not in {"json", "plain"}:
            raise ConfigurationError("review_verdict_format must be json or plain")
        self.review_verdict_format = review_verdict_format

    def generate_json(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise ProviderFailure("authentication_error", retryable=False)
        payload = {
            "model": self.model,
            "instructions": system,
            "input": user,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": schema,
                    "strict": True,
                }
            },
        }
        if self.reasoning_effort is not None:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        output_limit = self.task_max_output_tokens.get(schema_name, self.max_output_tokens)
        if output_limit is not None:
            payload["max_output_tokens"] = output_limit
        data = _post_json(
            f"{self.base_url}/responses",
            {"Authorization": f"Bearer {api_key}"},
            payload,
            self.timeout,
        )
        if data.get("status") in {"failed", "cancelled", "incomplete"}:
            details = json.dumps(
                data.get("error") or data.get("incomplete_details") or {},
                ensure_ascii=False,
            ).casefold()
            code = "quota_exhausted" if "quota" in details else "provider_error"
            raise ProviderFailure(code, retryable=code == "provider_error")
        for item in data.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "refusal":
                    return _with_usage(_refusal_payload(schema_name), data)
                if content.get("type") == "output_text":
                    try:
                        return _with_usage(json.loads(content.get("text", "")), data)
                    except json.JSONDecodeError as exc:
                        raise ProviderFailure("malformed_response", retryable=True) from exc
        raise ProviderFailure("malformed_response", retryable=True)

    def fingerprint(self) -> dict[str, Any]:
        return {
            **super().fingerprint(),
            "model": self.model,
            "base_url": self.base_url,
            "reasoning_effort": self.reasoning_effort,
            "max_output_tokens": self.max_output_tokens,
            "task_max_output_tokens": self.task_max_output_tokens,
            "review_verdict_format": self.review_verdict_format,
        }

    def generate_review_verdict(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]:
        if self.review_verdict_format == "json":
            return self.generate_json(system=system, user=user, schema=schema, schema_name=schema_name)
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise ProviderFailure("authentication_error", retryable=False)
        payload: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": user,
            "store": False,
        }
        if self.reasoning_effort is not None:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        output_limit = self.task_max_output_tokens.get(schema_name, self.max_output_tokens)
        if output_limit is not None:
            payload["max_output_tokens"] = output_limit
        data = _post_json(
            f"{self.base_url}/responses",
            {"Authorization": f"Bearer {api_key}"},
            payload,
            self.timeout,
        )
        if data.get("status") in {"failed", "cancelled", "incomplete"}:
            raise ProviderFailure("provider_error", retryable=True)
        for item in data.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "refusal":
                    return _with_usage(_refusal_payload(schema_name), data)
                if content.get("type") == "output_text":
                    return _with_usage(_parse_plain_review_verdict(content.get("text", "")), data)
        raise ProviderFailure("malformed_response", retryable=True)


class CompatibleChatProvider(ModelProvider):
    """OpenAI-compatible /chat/completions provider."""

    def __init__(
        self,
        name: str,
        model: str,
        *,
        api_key_env: str,
        base_url: str,
        timeout: int = 120,
        response_format: str = "json_schema",
        temperature: float | None = None,
        thinking: str | None = None,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
        max_tokens_per_source_char: float | None = None,
        min_output_tokens: int = 96,
        max_output_tokens: int = 2048,
        task_max_output_tokens: dict[str, int] | None = None,
        max_input_chars: int | None = None,
        task_max_input_chars: dict[str, int] | None = None,
        review_verdict_format: str = "json",
    ):
        super().__init__(name)
        if response_format not in {"json_schema", "json_object"}:
            raise ConfigurationError(
                f"Compatible provider {name!r} response_format must be json_schema or json_object"
            )
        self.model = model
        self.api_key_env = api_key_env
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.response_format = response_format
        if temperature is not None and not 0 <= temperature <= 2:
            raise ConfigurationError(
                f"Compatible provider {name!r} temperature must be between 0 and 2"
            )
        self.temperature = temperature
        if thinking not in {None, "enabled", "disabled"}:
            raise ConfigurationError(
                f"Compatible provider {name!r} thinking must be enabled or disabled"
            )
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.max_tokens = max_tokens
        self.max_tokens_per_source_char = max_tokens_per_source_char
        self.min_output_tokens = min_output_tokens
        self.max_output_tokens = max_output_tokens
        self.task_max_output_tokens = {
            str(key): int(value) for key, value in (task_max_output_tokens or {}).items()
        }
        if max_input_chars is not None and max_input_chars < 1:
            raise ConfigurationError("max_input_chars must be greater than zero")
        self.max_input_chars = max_input_chars
        self.task_max_input_chars = {
            str(key): int(value) for key, value in (task_max_input_chars or {}).items()
        }
        if any(value < 1 for value in self.task_max_input_chars.values()):
            raise ConfigurationError("task_max_input_chars values must be greater than zero")
        if review_verdict_format not in {"json", "plain"}:
            raise ConfigurationError("review_verdict_format must be json or plain")
        self.review_verdict_format = review_verdict_format

    def _request_max_tokens(self, user: str, schema_name: str) -> int | None:
        if schema_name in self.task_max_output_tokens:
            return self.task_max_output_tokens[schema_name]
        if self.max_tokens is not None:
            return self.max_tokens
        if self.max_tokens_per_source_char is None:
            return None
        source_length = len(user)
        try:
            payload = json.loads(user)
            target = payload.get("translation_target", {})
            if isinstance(target, dict) and isinstance(target.get("source"), str):
                source_length = len(target["source"])
            elif isinstance(payload.get("candidate_translation"), str):
                source_length = len(payload["candidate_translation"])
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
        estimate = int(max(1, source_length) * self.max_tokens_per_source_char)
        return max(self.min_output_tokens, min(self.max_output_tokens, estimate))

    def _enforce_input_budget(self, system: str, user: str, schema_name: str) -> None:
        limit = self.task_max_input_chars.get(schema_name, self.max_input_chars)
        if limit is not None and len(system) + len(user) > limit:
            raise ProviderFailure("budget_exhausted", retryable=False)

    def generate_json(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]:
        api_key = os.environ.get(self.api_key_env, "")
        if self.api_key_env and not api_key:
            raise ProviderFailure("authentication_error", retryable=False)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        response_format: dict[str, Any]
        if self.response_format == "json_object":
            response_format = {"type": "json_object"}
            system = (
                f"{system}\n\nReturn a JSON object matching this JSON Schema exactly:\n"
                + json.dumps(schema, ensure_ascii=False, sort_keys=True)
            )
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        self._enforce_input_budget(system, user, schema_name)
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": response_format,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.thinking is not None:
            payload["thinking"] = {"type": self.thinking}
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = self.reasoning_effort
        request_max_tokens = self._request_max_tokens(user, schema_name)
        if request_max_tokens is not None:
            payload["max_tokens"] = request_max_tokens
        data = _post_json(f"{self.base_url}/chat/completions", headers, payload, self.timeout)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            return _with_usage(_refusal_payload(schema_name), data)
        if choice.get("finish_reason") == "length":
            raise ProviderFailure("malformed_response", retryable=True)
        if choice.get("finish_reason") in {"insufficient_system_resource", "aborted"}:
            raise ProviderFailure("provider_error", retryable=True)
        try:
            return _with_usage(json.loads(message.get("content") or ""), data)
        except json.JSONDecodeError as exc:
            raise ProviderFailure("malformed_response", retryable=True) from exc

    def fingerprint(self) -> dict[str, Any]:
        return {
            **super().fingerprint(),
            "model": self.model,
            "base_url": self.base_url,
            "response_format": self.response_format,
            "temperature": self.temperature,
            "thinking": self.thinking,
            "reasoning_effort": self.reasoning_effort,
            "max_tokens": self.max_tokens,
            "max_tokens_per_source_char": self.max_tokens_per_source_char,
            "min_output_tokens": self.min_output_tokens,
            "max_output_tokens": self.max_output_tokens,
            "task_max_output_tokens": self.task_max_output_tokens,
            "max_input_chars": self.max_input_chars,
            "task_max_input_chars": self.task_max_input_chars,
            "review_verdict_format": self.review_verdict_format,
        }

    def generate_review_verdict(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]:
        if self.review_verdict_format == "json":
            return self.generate_json(system=system, user=user, schema=schema, schema_name=schema_name)
        api_key = os.environ.get(self.api_key_env, "")
        if self.api_key_env and not api_key:
            raise ProviderFailure("authentication_error", retryable=False)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._enforce_input_budget(system, user, schema_name)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.thinking is not None:
            payload["thinking"] = {"type": self.thinking}
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = self.reasoning_effort
        request_max_tokens = self._request_max_tokens(user, schema_name)
        if request_max_tokens is not None:
            payload["max_tokens"] = request_max_tokens
        data = _post_json(f"{self.base_url}/chat/completions", headers, payload, self.timeout)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            return _with_usage(_refusal_payload(schema_name), data)
        if choice.get("finish_reason") == "length":
            raise ProviderFailure("malformed_response", retryable=True)
        if choice.get("finish_reason") in {"insufficient_system_resource", "aborted"}:
            raise ProviderFailure("provider_error", retryable=True)
        return _with_usage(_parse_plain_review_verdict(message.get("content") or ""), data)


class CommandProvider(ModelProvider):
    """Local/OpenCode/Codex harness adapter using a stable JSON-over-stdio protocol.

    The command receives one JSON request on stdin and must print one JSON object on stdout.
    No shell is used. This prevents game text from becoming executable command text.
    """

    def __init__(self, name: str, command: list[str], *, timeout: int = 180, cwd: str | None = None):
        super().__init__(name)
        if not command:
            raise ConfigurationError(f"Command provider {name!r} has an empty command")
        self.command = command
        self.timeout = timeout
        self.cwd = cwd

    def generate_json(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]:
        request = json.dumps(
            {"protocol": "game-l10n-provider/v1", "system": system, "input": user, "schema": schema, "schema_name": schema_name},
            ensure_ascii=False,
        )
        try:
            completed = subprocess.run(
                self.command,
                input=request,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=self.timeout,
                cwd=self.cwd,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderError(f"Command provider {self.name!r} could not run: {exc}") from exc
        if completed.returncode != 0:
            raise ProviderError(
                f"Command provider {self.name!r} exited {completed.returncode}: {completed.stderr[-2000:]}"
            )
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Command provider {self.name!r} returned invalid JSON") from exc

    def fingerprint(self) -> dict[str, Any]:
        return {**super().fingerprint(), "command": self.command}


class ScriptedProvider(ModelProvider):
    """In-memory provider for tests and embedding applications."""

    def __init__(self, name: str, handler: Callable[..., dict[str, Any]]):
        super().__init__(name)
        self.handler = handler
        self.call_count = 0

    def generate_json(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]:
        self.call_count += 1
        return self.handler(system=system, user=user, schema=schema, schema_name=schema_name)


class ProviderRegistry:
    def __init__(self, providers: dict[str, ModelProvider] | None = None):
        self._providers = providers or {}

    def add(self, provider: ModelProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> ModelProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ConfigurationError(f"Unknown provider: {name}") from exc

    def fingerprints(self, names: list[str]) -> list[dict[str, Any]]:
        return [self.get(name).fingerprint() for name in names]

    @classmethod
    def from_config(cls, config: dict[str, Any], project_root: Path) -> "ProviderRegistry":
        validate_provider_policy(config)
        registry = cls()
        for item in config.get("providers", []):
            kind = item.get("type")
            common = {"name": item["name"]}
            if kind == "openai_responses":
                provider = OpenAIResponsesProvider(
                    **common,
                    model=item["model"],
                    api_key_env=item.get("api_key_env", "OPENAI_API_KEY"),
                    base_url=item.get("base_url", "https://api.openai.com/v1"),
                    timeout=int(item.get("timeout", 120)),
                    reasoning_effort=item.get("reasoning_effort"),
                    max_output_tokens=(
                        int(item["max_output_tokens"])
                        if item.get("max_output_tokens") is not None
                        else None
                    ),
                    task_max_output_tokens=item.get("task_max_output_tokens"),
                    review_verdict_format=item.get("review_verdict_format", "json"),
                )
            elif kind == "compatible_chat":
                task_max_input_chars = item.get("task_max_input_chars")
                if (
                    task_max_input_chars is None
                    and item.get("name") == "deepseek"
                    and config.get("provider_preset")
                    in {"openai-deepseek", "codex-app-deepseek"}
                ):
                    # Apply the current preset safety budget to old private
                    # workspaces without rewriting their stored configuration.
                    task_max_input_chars = {
                        schema_name: _DEFAULT_FOCUSED_TEXT_INPUT_CHARS
                        for schema_name in _FOCUSED_TEXT_SCHEMA_NAMES
                    }
                provider = CompatibleChatProvider(
                    **common,
                    model=item["model"],
                    api_key_env=item.get("api_key_env", "COMPATIBLE_API_KEY"),
                    base_url=item["base_url"],
                    timeout=int(item.get("timeout", 120)),
                    response_format=item.get("response_format", "json_schema"),
                    temperature=(
                        float(item["temperature"]) if item.get("temperature") is not None else None
                    ),
                    thinking=item.get("thinking"),
                    reasoning_effort=item.get("reasoning_effort"),
                    max_tokens=(int(item["max_tokens"]) if item.get("max_tokens") is not None else None),
                    max_tokens_per_source_char=(
                        float(item["max_tokens_per_source_char"])
                        if item.get("max_tokens_per_source_char") is not None
                        else None
                    ),
                    min_output_tokens=int(item.get("min_output_tokens", 96)),
                    max_output_tokens=int(item.get("max_output_tokens", 2048)),
                    task_max_output_tokens=item.get("task_max_output_tokens"),
                    max_input_chars=(
                        int(item["max_input_chars"])
                        if item.get("max_input_chars") is not None
                        else None
                    ),
                    task_max_input_chars=task_max_input_chars,
                    review_verdict_format=item.get("review_verdict_format", "json"),
                )
            elif kind == "command":
                cwd = item.get("cwd")
                if cwd and not Path(cwd).is_absolute():
                    cwd = str(project_root / cwd)
                provider = CommandProvider(
                    **common,
                    command=[str(value) for value in item["command"]],
                    timeout=int(item.get("timeout", 180)),
                    cwd=cwd,
                )
            else:
                raise ConfigurationError(f"Unsupported provider type: {kind!r}")
            registry.add(provider)
        return registry
