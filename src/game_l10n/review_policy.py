"""Metadata-only explicit review limits with a documented fallback preset default."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model_policy import (
    NATIVE_ROLE_ROUTES, apply_native_role_models, exact_native_model,
    model_selection_authorization, native_role_bindings, native_role_models,
)

DEFAULT_REVIEW_FALLBACK_CALLS = 3
JUDGE_STAGES = frozenset({"review", "review-confirm", "review-grace-retry", "review-refusal-retry"})
REVIEW_SETTING_FIELDS = (
    "triage_max_calls", "strong_max_calls", "review_max_rounds",
    "review_max_workers", "triage_max_workers", "strong_max_workers",
)
TRIAGE_STAGES = frozenset({"review-triage", "review-triage-confirm", "review-triage-grace-retry", "review-triage-refusal-retry"})

_ADDITIONAL_FALLBACK_ROOT = Path(".game-l10n/native/additional-fallback-review")
_ADDITIONAL_FALLBACK_SCHEMA = "parr0t.additional-fallback-review.v1"


def _record_additional_fallback_review(
    work_dir: Path, segments: list[Any], provider: str, model: str,
) -> Path:
    """Bind a one-call consent to one immutable review namespace."""
    from dataclasses import asdict
    from .control_plane import require_control_plane_ready
    from .native_followup import _atomic_json
    from .store import stable_hash, stable_hash_sequence

    require_control_plane_ready(work_dir)
    ids = sorted(segment.id for segment in segments)
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("E_ADDITIONAL_FALLBACK_SCOPE_INVALID")
    scope = stable_hash(ids)
    output = work_dir / _ADDITIONAL_FALLBACK_ROOT / scope
    if output.resolve() != output:
        raise ValueError("E_ADDITIONAL_FALLBACK_SCOPE_INVALID")
    receipt = output / "approval.json"
    payload = {
        "schema": _ADDITIONAL_FALLBACK_SCHEMA,
        "source_fingerprint": stable_hash_sequence(asdict(segment) for segment in segments),
        "records": len(ids),
        "route_binding": {"provider": provider, "model": model, "attempt": 1},
        "additional_calls_per_target": 1,
        "scope": scope,
    }
    if receipt.exists():
        if json.loads(receipt.read_text(encoding="utf-8")) != payload:
            raise ValueError("E_ADDITIONAL_FALLBACK_SCOPE_IMMUTABLE")
    else:
        _atomic_json(receipt, payload)
    return output


def _additional_fallback_review_budget(
    work_dir: Path, manifest_path: Path, manifest: dict[str, Any],
) -> int | None:
    namespace = Path(manifest.get("execution_namespace", ""))
    if not namespace.is_relative_to(_ADDITIONAL_FALLBACK_ROOT):
        return None
    expected = {
        "schema": _ADDITIONAL_FALLBACK_SCHEMA,
        "source_fingerprint": manifest["source_fingerprint"],
        "records": manifest["records"],
        "route_binding": manifest.get("route_binding"),
        "additional_calls_per_target": 1,
        "scope": namespace.name,
    }
    try:
        valid = (
            namespace.parent == _ADDITIONAL_FALLBACK_ROOT
            and (work_dir / namespace).resolve() == manifest_path.resolve().parent
            and manifest["execution_contract"].get("review_tier") == "strong"
            and json.loads((manifest_path.parent / "approval.json").read_text(encoding="utf-8")) == expected
        )
    except (OSError, ValueError):
        valid = False
    if not valid:
        raise ValueError("E_ADDITIONAL_FALLBACK_APPROVAL_INVALID")
    return 1




def validate_review_limits(limits: Any) -> None:
    if not isinstance(limits, dict) or any(
        not isinstance(route, str) or not route or not isinstance(count, int)
        or isinstance(count, bool) or count < 1
        for route, count in limits.items()
    ):
        raise ValueError("E_REVIEW_CALL_POLICY_INVALID")




def review_call_limit(
    provider: str, reviewers: list[str], limits: dict[str, int], *, model: str = "",
    tier_limit: int | None = None,
) -> int | None:
    validate_review_limits(limits)
    if provider in limits:
        count = limits[provider]
    elif reviewers and provider == reviewers[0]:
        count = None
    else:
        count = DEFAULT_REVIEW_FALLBACK_CALLS
    if tier_limit is None:
        return count
    return min(count, tier_limit) if count is not None else tier_limit


def judge_calls(attempts: list[Any], provider: str, *, model: str = "", stage_prefix: str = "review") -> int:
    stages = TRIAGE_STAGES if stage_prefix == "review-triage" else JUDGE_STAGES
    matching = [attempt for attempt in attempts if attempt.stage in stages
                and (attempt.model == model if model and attempt.model
                     else attempt.provider == provider)]
    semantic = [attempt for attempt in matching if getattr(attempt, "operational", None) is not True]
    last_ordinal = max((attempt.ordinal for attempt in semantic), default=0)
    operational_ordinals = {
        attempt.ordinal for attempt in matching
        if getattr(attempt, "operational", None) is True and 0 < attempt.ordinal < last_ordinal
    }
    # Ordinals preserve event lineage. Subtract only proven nonsemantic events;
    # unobserved compact-history gaps still conservatively consume the budget.
    return max(len(semantic), last_ordinal - len(operational_ordinals))


def validate_review_settings(settings: dict[str, Any]) -> None:
    for key in REVIEW_SETTING_FIELDS:
        value = settings.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                  or value < 1):
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
    defaulted = [name for name in routing.get("reviewers", [])[1:] if name not in limits]
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
        "status": "native_binding_required" if pending else "configured",
        "default_fallback_max_calls": DEFAULT_REVIEW_FALLBACK_CALLS,
        "review_attempt_limits": dict(limits),
        "review_max_rounds": routing.get("review_max_rounds", 2),
        "review_limits": {
            key: routing[key] for key in REVIEW_SETTING_FIELDS
            if key != "review_max_rounds" and routing.get(key) is not None
        },
        "defaulted_fallback_providers": defaulted,
        "requested_native_models": native_role_models(config),
        "model_authorizations": {
            route: model_selection_authorization(config, route, model)
            for route, model in requested.items()
        },
        "native_binding_pending": pending,
    }


def configure_review_policy(
    work_dir: Path, *, user_requested: bool, model: str | None = None,
    max_calls: int | None = None, provider: str | None = None,
    settings: dict[str, int] | None = None,
) -> dict[str, Any]:
    from .native_runtime import configure_native_runtime, native_runtime
    from .provider_policy import CODEX_APP_PRESETS, validate_provider_policy
    from .workspace import _write_config, load_config

    if not user_requested:
        raise ValueError("E_REVIEW_POLICY_USER_REQUEST_REQUIRED")
    settings = settings or {}
    if set(settings) - set(REVIEW_SETTING_FIELDS):
        raise ValueError("E_REVIEW_TIER_POLICY_INVALID")
    validate_review_settings(settings)
    if model is None and max_calls is not None:
        raise ValueError("E_REVIEW_MODEL_BINDING_INVALID")
    config = load_config(work_dir)
    routing = config["routing"]
    try:
        runtime = native_runtime(work_dir)
    except ValueError:
        runtime = None
    bind_requested = False
    selected = {}
    if model is not None:
        if max_calls is not None:
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
            and (config.get("provider_preset") in CODEX_APP_PRESETS
                 or (config.get("provider_preset") == "custom" and runtime is not None))
        ):
            config = apply_native_role_models(config, {"review-fallback": model})
            routing = config["routing"]
            route = NATIVE_ROLE_ROUTES["review-fallback"]
            bind_requested = True
        else:
            raise ValueError("E_REVIEW_MODEL_BINDING_INVALID")
        if max_calls is None:
            max_calls = routing.get("review_attempt_limits", {}).get(route, DEFAULT_REVIEW_FALLBACK_CALLS)
        routing.setdefault("review_attempt_limits", {})[route] = max_calls
        selected = {"provider": route, "model": model}
    routing.update(settings)
    validate_provider_policy(config)
    _write_config(work_dir, config)
    binding_issue = ""
    if bind_requested and runtime is not None:
        try:
            configure_native_runtime(work_dir, runtime["host"], {})
        except ValueError as error:
            # Preserve the exact request and limit as pending; no model ran.
            binding_issue = str(error).split(":", 1)[0]
    result = {**review_policy_status(load_config(work_dir), work_dir), **selected}
    if binding_issue:
        result["binding_issue_code"] = binding_issue
    return result
