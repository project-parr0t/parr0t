"""User-selected native role models override preset defaults, not transport policy."""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .errors import ConfigurationError

NATIVE_ROLE_ROUTES = {
    "translation": "native-translation",
    "triage": "native-triage",
    "strong-review": "native-strong-review",
    "context": "native-context",
    "review-fallback": "native-review-fallback",
}
_ROLE_SLOTS = {
    "translation": ("translators", 0),
    "triage": ("review_triagers", 0),
    "strong-review": ("reviewers", 0),
    "context": ("context_analyzers", 0),
    "review-fallback": ("reviewers", 1),
}


def exact_native_model(value: str) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]*", value) is not None
        and value not in {"default", "inherit", "task", "smol", "slow", "luna", "sol"}
        and not value.startswith("@")
    )


def native_role_models(config: dict[str, Any]) -> dict[str, str]:
    policy = config.get("model_policy")
    if policy is None:
        return {}
    if (
        not isinstance(policy, dict)
        or set(policy) != {"source", "native_roles"}
        or policy["source"] != "user_prompt"
        or not isinstance(policy["native_roles"], dict)
        or not policy["native_roles"]
        or set(policy["native_roles"]) - NATIVE_ROLE_ROUTES.keys()
        or not all(exact_native_model(model) for model in policy["native_roles"].values())
    ):
        raise ConfigurationError("E_MODEL_POLICY_INVALID: exact user-selected native role models required")
    return dict(policy["native_roles"])


def native_role_bindings(config: dict[str, Any]) -> dict[str, str]:
    return {NATIVE_ROLE_ROUTES[role]: model for role, model in native_role_models(config).items()}


def codex_app_routes(config: dict[str, Any]) -> dict[str, list[str]]:
    routes = {
        "context_analyzers": ["native-sol", "native-luna"],
        "translators": ["native-luna", "native-sol", "deepseek"],
        "review_triagers": ["native-luna"],
        "reviewers": ["native-sol", "deepseek"],
        "rewriters": ["deepseek"],
    }
    for role in native_role_models(config):
        key, index = _ROLE_SLOTS[role]
        routes[key][index] = NATIVE_ROLE_ROUTES[role]
    return routes


def codex_app_translation_retries(config: dict[str, Any]) -> dict[str, int]:
    primary = codex_app_routes(config)["translators"][0]
    return {primary: 5, "native-sol": 3}


def apply_native_role_models(config: dict[str, Any], models: dict[str, str]) -> dict[str, Any]:
    """Replace named role slots only; preserve unspecified roles and provider settings."""
    if config.get("provider_preset") not in {"codex-app-deepseek", "custom"}:
        raise ConfigurationError("E_MODEL_POLICY_TRANSPORT_UNSUPPORTED: native role models require a native workflow")
    if not models or set(models) - NATIVE_ROLE_ROUTES.keys() or not all(
        exact_native_model(model) for model in models.values()
    ):
        raise ConfigurationError("E_MODEL_POLICY_INVALID: exact user-selected native role models required")
    selected = native_role_models(config)
    selected.update(models)
    updated = deepcopy(config)
    routing = updated["routing"]
    for role in models:
        key, index = _ROLE_SLOTS[role]
        routes = routing.get(key)
        if not isinstance(routes, list) or len(routes) <= index:
            raise ConfigurationError("E_MODEL_POLICY_ROLE_UNAVAILABLE: role has no configured slot")
        previous = routes[index]
        route = NATIVE_ROLE_ROUTES[role]
        if role == "translation" and previous != route:
            retries = routing.setdefault("translator_retries", {})
            if previous in retries:
                retries[route] = retries.pop(previous)
        routes[index] = route
    updated["model_policy"] = {"source": "user_prompt", "native_roles": selected}
    return updated
