from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .errors import GameL10nError


CHECKPOINT_NAME = "parr0t-release-checkpoint.json"
CHECKPOINT_SCHEMA = "parr0t.release-checkpoint/v1"
LOCAL_INTEGRITY_SCHEMA = "parr0t.local-release-integrity/v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")
_STATUS = {"reviewed", "accepted_unreviewed", "source_preserved"}
_LOCAL_STATUS = _STATUS | {"unreviewed"}
_FALLBACK_MODES = {"fallback_locale", "placeholder", "source_text", "none"}


class ReleaseCheckpointError(GameL10nError):
    pass


def _required_keys(payload: dict[str, Any], expected: set[str], *, scope: str) -> None:
    missing = expected - set(payload)
    extra = set(payload) - expected
    if missing or extra:
        raise ReleaseCheckpointError(
            f"{scope} fields do not match the checkpoint contract "
            f"(missing={sorted(missing)}, extra={sorted(extra)})"
        )


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseCheckpointError(f"{field} must be a non-empty string")
    return value


def _digest(value: Any, *, field: str) -> str:
    value = _text(value, field=field)
    if not _SHA256.fullmatch(value):
        raise ReleaseCheckpointError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _validate_locale_fallback(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReleaseCheckpointError("locale_fallback must be an object")
    _required_keys(
        payload,
        {"status", "mode", "fallback_locale", "placeholder", "verified", "reason_code"},
        scope="locale_fallback",
    )
    status = _text(payload["status"], field="locale_fallback.status")
    mode = _text(payload["mode"], field="locale_fallback.mode")
    if mode not in _FALLBACK_MODES:
        raise ReleaseCheckpointError("locale_fallback.mode is unsupported")
    if not isinstance(payload["verified"], bool):
        raise ReleaseCheckpointError("locale_fallback.verified must be boolean")
    fallback_locale = payload["fallback_locale"]
    placeholder = payload["placeholder"]
    reason_code = payload["reason_code"]
    if not all(isinstance(item, str) for item in (fallback_locale, placeholder, reason_code)):
        raise ReleaseCheckpointError("locale fallback optional fields must be strings")
    if status == "implemented":
        if mode == "none" or payload["verified"] is not True:
            raise ReleaseCheckpointError(
                "implemented locale fallback requires a verified non-none mode"
            )
        if mode == "fallback_locale" and not fallback_locale.strip():
            raise ReleaseCheckpointError(
                "fallback_locale mode requires an explicit locale such as en"
            )
        if mode == "placeholder" and not placeholder:
            raise ReleaseCheckpointError("placeholder mode requires placeholder text")
        if reason_code:
            raise ReleaseCheckpointError("implemented locale fallback cannot have a reason code")
    elif status == "unsupported":
        if mode != "none" or payload["verified"] is not False or not reason_code.strip():
            raise ReleaseCheckpointError(
                "unsupported locale fallback requires mode none, verified false, and a reason code"
            )
        if fallback_locale or placeholder:
            raise ReleaseCheckpointError(
                "unsupported locale fallback cannot claim a locale or placeholder"
            )
    else:
        raise ReleaseCheckpointError("locale_fallback.status must be implemented or unsupported")
    return payload


def validate_release_checkpoint(
    payload: Any,
    *,
    expected_patch_id: str | None = None,
    expected_release_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReleaseCheckpointError("Release checkpoint must be a JSON object")
    if payload.get("schema") == LOCAL_INTEGRITY_SCHEMA:
        return _validate_local_integrity(
            payload, expected_patch_id=expected_patch_id,
            expected_release_id=expected_release_id,
        )
    _required_keys(
        payload,
        {
            "schema",
            "patch_id",
            "release_id",
            "sdk",
            "game_builds",
            "source_inventory",
            "context",
            "entries",
            "locale_fallback",
        },
        scope="checkpoint",
    )
    if payload["schema"] != CHECKPOINT_SCHEMA:
        raise ReleaseCheckpointError("Release checkpoint schema is unsupported")
    patch_id = _text(payload["patch_id"], field="patch_id")
    release_id = _text(payload["release_id"], field="release_id")
    if expected_patch_id is not None and patch_id != expected_patch_id:
        raise ReleaseCheckpointError("Release checkpoint patch_id does not match the package")
    if expected_release_id is not None and release_id != expected_release_id:
        raise ReleaseCheckpointError("Release checkpoint release_id does not match the package")

    sdk = payload["sdk"]
    if not isinstance(sdk, dict):
        raise ReleaseCheckpointError("sdk must be an object")
    _required_keys(sdk, {"branch", "commit", "adapter_fingerprint"}, scope="sdk")
    _text(sdk["branch"], field="sdk.branch")
    commit = _text(sdk["commit"], field="sdk.commit")
    if not _GIT_COMMIT.fullmatch(commit):
        raise ReleaseCheckpointError("sdk.commit must be a Git commit ID")
    _digest(sdk["adapter_fingerprint"], field="sdk.adapter_fingerprint")

    builds = payload["game_builds"]
    if not isinstance(builds, list) or not builds:
        raise ReleaseCheckpointError("game_builds must contain at least one verified build")
    build_ids: set[str] = set()
    for index, build in enumerate(builds):
        if not isinstance(build, dict):
            raise ReleaseCheckpointError(f"game_builds[{index}] must be an object")
        _required_keys(build, {"id", "binding_fingerprint"}, scope=f"game_builds[{index}]")
        build_id = _text(build["id"], field=f"game_builds[{index}].id")
        if build_id in build_ids:
            raise ReleaseCheckpointError("game build IDs must be unique")
        build_ids.add(build_id)
        _digest(
            build["binding_fingerprint"],
            field=f"game_builds[{index}].binding_fingerprint",
        )

    inventory = payload["source_inventory"]
    if not isinstance(inventory, dict):
        raise ReleaseCheckpointError("source_inventory must be an object")
    _required_keys(
        inventory,
        {"method", "fingerprint", "target_count"},
        scope="source_inventory",
    )
    _text(inventory["method"], field="source_inventory.method")
    _digest(inventory["fingerprint"], field="source_inventory.fingerprint")
    if not isinstance(inventory["target_count"], int) or inventory["target_count"] < 1:
        raise ReleaseCheckpointError("source_inventory.target_count must be positive")

    context = payload["context"]
    if not isinstance(context, dict):
        raise ReleaseCheckpointError("context must be an object")
    _required_keys(
        context,
        {"fingerprint", "review_policy_fingerprint"},
        scope="context",
    )
    _digest(context["fingerprint"], field="context.fingerprint")
    _digest(
        context["review_policy_fingerprint"],
        field="context.review_policy_fingerprint",
    )

    entries = payload["entries"]
    if not isinstance(entries, list) or len(entries) != inventory["target_count"]:
        raise ReleaseCheckpointError(
            "entries must match source_inventory.target_count exactly"
        )
    ids: set[str] = set()
    dependencies: dict[str, list[str]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ReleaseCheckpointError(f"entries[{index}] must be an object")
        _required_keys(
            entry,
            {
                "id",
                "source_sha256",
                "target",
                "target_sha256",
                "location_fingerprint",
                "dependency_fingerprint",
                "review_fingerprint",
                "status",
                "dependencies",
            },
            scope=f"entries[{index}]",
        )
        target_id = _text(entry["id"], field=f"entries[{index}].id")
        if target_id in ids:
            raise ReleaseCheckpointError("checkpoint target IDs must be unique")
        ids.add(target_id)
        source_digest = _digest(
            entry["source_sha256"], field=f"entries[{index}].source_sha256"
        )
        target = entry["target"]
        if not isinstance(target, str):
            raise ReleaseCheckpointError(f"entries[{index}].target must be a string")
        target_digest = _digest(
            entry["target_sha256"], field=f"entries[{index}].target_sha256"
        )
        calculated = hashlib.sha256(target.encode("utf-8")).hexdigest()
        if target_digest != calculated:
            raise ReleaseCheckpointError(
                f"entries[{index}].target_sha256 does not match target"
            )
        for key in (
            "location_fingerprint",
            "dependency_fingerprint",
            "review_fingerprint",
        ):
            _digest(entry[key], field=f"entries[{index}].{key}")
        status = entry["status"]
        if status not in _STATUS:
            raise ReleaseCheckpointError(
                f"entries[{index}].status must be reviewed, accepted_unreviewed, "
                "or source_preserved"
            )
        if status == "source_preserved" and source_digest != target_digest:
            raise ReleaseCheckpointError(
                "source_preserved checkpoint targets must hash to the source digest"
            )
        refs = entry["dependencies"]
        if not isinstance(refs, list) or not all(
            isinstance(item, str) and item for item in refs
        ):
            raise ReleaseCheckpointError(
                f"entries[{index}].dependencies must be target ID strings"
            )
        if len(refs) != len(set(refs)) or target_id in refs:
            raise ReleaseCheckpointError(
                f"entries[{index}].dependencies must be unique and cannot include itself"
            )
        dependencies[target_id] = refs
    unknown_dependencies = {
        dependency
        for refs in dependencies.values()
        for dependency in refs
        if dependency not in ids
    }
    if unknown_dependencies:
        raise ReleaseCheckpointError("checkpoint dependencies reference unknown targets")

    _validate_locale_fallback(payload["locale_fallback"])
    return payload


def _validate_local_integrity(
    payload: dict[str, Any], *,
    expected_patch_id: str | None, expected_release_id: str | None,
) -> dict[str, Any]:
    """Local delivery retains integrity without claiming future-update readiness."""
    from .language_policy import normalize_language_tag

    _required_keys(payload, {
        "schema", "patch_id", "release_id", "ownership", "source_inventory",
        "language", "entries",
    } | ({"integrity_evidence"} if "integrity_evidence" in payload else set()), scope="local_integrity")
    for key, expected in (("patch_id", expected_patch_id), ("release_id", expected_release_id)):
        actual = _text(payload[key], field=key)
        if expected is not None and expected != actual:
            raise ReleaseCheckpointError(f"Local integrity {key} does not match package")
    ownership = payload["ownership"]
    if not isinstance(ownership, dict):
        raise ReleaseCheckpointError("ownership must be an object")
    _required_keys(ownership, {"owner", "license"}, scope="ownership")
    for key in ("owner", "license"):
        _text(ownership[key], field=f"ownership.{key}")
    language = payload["language"]
    if not isinstance(language, dict):
        raise ReleaseCheckpointError("language must be an object")
    _required_keys(language, {"source", "target"}, scope="language")
    for key in ("source", "target"):
        try:
            canonical = normalize_language_tag(language[key], field=key)
        except GameL10nError as exc:
            raise ReleaseCheckpointError("language tags must be valid") from exc
        if canonical != language[key]:
            raise ReleaseCheckpointError("language tags must be canonical")
    inventory = payload["source_inventory"]
    if not isinstance(inventory, dict):
        raise ReleaseCheckpointError("source_inventory must be an object")
    _required_keys(inventory, {"method", "fingerprint", "target_count"}, scope="source_inventory")
    _text(inventory["method"], field="source_inventory.method")
    _digest(inventory["fingerprint"], field="source_inventory.fingerprint")
    count = inventory["target_count"]
    entries = payload["entries"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1 or not isinstance(entries, list) or len(entries) != count:
        raise ReleaseCheckpointError("entries must match positive source_inventory.target_count")
    ids = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ReleaseCheckpointError("local entries must be objects")
        _required_keys(entry, {
            "id", "source_sha256", "target", "target_sha256", "status",
            "integrity_evidence_sha256",
        }, scope="local entry")
        target_id = _text(entry["id"], field="entry.id")
        if target_id in ids:
            raise ReleaseCheckpointError("local entry IDs must be unique")
        ids.add(target_id)
        source_digest = _digest(entry["source_sha256"], field="entry.source_sha256")
        target_digest = _digest(entry["target_sha256"], field="entry.target_sha256")
        _digest(entry["integrity_evidence_sha256"], field="entry.integrity_evidence_sha256")
        if not isinstance(entry["target"], str) or hashlib.sha256(entry["target"].encode("utf-8")).hexdigest() != target_digest:
            raise ReleaseCheckpointError("local entry target digest mismatch")
        if entry["status"] not in _LOCAL_STATUS:
            raise ReleaseCheckpointError("local entry status is unsupported")
        if entry["status"] == "source_preserved" and source_digest != target_digest:
            raise ReleaseCheckpointError("source_preserved local target must match source")
    if "integrity_evidence" in payload:
        evidence = payload["integrity_evidence"]
        if not isinstance(evidence, dict) or set(evidence) != ids:
            raise ReleaseCheckpointError("local integrity evidence must match exact target IDs")
        for entry in entries:
            proof = evidence[entry["id"]]
            if (
                not isinstance(proof, dict)
                or proof.get("source_sha256") != entry["source_sha256"]
                or proof.get("target_sha256") != entry["target_sha256"]
                or _evidence_digest(proof) != entry["integrity_evidence_sha256"]
            ):
                raise ReleaseCheckpointError("local integrity evidence binding mismatch")
    return payload


def _evidence_digest(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def generate_local_release_integrity(
    work_dir: Path, inventory_path: Path, results: Iterable[Any], *,
    patch_id: str, release_id: str, owner: str, license_name: str,
    source_language: str, target_language: str,
    review_required: bool = True, allow_incomplete_artifact: bool = False,
    verified_subset: bool = False,
    glossary: Iterable[Any] = (), extra_patterns: list[str] | None = None,
    max_length_ratio: float = 4.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Generate a self-contained local checkpoint from actual validated results."""
    from .coverage import _inventory_document, completion_attestation, inventory_fingerprint, require_inventory_attestation
    from .language_policy import language_binding, normalize_language_tag
    from .models import FailureCode, Segment, TranslationResult
    from .store import stable_hash
    from . import validators

    if not isinstance(review_required, bool) or not isinstance(allow_incomplete_artifact, bool):
        raise ReleaseCheckpointError("review and incomplete artifact options must be boolean")
    _text(owner, field="ownership.owner")
    _text(license_name, field="ownership.license")
    config = json.loads((work_dir / "game-l10n.json").read_text(encoding="utf-8"))
    binding = language_binding(work_dir, config)
    source_language = normalize_language_tag(source_language, field="source_language")
    target_language = normalize_language_tag(target_language, field="target_language")
    if source_language != binding["source_language"] or target_language != binding["target_language"]:
        raise ReleaseCheckpointError("local release language does not match current source/target policy")
    inventory = require_inventory_attestation(work_dir, inventory_path, config=config)
    document = _inventory_document(inventory_path)
    if inventory_fingerprint(document.payload) != inventory["primary_fingerprint"]:
        raise ReleaseCheckpointError("inventory changed during local release generation")
    segments = [
        Segment(**{key: row[key] for key in (
            "id", "source", "speaker", "scene", "previous", "next", "metadata",
        ) if key in row})
        for row in document.active_rows
    ]
    rows = list(results)
    if verified_subset:
        selected = {row.segment_id for row in rows if isinstance(row, TranslationResult)}
        if (not release_id.startswith("partial-") or not selected or len(selected) != len(rows)
                or any(row.status not in {"reviewed", "accepted_unreviewed"} for row in rows)):
            raise ReleaseCheckpointError("verified subset requires an explicit partial release and accepted targets")
        segments = [segment for segment in segments if segment.id in selected]
    if (
        not rows or any(not isinstance(row, TranslationResult) for row in rows)
        or [row.segment_id for row in rows] != [segment.id for segment in segments]
    ):
        raise ReleaseCheckpointError("local results must match exact ordered active inventory")
    for segment, row in zip(segments, rows):
        if row.source != segment.source or row.status not in _STATUS | {"translated"}:
            raise ReleaseCheckpointError("local result source or status is invalid")
        if row.resolution_code == FailureCode.INTEGRITY_INVALID.value:
            raise ReleaseCheckpointError("integrity-rejected results cannot be released")
        if row.status == "source_preserved":
            if row.text != segment.source:
                raise ReleaseCheckpointError("source-preserved result changed its source")
        elif row.candidate_digest != stable_hash(row.text):
            raise ReleaseCheckpointError("local candidate digest does not match released text")
        if row.status == "accepted_unreviewed" and row.acceptance_authority != "user_policy":
            raise ReleaseCheckpointError("accepted-unreviewed result requires real user-policy authority")
    completion = completion_attestation(
        segments, rows, inventory_segments=segments, inventory=inventory, runtime={},
        review_required=review_required, target_language=target_language, language_binding=binding,
        allow_incomplete_artifact=allow_incomplete_artifact,
    )
    if (
        completion["semantic_review"]["reviewed_count"] != sum(row.status == "reviewed" for row in rows)
        or not completion["accounting_complete"]
        or (not completion["target_completion"] and not completion["incomplete_artifact_authorized"])
    ):
        raise ReleaseCheckpointError("local results have not passed the selected review/incomplete policy")
    glossary_rows = list(glossary)
    glossary_digest = stable_hash([{
        key: getattr(entry, key) for key in entry.__dataclass_fields__
    } for entry in glossary_rows])
    validator_digest = hashlib.sha256(Path(validators.__file__).read_bytes()).hexdigest()
    evidence = {}
    entries = []
    for segment, row in zip(segments, rows):
        issues = validators.validate_integrity(
            segment, row.text, glossary_rows, extra_patterns=extra_patterns,
            max_length_ratio=max_length_ratio,
        )
        if issues:
            raise ReleaseCheckpointError("local result failed deterministic integrity validation")
        source_digest = hashlib.sha256(segment.source.encode("utf-8")).hexdigest()
        target_digest = hashlib.sha256(row.text.encode("utf-8")).hexdigest()
        status = "unreviewed" if row.status == "translated" else row.status
        proof = {
            "schema": "parr0t.local-integrity-evidence.v1",
            "source_sha256": source_digest, "target_sha256": target_digest,
            "inventory_fingerprint": inventory["primary_fingerprint"],
            "validator_source_sha256": validator_digest,
            "protected_patterns": list(extra_patterns or []),
            "max_length_ratio": max_length_ratio,
            "glossary_digest": glossary_digest,
            "status": status, "candidate_generation": row.candidate_generation,
            "candidate_digest": row.candidate_digest,
            "acceptance_authority": row.acceptance_authority,
            "review_verdicts": [{
                "stage": attempt.stage, "provider": attempt.provider, "model": attempt.model,
                "status": attempt.status, "candidate_generation": attempt.candidate_generation,
            } for attempt in row.attempts if attempt.status == "approved"],
            "validation_issues": [],
        }
        evidence[segment.id] = proof
        entries.append({
            "id": segment.id, "source_sha256": source_digest, "target": row.text,
            "target_sha256": target_digest, "status": status,
            "integrity_evidence_sha256": _evidence_digest(proof),
        })
    checkpoint = {
        "schema": LOCAL_INTEGRITY_SCHEMA, "patch_id": patch_id, "release_id": release_id,
        "ownership": {"owner": owner, "license": license_name},
        "source_inventory": {
            "method": document.payload["method"], "fingerprint": inventory["primary_fingerprint"],
            "target_count": len(segments),
        },
        "language": {"source": source_language, "target": target_language},
        "entries": entries, "integrity_evidence": evidence,
    }
    validate_release_checkpoint(checkpoint, expected_patch_id=patch_id, expected_release_id=release_id)
    return checkpoint, evidence


def canonical_checkpoint_bytes(
    path: Path,
    *,
    expected_patch_id: str | None = None,
    expected_release_id: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    validated = validate_release_checkpoint(
        payload,
        expected_patch_id=expected_patch_id,
        expected_release_id=expected_release_id,
    )
    data = (
        json.dumps(validated, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    return validated, data
