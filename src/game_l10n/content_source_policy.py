from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .privacy import is_within
from .store import stable_hash


GRANT_SCHEMA = "parr0t.content-source-grant.v1"
POLICY_SCHEMA = "parr0t.content-source-policy.v1"
PROVENANCE_SCHEMA = "parr0t.content-provenance.v1"
VALIDATION_SESSION_CONTRACT_SCHEMA = "parr0t.validation-session-contract.v1"
GRANT_DIRECTORY = Path(".game-l10n/content-source-grants")
SEMANTIC_TASK_KINDS = frozenset(
    {
        "translation",
        "rewrite",
        "triage",
        "review",
        "strong_review",
        "dialogue_review",
        "terminology_review",
        "localization_context",
        "image_localization",
    }
)
INVENTORY_TASK_KINDS = frozenset(
    {
        "source_probe",
        "primary_inventory",
        "independent_inventory",
        "inventory_reconciler",
        "image_ui_inventory",
    }
)
FORBIDDEN_EXTERNAL_CONTENT_SOURCES = (
    "browser_translation",
    "external_translation_api",
    "external_machine_translation_library",
    "google_translate",
    "implicit_web_search",
    "remote_dictionary",
)
_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_ROUTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,255}")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _exact_strings(values: Iterable[str], *, field: str) -> list[str]:
    normalized = sorted({str(value).strip() for value in values})
    if not normalized or any(not value or len(value) > 2048 for value in normalized):
        raise ValueError(f"E_CONTENT_SOURCE_GRANT_INVALID: {field} must contain exact values")
    return normalized


def _task_kinds(values: Iterable[str]) -> list[str]:
    result = _exact_strings(values, field="task_kinds")
    if any(value not in SEMANTIC_TASK_KINDS for value in result):
        raise ValueError("E_CONTENT_SOURCE_GRANT_INVALID: unsupported semantic task kind")
    return result


def _routes(values: Iterable[str]) -> list[str]:
    result = _exact_strings(values, field="routes")
    if any(_ROUTE.fullmatch(value) is None for value in result):
        raise ValueError("E_CONTENT_SOURCE_GRANT_INVALID: route is not exact")
    return result


def _sources(values: Iterable[str]) -> list[str]:
    result = _exact_strings(values, field="external_sources")
    if any("*" in value or value.casefold() in {"any", "all", "web", "internet"} for value in result):
        raise ValueError("E_CONTENT_SOURCE_GRANT_INVALID: external source is not exact")
    return result


def _validation_contract(
    work_dir: Path,
    path: Path,
    *,
    scope_id: str,
    task_kinds: list[str],
    routes: list[str],
    external_sources: list[str],
) -> dict[str, Any]:
    resolved = path.resolve()
    if not is_within(resolved, work_dir) or not resolved.is_file():
        raise ValueError("E_CONTENT_SOURCE_VALIDATION_CONTRACT_INVALID")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("E_CONTENT_SOURCE_VALIDATION_CONTRACT_INVALID") from exc
    expected = {
        "schema",
        "contract_id",
        "scope_id",
        "task_kinds",
        "routes",
        "external_sources",
        "external_content_access",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema") != VALIDATION_SESSION_CONTRACT_SCHEMA
        or not isinstance(value.get("contract_id"), str)
        or not value["contract_id"]
        or value.get("scope_id") != scope_id
        or value.get("task_kinds") != task_kinds
        or value.get("routes") != routes
        or value.get("external_sources") != external_sources
        or value.get("external_content_access") is not True
    ):
        raise ValueError("E_CONTENT_SOURCE_VALIDATION_CONTRACT_INVALID")
    return {
        "contract_id": value["contract_id"],
        "contract_path": resolved.relative_to(work_dir.resolve()).as_posix(),
        "contract_sha256": _sha256(resolved),
    }


def create_content_source_grant(
    work_dir: Path,
    *,
    authority: str,
    scope_id: str,
    task_kinds: Iterable[str],
    routes: Iterable[str],
    external_sources: Iterable[str],
    user_confirmed: bool = False,
    validation_contract: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Record an immutable, exact content-source grant without using the source."""

    work_dir = work_dir.resolve()
    scope = str(scope_id).strip()
    if _SCOPE.fullmatch(scope) is None:
        raise ValueError("E_CONTENT_SOURCE_GRANT_INVALID: scope_id is not exact")
    kinds = _task_kinds(task_kinds)
    bound_routes = _routes(routes)
    sources = _sources(external_sources)
    if authority == "validation_session" and any(
        kind in {"translation", "rewrite"} for kind in kinds
    ):
        raise ValueError(
            "E_CONTENT_SOURCE_GRANT_SCOPE_MISMATCH: validation sessions cannot "
            "authorize candidate-producing translation or rewrite"
        )
    if authority == "explicit_user":
        if user_confirmed is not True or validation_contract is not None:
            raise ValueError("E_CONTENT_SOURCE_USER_CONFIRMATION_REQUIRED")
        evidence: dict[str, Any] = {"user_confirmed": True}
    elif authority == "validation_session":
        if user_confirmed or validation_contract is None:
            raise ValueError("E_CONTENT_SOURCE_VALIDATION_CONTRACT_REQUIRED")
        evidence = _validation_contract(
            work_dir,
            validation_contract,
            scope_id=scope,
            task_kinds=kinds,
            routes=bound_routes,
            external_sources=sources,
        )
    else:
        raise ValueError("E_CONTENT_SOURCE_GRANT_INVALID: unsupported authority")
    unsigned = {
        "schema": GRANT_SCHEMA,
        "authority": authority,
        "scope_id": scope,
        "task_kinds": kinds,
        "routes": bound_routes,
        "external_sources": sources,
        "evidence": evidence,
    }
    grant = {**unsigned, "grant_id": stable_hash(unsigned)}
    path = work_dir / GRANT_DIRECTORY / f"{grant['grant_id']}.json"
    if path.is_file():
        try:
            if json.loads(path.read_text(encoding="utf-8")) != grant:
                raise ValueError("E_CONTENT_SOURCE_GRANT_IMMUTABLE")
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("E_CONTENT_SOURCE_GRANT_IMMUTABLE") from exc
    else:
        _atomic_json(path, grant)
    return path, grant


def load_content_source_grant(work_dir: Path, path: Path) -> dict[str, Any]:
    work_dir = work_dir.resolve()
    resolved = path.resolve()
    if not is_within(resolved, work_dir) or resolved.parent != (work_dir / GRANT_DIRECTORY).resolve():
        raise ValueError("E_CONTENT_SOURCE_GRANT_INVALID: grant is outside the immutable store")
    try:
        grant = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("E_CONTENT_SOURCE_GRANT_INVALID: grant is unreadable") from exc
    required = {
        "schema",
        "authority",
        "scope_id",
        "task_kinds",
        "routes",
        "external_sources",
        "evidence",
        "grant_id",
    }
    unsigned = dict(grant) if isinstance(grant, dict) else {}
    grant_id = unsigned.pop("grant_id", None)
    if (
        not isinstance(grant, dict)
        or set(grant) != required
        or grant.get("schema") != GRANT_SCHEMA
        or grant_id != stable_hash(unsigned)
        or resolved.name != f"{grant_id}.json"
        or grant.get("authority") not in {"explicit_user", "validation_session"}
        or _SCOPE.fullmatch(str(grant.get("scope_id", ""))) is None
        or not isinstance(grant.get("task_kinds"), list)
        or not isinstance(grant.get("routes"), list)
        or not isinstance(grant.get("external_sources"), list)
        or grant.get("task_kinds") != _task_kinds(grant.get("task_kinds", []))
        or grant.get("routes") != _routes(grant.get("routes", []))
        or grant.get("external_sources") != _sources(grant.get("external_sources", []))
    ):
        raise ValueError("E_CONTENT_SOURCE_GRANT_INVALID: grant binding changed")
    if grant["authority"] == "explicit_user":
        if grant.get("evidence") != {"user_confirmed": True}:
            raise ValueError("E_CONTENT_SOURCE_GRANT_INVALID: user evidence changed")
    else:
        if any(
            kind in {"translation", "rewrite"}
            for kind in grant["task_kinds"]
        ):
            raise ValueError(
                "E_CONTENT_SOURCE_GRANT_SCOPE_MISMATCH: validation sessions "
                "cannot authorize candidate-producing translation or rewrite"
            )
        evidence = grant.get("evidence")
        if not isinstance(evidence, dict) or set(evidence) != {
            "contract_id",
            "contract_path",
            "contract_sha256",
        }:
            raise ValueError("E_CONTENT_SOURCE_GRANT_INVALID: validation evidence changed")
        contract = (work_dir / str(evidence["contract_path"])).resolve()
        if not is_within(contract, work_dir) or not contract.is_file() or _sha256(contract) != evidence["contract_sha256"]:
            raise ValueError("E_CONTENT_SOURCE_VALIDATION_CONTRACT_INVALID")
    return grant


def content_source_policy(
    work_dir: Path,
    *,
    task_kind: str,
    route: str = "",
    scope_id: str = "",
    grant_path: Path | None = None,
) -> dict[str, Any]:
    """Create the immutable policy bound into one semantic or inventory task."""

    if task_kind not in SEMANTIC_TASK_KINDS | INVENTORY_TASK_KINDS:
        raise ValueError("E_CONTENT_SOURCE_POLICY_INVALID: unsupported task kind")
    semantic = task_kind in SEMANTIC_TASK_KINDS
    authorization = "default_deny"
    grant_id = ""
    allowed_sources: list[str] = []
    bound_scope = ""
    if grant_path is None and scope_id:
        matches = []
        for candidate in sorted((work_dir / GRANT_DIRECTORY).glob("*.json")):
            if candidate.resolve().parent != (work_dir / GRANT_DIRECTORY).resolve():
                continue
            try:
                candidate_scope = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(candidate_scope, dict) or candidate_scope.get("scope_id") != scope_id:
                continue
            grant = load_content_source_grant(work_dir, candidate)
            if (
                grant["scope_id"] == scope_id
                and task_kind in grant["task_kinds"]
                and route in grant["routes"]
            ):
                matches.append((candidate, grant))
        if len(matches) > 1:
            raise ValueError(
                "E_CONTENT_SOURCE_GRANT_AMBIGUOUS: select the exact versioned grant"
            )
        if matches:
            grant_path = matches[0][0]
    if grant_path is not None:
        grant = load_content_source_grant(work_dir, grant_path)
        if (
            not scope_id
            or grant["scope_id"] != scope_id
            or task_kind not in grant["task_kinds"]
            or route not in grant["routes"]
        ):
            raise ValueError("E_CONTENT_SOURCE_GRANT_SCOPE_MISMATCH")
        authorization = grant["authority"]
        grant_id = grant["grant_id"]
        allowed_sources = list(grant["external_sources"])
        bound_scope = scope_id
    elif scope_id:
        raise ValueError("E_CONTENT_SOURCE_GRANT_REQUIRED")
    unsigned = {
        "schema": POLICY_SCHEMA,
        "task_kind": task_kind,
        "route": route,
        "semantic_content_generation": semantic,
        "effect": "allow_exact_sources" if allowed_sources else "deny_external_content",
        "authorization": authorization,
        "scope_id": bound_scope,
        "grant_id": grant_id,
        "allowed_external_sources": allowed_sources,
        "content_network_access": bool(allowed_sources),
        "model_provider_transport": "selected_model_invocation_only_not_a_content_source",
        "candidate_source": "exact_bound_local_task_and_context",
        "direct_model_provenance_required": semantic,
        "inventory_local_inspection_allowed": task_kind in INVENTORY_TASK_KINDS,
        "forbidden_without_exact_grant": list(
            FORBIDDEN_EXTERNAL_CONTENT_SOURCES
        ),
    }
    return {**unsigned, "policy_digest": stable_hash(unsigned)}


def validate_content_source_policy(policy: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(policy, dict) or set(policy) != {
        "schema",
        "task_kind",
        "route",
        "semantic_content_generation",
        "effect",
        "authorization",
        "scope_id",
        "grant_id",
        "allowed_external_sources",
        "content_network_access",
        "model_provider_transport",
        "candidate_source",
        "direct_model_provenance_required",
        "inventory_local_inspection_allowed",
        "forbidden_without_exact_grant",
        "policy_digest",
    }:
        raise ValueError("E_CONTENT_SOURCE_POLICY_INVALID")
    unsigned = dict(policy)
    digest = unsigned.pop("policy_digest")
    sources = policy.get("allowed_external_sources")
    semantic = policy.get("task_kind") in SEMANTIC_TASK_KINDS
    if (
        policy.get("schema") != POLICY_SCHEMA
        or digest != stable_hash(unsigned)
        or policy.get("task_kind")
        not in SEMANTIC_TASK_KINDS | INVENTORY_TASK_KINDS
        or _ROUTE.fullmatch(str(policy.get("route", ""))) is None
        or policy.get("authorization")
        not in {"default_deny", "explicit_user", "validation_session"}
        or not isinstance(sources, list)
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > 2048
            or "*" in item
            for item in sources
        )
        or sources != sorted(set(sources))
        or policy.get("content_network_access") is not bool(sources)
        or policy.get("effect")
        != ("allow_exact_sources" if sources else "deny_external_content")
        or policy.get("semantic_content_generation") is not semantic
        or policy.get("direct_model_provenance_required") is not semantic
        or policy.get("inventory_local_inspection_allowed")
        is not (not semantic)
        or policy.get("model_provider_transport")
        != "selected_model_invocation_only_not_a_content_source"
        or policy.get("candidate_source")
        != "exact_bound_local_task_and_context"
        or policy.get("forbidden_without_exact_grant")
        != list(FORBIDDEN_EXTERNAL_CONTENT_SOURCES)
        or (
            policy.get("authorization") == "default_deny"
            and (
                sources
                or policy.get("grant_id")
                or policy.get("scope_id")
            )
        )
        or (
            policy.get("authorization") != "default_deny"
            and (
                not sources
                or not isinstance(policy.get("grant_id"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", policy["grant_id"])
                or _SCOPE.fullmatch(str(policy.get("scope_id", ""))) is None
            )
        )
    ):
        raise ValueError("E_CONTENT_SOURCE_POLICY_INVALID")
    return policy


def direct_model_provenance(
    policy: dict[str, Any], model: str, *, route: str = ""
) -> dict[str, Any]:
    validate_content_source_policy(policy)
    if (
        not isinstance(model, str)
        or not model
        or not isinstance(route, str)
        or not route
        or _ROUTE.fullmatch(route) is None
    ):
        raise ValueError("E_OUTPUT_PROVENANCE_INVALID")
    return {
        "schema": PROVENANCE_SCHEMA,
        "mode": "direct_model",
        "model": model,
        "route": route,
        "policy_digest": policy["policy_digest"],
        "external_sources_used": [],
    }


def validate_output_provenance(
    value: Any,
    *,
    policy: dict[str, Any],
    model: str,
    route: str = "",
) -> dict[str, Any]:
    """Validate immutable model, route, policy, and external-source evidence."""

    validate_content_source_policy(policy)
    if value is None:
        if policy["semantic_content_generation"]:
            raise ValueError("E_OUTPUT_PROVENANCE_INVALID")
        return direct_model_provenance(policy, model, route=route)
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "mode",
        "model",
        "route",
        "policy_digest",
        "external_sources_used",
    }:
        raise ValueError("E_OUTPUT_PROVENANCE_INVALID")
    used = value.get("external_sources_used")
    if (
        value.get("schema") != PROVENANCE_SCHEMA
        or value.get("mode") != "direct_model"
        or value.get("model") != model
        or value.get("route") != route
        or _ROUTE.fullmatch(str(value.get("route", ""))) is None
        or value.get("policy_digest") != policy["policy_digest"]
        or not isinstance(used, list)
        or any(not isinstance(item, str) or not item for item in used)
        or len(set(used)) != len(used)
    ):
        raise ValueError("E_OUTPUT_PROVENANCE_INVALID")
    allowed = set(policy["allowed_external_sources"])
    if any(source not in allowed for source in used) or (
        used and not policy["content_network_access"]
    ):
        raise ValueError("E_EXTERNAL_CONTENT_PROVENANCE_DENIED")
    return dict(value)
