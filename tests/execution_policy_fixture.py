from __future__ import annotations


def approved_execution_policy(*failure_codes: str) -> dict:
    return {
        "schema": "parr0t.execution-environment-policy.v1",
        "host": "omp",
        "authorization": "user_requested",
        "fallback_on": list(failure_codes or ("provider_operational_error", "execution_guard_denied")),
        "fallback_strategy": "next_configured_native_route",
        "preserve_successes": True,
        "preserve_isolation": True,
        "silent_model_switch_allowed": False,
    }
