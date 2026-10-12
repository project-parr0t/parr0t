from __future__ import annotations

import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

from .errors import ConfigurationError, ProviderError


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
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise ProviderError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ProviderError(f"Request to {url} failed: {exc}") from exc


class OpenAIResponsesProvider(ModelProvider):
    """OpenAI Responses API provider using strict Structured Outputs."""

    def __init__(
        self,
        name: str,
        model: str,
        *,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str = "https://api.openai.com/v1",
        timeout: int = 120,
    ):
        super().__init__(name)
        self.model = model
        self.api_key_env = api_key_env
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def generate_json(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise ProviderError(f"Required environment variable is not set: {self.api_key_env}")
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
        data = _post_json(
            f"{self.base_url}/responses",
            {"Authorization": f"Bearer {api_key}"},
            payload,
            self.timeout,
        )
        if data.get("status") in {"failed", "cancelled", "incomplete"}:
            raise ProviderError(f"Response status was {data.get('status')}: {data.get('error') or data.get('incomplete_details')}")
        for item in data.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "refusal":
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
                        }
                    return {"status": "refused", "text": "", "reason": "provider_refusal"}
                if content.get("type") == "output_text":
                    try:
                        return json.loads(content.get("text", ""))
                    except json.JSONDecodeError as exc:
                        raise ProviderError("OpenAI response was not valid JSON") from exc
        raise ProviderError("OpenAI response contained no output_text")

    def fingerprint(self) -> dict[str, Any]:
        return {**super().fingerprint(), "model": self.model, "base_url": self.base_url}


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

    def generate_json(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]:
        api_key = os.environ.get(self.api_key_env, "")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        response_format: dict[str, Any]
        if self.response_format == "json_object":
            response_format = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": response_format,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        data = _post_json(f"{self.base_url}/chat/completions", headers, payload, self.timeout)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        if message.get("refusal"):
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
                }
            return {"status": "refused", "text": "", "reason": "provider_refusal"}
        if choice.get("finish_reason") in {"content_filter", "length"}:
            raise ProviderError(f"Incomplete compatible response: {choice.get('finish_reason')}")
        try:
            return json.loads(message.get("content") or "")
        except json.JSONDecodeError as exc:
            raise ProviderError("Compatible provider response was not valid JSON") from exc

    def fingerprint(self) -> dict[str, Any]:
        return {
            **super().fingerprint(),
            "model": self.model,
            "base_url": self.base_url,
            "response_format": self.response_format,
            "temperature": self.temperature,
        }


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
                )
            elif kind == "compatible_chat":
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
