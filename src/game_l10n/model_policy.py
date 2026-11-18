"""User-selected native role models override preset defaults, not transport policy."""
from __future__ import annotations

from copy import deepcopy
import unicodedata
from typing import Any

from .errors import ConfigurationError

NATIVE_ROLE_ROUTES = {
    "translation": "native-translation",
    "triage": "native-triage",
    "strong-review": "native-strong-review",
    "context": "native-context",
    "review-fallback": "native-review-fallback",
    "rewrite": "native-rewrite",
}
_ROLE_SLOTS = {
    "translation": ("translators", 0),
    "triage": ("review_triagers", 0),
    "strong-review": ("reviewers", 0),
    "context": ("context_analyzers", 0),
    "review-fallback": ("reviewers", 1),
    "rewrite": ("rewriters", 0),
}


def exact_native_model(value: str) -> bool:
    """Validate an opaque selector, not a Core catalog or provider-name grammar."""
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and not any(unicodedata.category(char).startswith("C") for char in value)
        and not any(char in value for char in "\\*?[]")
        and not value.startswith(("/", ".", "~", "@"))
        and "://" not in value
        and not (len(value) > 2 and value[0].isalpha() and value[1:3] == ":/")
        and not any(part in {".", ".."} for part in value.split("/"))
        and value.casefold() not in {"default", "inherit", "task", "smol", "slow", "luna", "sol"}
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


def model_selection_authorization(config: dict[str, Any], route: str, model: str) -> dict[str, Any]:
    """Authorize an exact bound selection, independently of engine or provider catalogs."""
    if not exact_native_model(model):
        raise ConfigurationError("E_MODEL_POLICY_INVALID: an exact model selection is required")
    requested = native_role_bindings(config).get(route)
    if requested is not None and requested != model:
        raise ConfigurationError("E_MODEL_POLICY_BINDING_REQUIRED: requested role model is not bound")
    return {
        "schema": "parr0t.model-selection-authorization.v1",
        "status": "approved",
        "source": "user_prompt" if requested is not None else "runtime_binding",
        "scope": "all_sdks",
        "model": model,
        "additional_allowlist_required": False,
        "execution_availability": "separate_host_result",
    }



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
        if routes is None:
            routes = routing[key] = []
        if not isinstance(routes, list) or len(routes) < index:
            raise ConfigurationError("E_MODEL_POLICY_ROLE_UNAVAILABLE: role has no configured primary")
        previous = routes[index] if len(routes) > index else None
        route = NATIVE_ROLE_ROUTES[role]
        if len(routes) == index:
            routes.append(route)
        else:
            routes[index] = route
        if role == "translation" and previous != route:
            retries = routing.setdefault("translator_retries", {})
            if previous in retries:
                retries.setdefault(route, retries[previous])
                if previous not in routes:
                    del retries[previous]
        if role in {"triage", "strong-review", "review-fallback"} and previous != route:
            limits = routing.get("review_attempt_limits", {})
            if previous in limits:
                limits.setdefault(route, limits[previous])
                if previous not in routing.get("reviewers", []) + routing.get("review_triagers", []):
                    del limits[previous]
    updated["model_policy"] = {"source": "user_prompt", "native_roles": selected}
    return updated
