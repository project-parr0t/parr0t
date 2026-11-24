"""Text-free result contract for an isolated, read-only SDK locale probe."""
from __future__ import annotations

import re
from typing import Any

from .errors import ConfigurationError


PURPOSE = "locale_applicability"
SCHEMA = "parr0t.locale-applicability.v1"
CHECKS = ("loader_binding", "source_catalog", "target_route", "reversible_value_apply")


def validate_locale_probe(payload: Any, task_id: str, result: dict[str, Any]) -> str:
    if not isinstance(payload, dict) or set(payload) != {
        "schema", "task_id", "outcome", "checks", "evidence_sha256",
    }:
        raise ConfigurationError("E_LOCALE_PROBE_RESULT_INVALID")
    checks, outcome = payload["checks"], payload["outcome"]
    digest = payload["evidence_sha256"]
    if (payload["schema"] != SCHEMA or payload["task_id"] != task_id
            or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or not isinstance(checks, dict) or set(checks) != set(CHECKS)
            or any(value not in ("verified", "disproved", "unresolved") for value in checks.values())):
        raise ConfigurationError("E_LOCALE_PROBE_RESULT_INVALID")
    states = set(checks.values())
    if outcome == "applicable" and states == {"verified"}:
        status, counts = "success", {"total": 1, "success": 1, "error": 0, "pending": 0}
    elif outcome == "unavailable" and "disproved" in states:
        status, counts = "success", {"total": 1, "success": 1, "error": 0, "pending": 0}
    elif outcome == "inconclusive" and "unresolved" in states:
        status, counts = "partial", {"total": 1, "success": 0, "error": 0, "pending": 1}
    else:
        raise ConfigurationError("E_LOCALE_PROBE_RESULT_INVALID")
    if (result["status"] != status or any(result["counts"].get(key, 0) != value for key, value in counts.items())
            or not result["issue_codes"] == result["target_ids"] == result["queue_transitions"] == []
            or result.get("route_evidence_path")):
        raise ConfigurationError("E_LOCALE_PROBE_RESULT_INVALID")
    return f"locale_applicability_{outcome}"
