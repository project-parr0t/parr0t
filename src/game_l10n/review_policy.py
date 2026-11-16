"""Metadata-only review-call limits; missing user policy is not exhaustion."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .model_policy import (
    NATIVE_ROLE_ROUTES, apply_native_role_models, exact_native_model,
    native_role_bindings, native_role_models,
)

SOL_REVIEW_CALLS = 2
JUDGE_STAGES = frozenset({"review", "review-confirm", "review-grace-retry", "review-refusal-retry"})
REVIEW_SETTING_FIELDS = ("triage_max_calls", "strong_max_calls", "review_max_workers", "triage_max_workers", "strong_max_workers")
TRIAGE_STAGES = frozenset({"review-triage", "review-triage-confirm", "review-triage-grace-retry", "review-triage-refusal-retry"})


class ReviewPolicyRequired(ValueError):
    def __init__(self, provider: str) -> None:
        super().__init__("E_REVIEW_FALLBACK_POLICY_REQUIRED")
        self.provider = provider


def validate_review_limits(limits: Any) -> None:
    if not isinstance(limits, dict) or any(
        not isinstance(route, str) or not route or not isinstance(count, int)
        or isinstance(count, bool) or count < 1
        for route, count in limits.items()
    ):
        raise ValueError("E_REVIEW_CALL_POLICY_INVALID")


def is_sol_route(provider: str, model: str = "") -> bool:
    identity = model.split("@", 1)[0].split(":", 1)[0].rsplit("/", 1)[-1]
    return identity == "gpt-6.1-sol" if model else provider in {"native-sol", "openai-sol"}


def review_call_limit(
    provider: str, reviewers: list[str], limits: dict[str, int], *, model: str = "",
    tier_limit: int | None = None,
) -> int | None:
    validate_review_limits(limits)
    if is_sol_route(provider, model):
        count = limits.get(provider, SOL_REVIEW_CALLS)
        if count > SOL_REVIEW_CALLS:
            raise ValueError("E_SOL_REVIEW_CALL_LIMIT")
    elif provider in limits:
        count = limits[provider]
    elif reviewers and provider == reviewers[0]:
        count = None
    else:
        raise ReviewPolicyRequired(provider)
    if tier_limit is None:
        return count
    return min(count, tier_limit) if count is not None else tier_limit


def judge_calls(attempts: list[Any], provider: str, *, model: str = "", stage_prefix: str = "review") -> int:
    stages = TRIAGE_STAGES if stage_prefix == "review-triage" else JUDGE_STAGES
    sol = is_sol_route(provider, model)
    matching = [attempt for attempt in attempts if attempt.stage in stages
                and (attempt.provider == provider or (bool(model) and attempt.model == model)
                     or (sol and is_sol_route(attempt.provider, attempt.model)))]
    return max(len(matching), max((attempt.ordinal for attempt in matching), default=0))


def validate_review_settings(settings: dict[str, Any]) -> None:
    for key in REVIEW_SETTING_FIELDS:
        value = settings.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                  or value < 1 or (key.endswith("workers") and value > 64)):
            raise ValueError("E_REVIEW_TIER_POLICY_INVALID")


def review_worker_limit(settings: dict[str, Any], requested: int, *, tier: str) -> int:
    validate_review_settings(settings)
    values = [requested, settings.get("review_max_workers"), settings.get(f"{tier}_max_workers")]
    return min(value for value in values if value is not None)


def review_policy_status(config: dict[str, Any], work_dir: Path | None = None) -> dict[str, Any]:
    routing = config.get("routing", {})
    limits = routing.get("review_attempt_limits", {})
    validate_review_limits(limits)
    validate_review_settings(routing)
    missing = [name for name in routing.get("reviewers", [])[1:] if name not in limits]
    requested = native_role_bindings(config)
    pending = []
    if work_dir is not None and requested:
        from .native_runtime import native_runtime
        try:
            runtime = native_runtime(work_dir)
        except ValueError:
            runtime = None
        bound = runtime["models"] if runtime else {}
        pending = [route for route, model in requested.items() if bound.get(route) != model]
    return {
        "status": ("native_binding_required" if pending else
                   "user_configuration_required" if missing else "configured"),
        "sol_max_calls": SOL_REVIEW_CALLS,
        "review_attempt_limits": dict(limits),
        "review_limits": {key: routing[key] for key in REVIEW_SETTING_FIELDS if routing.get(key) is not None},
        "missing_fallback_providers": missing,
        "requested_native_models": native_role_models(config),
        "native_binding_pending": pending,
    }


def configure_review_policy(
    work_dir: Path, *, user_requested: bool, model: str | None = None,
    max_calls: int | None = None, provider: str | None = None,
    settings: dict[str, int] | None = None,
) -> dict[str, Any]:
    from .native_runtime import configure_native_runtime, native_runtime
    from .workspace import _write_config, load_config

    if not user_requested:
        raise ValueError("E_REVIEW_POLICY_USER_REQUEST_REQUIRED")
    settings = settings or {}
    if set(settings) - set(REVIEW_SETTING_FIELDS):
        raise ValueError("E_REVIEW_TIER_POLICY_INVALID")
    validate_review_settings(settings)
    if (model is None) != (max_calls is None):
        raise ReviewPolicyRequired(provider or "")
    config = load_config(work_dir)
    routing = config["routing"]
    try:
        runtime = native_runtime(work_dir)
    except ValueError:
        runtime = None
    bind_requested = False
    selected = {}
    if model is not None:
        validate_review_limits({"requested": max_calls})
        if not isinstance(model, str) or not model.strip():
            raise ValueError("E_REVIEW_MODEL_BINDING_INVALID")
        fallback_routes = routing.get("reviewers", [])[1:]
        models = {item["name"]: item.get("model", "") for item in config.get("providers", [])}
        if runtime:
            models.update(runtime["models"])
        matched = [route for route in fallback_routes
                   if models.get(route) == model and (provider is None or route == provider)]
        if len(matched) == 1:
            route = matched[0]
        elif (
            provider in {None, NATIVE_ROLE_ROUTES["review-fallback"]}
            and exact_native_model(model)
            and (config.get("provider_preset") == "codex-app-deepseek"
                 or (config.get("provider_preset") == "custom" and runtime is not None))
        ):
            config = apply_native_role_models(config, {"review-fallback": model})
            routing = config["routing"]
            route = NATIVE_ROLE_ROUTES["review-fallback"]
            bind_requested = True
        else:
            raise ValueError("E_REVIEW_MODEL_BINDING_INVALID")
        if is_sol_route(route, model) and max_calls > SOL_REVIEW_CALLS:
            raise ValueError("E_SOL_REVIEW_CALL_LIMIT")
        routing.setdefault("review_attempt_limits", {})[route] = max_calls
        selected = {"provider": route, "model": model}
    routing.update(settings)
    from .provider_policy import validate_provider_policy
    validate_provider_policy(config)
    _write_config(work_dir, config)
    binding_issue = ""
    if bind_requested and runtime is not None:
        try:
            configure_native_runtime(work_dir, runtime["host"], {})
        except ValueError as error:
            # Preserve the exact request and limit as pending; no model ran.
            binding_issue = str(error).split(":", 1)[0]
    result = {**review_policy_status(config, work_dir), **selected}
    if binding_issue:
        result["binding_issue_code"] = binding_issue
    return result
