from __future__ import annotations

from typing import Any

from .errors import ConfigurationError


CODEX_APP_PRESET = "codex-app-deepseek"
CODEX_APP_EXECUTION_MODE = "codex_app_native"
MODE_LOCK_VERSION = 1
MODE_LOCK_PATH = ".game-l10n/provider-mode.json"

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

    if require_mode_lock and mode_lock is None:
        _mode_drift("the workspace provider-mode lock is missing")
    if mode_lock is not None:
        if mode_lock.get("version") != MODE_LOCK_VERSION:
            _mode_drift("the workspace provider-mode lock version is invalid")
        if mode_lock.get("provider_preset") != preset:
            _mode_drift("provider_preset differs from the workspace provider-mode lock")
        if mode_lock.get("execution_mode") != mode:
            _mode_drift("execution_mode differs from the workspace provider-mode lock")

    if preset != CODEX_APP_PRESET:
        return

    if not policy:
        _mode_drift("codex-app-deepseek requires an explicit provider_policy")

    native = config.get("native_workflow", {})
    if native.get("required") is not True:
        _mode_drift("Codex App mode requires native workers")
    if native.get("launcher") != "collaboration":
        _mode_drift("Codex App mode requires the native collaboration launcher")
    if native.get("child_model_cli_allowed") is not False:
        _mode_drift("child model CLIs are forbidden in Codex App mode")
    if native.get("external_provider_primary_allowed") is not False:
        _mode_drift("an external provider cannot become primary in Codex App mode")
    if set(native.get("external_provider_roles", [])) != CODEX_APP_EXTERNAL_ROLES:
        _mode_drift("external providers are limited to the declared fallback roles")

    providers = config.get("providers", [])
    if len(providers) != 1:
        _mode_drift("Codex App mode permits only its declared DeepSeek fallback adapter")
    provider = providers[0]
    if provider.get("name") != "deepseek" or provider.get("type") != "compatible_chat":
        _mode_drift("Codex App mode permits only the DeepSeek compatible-chat adapter")
    if provider.get("api_key_env") != "DEEPSEEK_API_KEY":
        _mode_drift("Codex App mode may read only DEEPSEEK_API_KEY")
    if set(provider.get("allowed_roles", [])) != CODEX_APP_EXTERNAL_ROLES:
        _mode_drift("the DeepSeek adapter must remain scoped to fallback roles")

    routing = config.get("routing", {})
    sample_percent = routing.get("strong_review_sample_percent")
    if (
        not isinstance(sample_percent, (int, float))
        or isinstance(sample_percent, bool)
        or not 0 <= sample_percent <= 100
    ):
        _mode_drift("strong_review_sample_percent must be between 0 and 100")
    high_risk_categories = routing.get("strong_review_high_risk_categories")
    if (
        not isinstance(high_risk_categories, list)
        or not all(isinstance(item, str) and item.strip() for item in high_risk_categories)
        or len(high_risk_categories) != len(set(high_risk_categories))
    ):
        _mode_drift("strong-review categories must be unique non-empty strings")
    required_routes = {
        "context_analyzers": ["native-sol"],
        "translators": ["native-luna", "native-sol", "deepseek"],
        "review_triagers": ["native-luna"],
        "reviewers": ["native-sol", "deepseek"],
        "rewriters": ["deepseek"],
    }
    for role, expected in required_routes.items():
        if routing.get(role) != expected:
            _mode_drift(f"Codex App route {role!r} must remain {expected!r}")
    if routing.get("translator_retries") != {
        "native-luna": 5,
        "native-sol": 3,
    }:
        _mode_drift(
            "Codex App translation retries must remain native-luna=5 and native-sol=3"
        )
    if routing.get("defer_fallbacks_until_primary_complete") is not True:
        _mode_drift("fallback cannot start before the complete native primary barrier")
    if routing.get("review_refusal_policy") != "fallback":
        _mode_drift("native review refusal must route to the configured fallback")


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
