from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import socket
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

from .errors import ConfigurationError, ProviderFailure
from .prompts import REVIEW_ISSUE_CODES
from .provider_policy import validate_provider_policy

_DEFAULT_FOCUSED_TEXT_INPUT_CHARS = 24_000
_FOCUSED_TEXT_SCHEMA_NAMES = (
    "game_translation",
    "game_translation_review_judge",
    "game_translation_review_rewrite",
)


def _parse_plain_review_verdict(value: str) -> dict[str, Any]:
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        raise ProviderFailure("malformed_response", retryable=True)
    line = value.strip()
    if not line:
        raise ProviderFailure("malformed_response", retryable=True)
    head, _, tail = line.partition(" ")
    verdicts = {"OK": "ok", "REWRITE": "rewrite", "REFUSED": "refused", "ERROR": "error"}
    verdict = verdicts.get(head.upper())
    if verdict is None:
        raise ProviderFailure("malformed_response", retryable=True)
    issues = [item.strip().casefold() for item in tail.split(",")] if tail else []
    if (verdict != "rewrite" and issues) or (verdict == "rewrite" and not issues):
        raise ProviderFailure("malformed_response", retryable=True)
    if len(issues) > 4 or any(item not in REVIEW_ISSUE_CODES for item in issues):
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
            if type(value) is int and value >= 0:
                normalized[target] = value
                break
    details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    if isinstance(details, dict):
        reasoning = details.get("reasoning_tokens")
        if type(reasoning) is int and reasoning >= 0:
            normalized["reasoning_tokens"] = reasoning
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached = usage.get("prompt_cache_hit_tokens")
    if (type(cached) is not int or cached < 0) and isinstance(input_details, dict):
        cached = input_details.get("cached_tokens")
    if type(cached) is int and cached >= 0:
        normalized["cached_input_tokens"] = cached
    cache_miss = usage.get("prompt_cache_miss_tokens")
    if type(cache_miss) is int and cache_miss >= 0:
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
    # Model-generated fields are not authoritative usage telemetry.
    result.pop("_usage", None)
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


def _retry_after_seconds(headers: Any) -> float | None:
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(value))
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def _provider_timeout(value: float | None) -> float | None:
    """No implicit generation deadline; a configured positive value is explicit."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConfigurationError("timeout must be null, zero, or positive seconds") from exc
    if isinstance(value, bool) or not math.isfinite(seconds) or seconds < 0:
        raise ConfigurationError("timeout must be null, zero, or positive seconds")
    return seconds or None


def _positive_int(value: Any, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConfigurationError(f"{field} must be a positive integer") from exc
    if isinstance(value, bool) or number < 1 or (not isinstance(value, str) and value != number):
        raise ConfigurationError(f"{field} must be a positive integer")
    return number


def _positive_limits(value: dict[str, int] | None, field: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigurationError(f"{field} must be an object of positive integers")
    return {str(key): _positive_int(limit, field) for key, limit in value.items()}


def _reject_json_constant(_value: str) -> None:
    raise ValueError("Nonstandard JSON constant")


def _parse_json_object(value: Any, response: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        if not isinstance(value, str):
            raise ValueError("Expected JSON text")
        result = json.loads(value, parse_constant=_reject_json_constant)
        if not isinstance(result, dict):
            raise ValueError("Expected a JSON object")
        return result
    except ValueError as exc:
        raise ProviderFailure(
            "malformed_response", retryable=True, usage=_normalized_usage(response or {})
        ) from exc


def _responses_text(data: dict[str, Any]) -> str | None:
    usage = _normalized_usage(data)
    if data.get("status") in ("failed", "cancelled", "incomplete"):
        details = data.get("error") or data.get("incomplete_details") or {}
        if not isinstance(details, dict):
            raise ProviderFailure("malformed_response", retryable=True, usage=usage)
        reason = details.get("code") or details.get("reason")
        if data.get("status") == "incomplete" and reason == "content_filter":
            return None
        codes = {
            "max_output_tokens": ("output_truncated", False),
            "insufficient_quota": ("quota_exhausted", False),
            "quota_exceeded": ("quota_exhausted", False),
            "invalid_api_key": ("authentication_error", False),
            "rate_limit_exceeded": ("rate_limited", True),
            "server_timeout": ("timeout", True),
        }
        code, retryable = codes.get(reason, ("provider_error", True)) if isinstance(reason, str) else ("provider_error", True)
        raise ProviderFailure(code, retryable=retryable, usage=usage)
    output = data.get("output")
    if not isinstance(output, list):
        raise ProviderFailure("malformed_response", retryable=True, usage=usage)
    parts: list[str] = []
    refused = False
    for item in output:
        if not isinstance(item, dict):
            raise ProviderFailure("malformed_response", retryable=True, usage=usage)
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            raise ProviderFailure("malformed_response", retryable=True, usage=usage)
        for part in content:
            if not isinstance(part, dict):
                raise ProviderFailure("malformed_response", retryable=True, usage=usage)
            if part.get("type") == "refusal":
                refused = True
            elif part.get("type") == "output_text":
                text = part.get("text")
                if not isinstance(text, str):
                    raise ProviderFailure("malformed_response", retryable=True, usage=usage)
                parts.append(text)
    if refused:
        return None
    if not parts:
        raise ProviderFailure("malformed_response", retryable=True, usage=usage)
    return "".join(parts)


def _chat_choice(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderFailure("malformed_response", retryable=True, usage=_normalized_usage(data))
    choice = choices[0]
    message = choice.get("message")
    if message is None and choice.get("finish_reason") in ("content_filter", "length", "insufficient_system_resource", "aborted"):
        message = {}
    if not isinstance(message, dict):
        raise ProviderFailure("malformed_response", retryable=True, usage=_normalized_usage(data))
    return choice, message


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_provider_timeout(timeout)) as response:
            return _parse_json_object(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace").casefold()
        except (OSError, http.client.HTTPException):
            detail = ""
        if exc.code in {401, 403}:
            code, retryable = "authentication_error", False
        elif exc.code == 402 or (exc.code == 429 and any(token in detail for token in ("quota", "balance", "credit"))):
            code, retryable = "quota_exhausted", False
        elif exc.code == 429:
            code, retryable = "rate_limited", True
        elif exc.code in {408, 504}:
            code, retryable = "timeout", True
        elif 500 <= exc.code < 600:
            code, retryable = "provider_error", True
        else:
            code, retryable = "provider_error", False
        raise ProviderFailure(
            code,
            retryable=retryable,
            status_code=exc.code,
            retry_after_seconds=(
                _retry_after_seconds(exc.headers)
                if retryable and (exc.code == 429 or 500 <= exc.code < 600)
                else None
            ),
        ) from exc
    except UnicodeDecodeError as exc:
        raise ProviderFailure("malformed_response", retryable=True) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ProviderFailure("timeout", retryable=True) from exc
    except urllib.error.URLError as exc:
        code = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "provider_error"
        raise ProviderFailure(code, retryable=True) from exc
    except (OSError, http.client.HTTPException) as exc:
        raise ProviderFailure("provider_error", retryable=True) from exc


class OpenAIResponsesProvider(ModelProvider):
    """OpenAI Responses provider using strict JSON, with optional compact plain judgments."""

    def __init__(
        self,
        name: str,
        model: str,
        *,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str = "https://api.openai.com/v1",
        timeout: float | None = None,
        reasoning_effort: str | None = None,
        max_output_tokens: int | None = None,
        task_max_output_tokens: dict[str, int] | None = None,
        review_verdict_format: str = "json",
    ):
        super().__init__(name)
        self.model = model
        self.api_key_env = api_key_env
        self.base_url = base_url.rstrip("/")
        self.timeout = _provider_timeout(timeout)
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = (
            _positive_int(max_output_tokens, "max_output_tokens")
            if max_output_tokens is not None else None
        )
        self.task_max_output_tokens = _positive_limits(task_max_output_tokens, "task_max_output_tokens")
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
        text = _responses_text(data)
        if text is None:
            return _with_usage(_refusal_payload(schema_name), data)
        return _with_usage(_parse_json_object(text, data), data)

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
        text = _responses_text(data)
        if text is None:
            return _with_usage(_refusal_payload(schema_name), data)
        try:
            verdict = _parse_plain_review_verdict(text)
        except ProviderFailure as exc:
            raise ProviderFailure(exc.code, retryable=exc.retryable, usage=_normalized_usage(data)) from exc
        return _with_usage(verdict, data)


class CompatibleChatProvider(ModelProvider):
    """OpenAI-compatible /chat/completions provider."""

    _MAX_OUTPUT_RETRY_STATES = 4096

    def __init__(
        self,
        name: str,
        model: str,
        *,
        api_key_env: str,
        base_url: str,
        timeout: float | None = None,
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
        self.timeout = _provider_timeout(timeout)
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
        self.max_tokens = _positive_int(max_tokens, "max_tokens") if max_tokens is not None else None
        if max_tokens_per_source_char is not None:
            try:
                ratio = float(max_tokens_per_source_char)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ConfigurationError("max_tokens_per_source_char must be finite and positive") from exc
            if isinstance(max_tokens_per_source_char, bool) or not math.isfinite(ratio) or ratio <= 0:
                raise ConfigurationError("max_tokens_per_source_char must be finite and positive")
            max_tokens_per_source_char = ratio
        self.max_tokens_per_source_char = max_tokens_per_source_char
        self.min_output_tokens = _positive_int(min_output_tokens, "min_output_tokens")
        self.max_output_tokens = _positive_int(max_output_tokens, "max_output_tokens")
        if self.min_output_tokens > self.max_output_tokens:
            raise ConfigurationError("min_output_tokens must not exceed max_output_tokens")
        self.task_max_output_tokens = _positive_limits(task_max_output_tokens, "task_max_output_tokens")
        self.max_input_chars = _positive_int(max_input_chars, "max_input_chars") if max_input_chars is not None else None
        self.task_max_input_chars = _positive_limits(task_max_input_chars, "task_max_input_chars")
        if review_verdict_format not in {"json", "plain"}:
            raise ConfigurationError("review_verdict_format must be json or plain")
        self.review_verdict_format = review_verdict_format
        self._output_retry_lock = threading.Lock()
        self._output_retry_limits: dict[str, int] = {}

    @staticmethod
    def _output_retry_key(user: str, schema_name: str) -> str:
        return hashlib.sha256(
            f"{schema_name}\0{user}".encode("utf-8")
        ).hexdigest()

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
        estimate = min(self.max_output_tokens, max(1, source_length) * self.max_tokens_per_source_char)
        base = max(self.min_output_tokens, int(estimate))
        with self._output_retry_lock:
            return max(
                base,
                self._output_retry_limits.get(
                    self._output_retry_key(user, schema_name), base
                ),
            )

    def _prepare_output_retry(
        self, user: str, schema_name: str, current_limit: int | None
    ) -> bool:
        if (
            current_limit is None
            or schema_name in self.task_max_output_tokens
            or self.max_tokens is not None
            or self.max_tokens_per_source_char is None
        ):
            return False
        next_limit = min(
            self.max_output_tokens,
            max(current_limit + self.min_output_tokens, current_limit * 2),
        )
        if next_limit <= current_limit:
            self._clear_output_retry(user, schema_name)
            return False
        with self._output_retry_lock:
            key = self._output_retry_key(user, schema_name)
            if (
                key not in self._output_retry_limits
                and len(self._output_retry_limits) >= self._MAX_OUTPUT_RETRY_STATES
            ):
                self._output_retry_limits.pop(next(iter(self._output_retry_limits)))
            self._output_retry_limits[key] = next_limit
        return True

    def _clear_output_retry(self, user: str, schema_name: str) -> None:
        with self._output_retry_lock:
            self._output_retry_limits.pop(
                self._output_retry_key(user, schema_name), None
            )

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
        choice, message = _chat_choice(data)
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            self._clear_output_retry(user, schema_name)
            return _with_usage(_refusal_payload(schema_name), data)
        if choice.get("finish_reason") == "length":
            retryable = self._prepare_output_retry(
                user, schema_name, request_max_tokens
            )
            raise ProviderFailure(
                "output_truncated",
                retryable=retryable,
                usage=_normalized_usage(data),
            )
        if choice.get("finish_reason") in ("insufficient_system_resource", "aborted"):
            raise ProviderFailure(
                "provider_error",
                retryable=True,
                usage=_normalized_usage(data),
            )
        result = _with_usage(_parse_json_object(message.get("content"), data), data)
        self._clear_output_retry(user, schema_name)
        return result

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
        choice, message = _chat_choice(data)
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            self._clear_output_retry(user, schema_name)
            return _with_usage(_refusal_payload(schema_name), data)
        if choice.get("finish_reason") == "length":
            retryable = self._prepare_output_retry(
                user, schema_name, request_max_tokens
            )
            raise ProviderFailure(
                "output_truncated",
                retryable=retryable,
                usage=_normalized_usage(data),
            )
        if choice.get("finish_reason") in ("insufficient_system_resource", "aborted"):
            raise ProviderFailure(
                "provider_error",
                retryable=True,
                usage=_normalized_usage(data),
            )
        try:
            verdict = _parse_plain_review_verdict(message.get("content") or "")
        except ProviderFailure as exc:
            raise ProviderFailure(
                exc.code,
                retryable=exc.retryable,
                status_code=exc.status_code,
                usage=_normalized_usage(data),
            ) from exc
        result = _with_usage(verdict, data)
        self._clear_output_retry(user, schema_name)
        return result


class CommandProvider(ModelProvider):
    """Local/OpenCode/Codex harness adapter using a stable JSON-over-stdio protocol.

    The command receives one JSON request on stdin and must print one JSON object on stdout.
    No shell is used. This prevents game text from becoming executable command text.
    """

    def __init__(self, name: str, command: list[str], *, timeout: float | None = None, cwd: str | None = None):
        super().__init__(name)
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(value, str) or "\0" in value for value in command)
            or not command[0].strip()
        ):
            raise ConfigurationError("command must be a nonempty argument array of strings")
        self.command = list(command)
        self.timeout = _provider_timeout(timeout)
        if cwd is not None:
            try:
                cwd = os.fspath(cwd)
            except TypeError as exc:
                raise ConfigurationError("cwd must be a nonempty path string") from exc
            if not isinstance(cwd, str) or not cwd or "\0" in cwd:
                raise ConfigurationError("cwd must be a nonempty path string")
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
        except subprocess.TimeoutExpired as exc:
            raise ProviderFailure("timeout", retryable=True) from exc
        except UnicodeDecodeError as exc:
            raise ProviderFailure("malformed_response", retryable=True) from exc
        except OSError as exc:
            raise ProviderFailure("provider_error", retryable=True) from exc
        if completed.returncode != 0:
            raise ProviderFailure("provider_error", retryable=True)
        return _parse_json_object(completed.stdout)

    def fingerprint(self) -> dict[str, Any]:
        return {**super().fingerprint(), "command": list(self.command), "cwd": self.cwd}


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
                    timeout=item.get("timeout"),
                    reasoning_effort=item.get("reasoning_effort"),
                    max_output_tokens=item.get("max_output_tokens"),
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
                    timeout=item.get("timeout"),
                    response_format=item.get("response_format", "json_schema"),
                    temperature=(
                        float(item["temperature"]) if item.get("temperature") is not None else None
                    ),
                    thinking=item.get("thinking"),
                    reasoning_effort=item.get("reasoning_effort"),
                    max_tokens=item.get("max_tokens"),
                    max_tokens_per_source_char=item.get("max_tokens_per_source_char"),
                    min_output_tokens=item.get("min_output_tokens", 96),
                    max_output_tokens=item.get("max_output_tokens", 2048),
                    task_max_output_tokens=item.get("task_max_output_tokens"),
                    max_input_chars=item.get("max_input_chars"),
                    task_max_input_chars=task_max_input_chars,
                    review_verdict_format=item.get("review_verdict_format", "json"),
                )
            elif kind == "command":
                provider = CommandProvider(
                    **common,
                    command=item["command"],
                    timeout=item.get("timeout"),
                    cwd=item.get("cwd"),
                )
                if provider.cwd is not None and not Path(provider.cwd).is_absolute():
                    provider.cwd = str(project_root / provider.cwd)
            else:
                raise ConfigurationError(f"Unsupported provider type: {kind!r}")
            registry.add(provider)
        return registry
