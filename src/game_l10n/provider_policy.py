from __future__ import annotations

from typing import Any

from .errors import ConfigurationError
from .model_policy import native_role_models


CODEX_APP_PRESET = "codex-app-deepseek"
CODEX_APP_EXECUTION_MODE = "codex_app_native"
MODE_LOCK_VERSION = 1
MODE_LOCK_PATH = ".game-l10n/provider-mode.json"

CONTROL_OBSERVATION_POLICY = {
    "observation_policy_schema": "parr0t.control-observation-policy.v1",
    "root_observation_sources": [
        "core_cli_metadata",
        "sealed_relay_receipt",
        "validated_control_envelope",
    ],
    "root_forbidden_observation_sources": [
        "agent_tree",
        "chat_transcript",
        "codex_session_log",
        "nested_worker_output",
        "nested_worker_status",
        "semantic_worker_log",
    ],
    "direct_relay_wait_only": True,
    "usage_telemetry_sources": [
        "core_usage_ledger",
        "sealed_relay_receipt",
    ],
    "forbidden_observation_action": "record_leak_and_rotate",
}

CODEX_APP_EXTERNAL_ROLES = {
    "translation_fallback",
    "selective_rewrite",
    "judge_fallback",
}

_PRESET_EXECUTION_MODES = {
    "openai-deepseek": "api",
    CODEX_APP_PRESET: CODEX_APP_EXECUTION_MODE,
    "custom": "custom_provider",
}


def provider_mode_lock(config: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable, text-free execution-mode marker for a workspace."""

    preset = str(config.get("provider_preset", ""))
    mode = effective_execution_mode(config)
    return {
        "version": MODE_LOCK_VERSION,
        "provider_preset": preset,
        "execution_mode": mode,
    }


def effective_execution_mode(config: dict[str, Any]) -> str:
    preset = str(config.get("provider_preset", ""))
    expected = _PRESET_EXECUTION_MODES.get(preset)
    if expected is None:
        raise ConfigurationError(
            "E_PROVIDER_MODE_DRIFT: provider_preset must be openai-deepseek, "
            "codex-app-deepseek, or custom"
        )
    return str(config.get("execution_mode") or expected)


def _mode_drift(message: str) -> None:
    raise ConfigurationError(f"E_PROVIDER_MODE_DRIFT: {message}")


def validate_provider_policy(
    config: dict[str, Any],
    *,
    mode_lock: dict[str, Any] | None = None,
    require_mode_lock: bool = False,
) -> None:
    """Validate provider topology before any game text or prompt is constructed."""

    preset = str(config.get("provider_preset", ""))
    expected_mode = _PRESET_EXECUTION_MODES.get(preset)
    if expected_mode is None:
        _mode_drift(
            "provider_preset must be openai-deepseek, codex-app-deepseek, or custom"
        )
    mode = effective_execution_mode(config)
    if mode != expected_mode:
        _mode_drift(
            f"preset {preset!r} requires execution_mode {expected_mode!r}, not {mode!r}"
        )

    policy = config.get("provider_policy", {})
    if policy:
        if policy.get("selected_preset") != preset:
            _mode_drift("provider_policy.selected_preset does not match provider_preset")
        if policy.get("key_presence_selects_mode") is not False:
            _mode_drift("API-key presence must never select or change execution mode")

    control_plane = config.get("control_plane")
    if not isinstance(control_plane, dict):
        _mode_drift("control_plane must be an object")
    for key, expected in CONTROL_OBSERVATION_POLICY.items():
        if control_plane.get(key) != expected:
            _mode_drift(f"control-plane observation policy {key!r} is immutable")

    if require_mode_lock and mode_lock is None:
        _mode_drift("the workspace provider-mode lock is missing")
    if mode_lock is not None:
        if mode_lock.get("version") != MODE_LOCK_VERSION:
            _mode_drift("the workspace provider-mode lock version is invalid")
        if mode_lock.get("provider_preset") != preset:
            _mode_drift("provider_preset differs from the workspace provider-mode lock")
        if mode_lock.get("execution_mode") != mode:
            _mode_drift("execution_mode differs from the workspace provider-mode lock")

    if native_role_models(config) and preset not in {CODEX_APP_PRESET, "custom"}:
        _mode_drift("native role model overrides require a native workflow")

    if preset != CODEX_APP_PRESET:
        return

    if not policy:
        _mode_drift("native mode requires an explicit provider_policy")
    native = config.get("native_workflow", {})
    if native.get("required") is not True:
        _mode_drift("Codex App mode requires native workers")
    if native.get("launcher") != "collaboration":
        _mode_drift("Codex App mode requires the native collaboration launcher")
    if native.get("child_model_cli_allowed") is not False:
        _mode_drift("child model CLIs are forbidden in Codex App mode")
    if native.get("external_provider_primary_allowed") is not False:
        _mode_drift("an external provider cannot become primary in Codex App mode")
    declared_roles = native.get("external_provider_roles", [])
    if not isinstance(declared_roles, list) or set(declared_roles) - CODEX_APP_EXTERNAL_ROLES:
        _mode_drift("external providers require declared consent-scoped roles")
    providers = config.get("providers", [])
    if not isinstance(providers, list):
        _mode_drift("providers must be an explicit adapter list")
    names = set()
    for provider in providers:
        name = provider.get("name")
        if not isinstance(name, str) or not name or name in names:
            _mode_drift("providers require unique explicit names")
        names.add(name)
        roles = provider.get("allowed_roles")
        if not isinstance(roles, list) or not roles or set(roles) - set(declared_roles):
            _mode_drift("external adapters require explicit consent-scoped fallback roles")
        if provider.get("type") != "compatible_chat":
            _mode_drift("native workflows permit only explicitly configured compatible-chat fallback adapters")
        if not isinstance(provider.get("api_key_env"), str) or not provider["api_key_env"]:
            _mode_drift("each external adapter requires its explicit credential binding")

    routing = config.get("routing", {})
    sample_percent = routing.get("strong_review_sample_percent", 0)
    if (
        not isinstance(sample_percent, (int, float))
        or isinstance(sample_percent, bool)
        or not 0 <= sample_percent <= 100
    ):
        _mode_drift("strong_review_sample_percent must be between 0 and 100")
    high_risk_categories = routing.get("strong_review_high_risk_categories", [])
    if (
        not isinstance(high_risk_categories, list)
        or not all(isinstance(item, str) and item.strip() for item in high_risk_categories)
        or len(high_risk_categories) != len(set(high_risk_categories))
    ):
        _mode_drift("strong-review categories must be unique non-empty strings")
    role_permissions = {
        "translators": "translation_fallback", "rewriters": "selective_rewrite",
        "reviewers": "judge_fallback",
    }
    adapters = {provider["name"]: provider for provider in providers}
    for role in ("translators", "review_triagers", "reviewers", "rewriters", "context_analyzers"):
        routes = routing.get(role, [])
        if not isinstance(routes, list) or any(not isinstance(route, str) or not route for route in routes):
            _mode_drift("routing roles require explicit route lists")
        for index, route in enumerate(routes):
            if route.startswith("native-"):
                continue
            adapter = adapters.get(route)
            permission = role_permissions.get(role)
            if adapter is None or permission not in adapter["allowed_roles"]:
                _mode_drift("external route is not authorized for this explicit role")
            if role in {"translators", "reviewers"} and index == 0:
                _mode_drift("an external adapter cannot become native translation or review primary")
    retries = routing.get("translator_retries", {})
    if not isinstance(retries, dict) or any(
        route not in routing.get("translators", []) or type(count) is not int or count < 0
        for route, count in retries.items()
    ):
        _mode_drift("translation retries require nonnegative counts for configured routes")


def require_direct_provider_pipeline(config: dict[str, Any], operation: str) -> None:
    """Prevent API/command orchestration from impersonating native App workers."""

    validate_provider_policy(config)
    if effective_execution_mode(config) == CODEX_APP_EXECUTION_MODE:
        raise ConfigurationError(
            "E_NATIVE_WORKFLOW_REQUIRED: "
            f"{operation} must use native collaboration workers and private native-pass "
            "or core data-plane relay artifacts; stop only this direct-provider command, "
            "then run game-l10n status and dispatch its reported relay. Do not run "
            "codex.exe, codex exec, another Codex session, or a command provider as a "
            "substitute"
        )
