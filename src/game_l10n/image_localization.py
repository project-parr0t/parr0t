from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import ConfigurationError
from .privacy import is_within
from .store import stable_hash


POLICY_SCHEMA = "parr0t.image-localization-policy.v1"
INVENTORY_SCHEMA = "parr0t.image-ui-inventory.v1"
SCOPE_SCHEMA = "parr0t.image-ui-scope-attestation.v1"
RESULT_SCHEMA = "parr0t.image-ui-results.v1"
RESULT_ATTESTATION_SCHEMA = "parr0t.image-localization-attestation.v1"
USER_ACCEPTANCE_SCHEMA = "parr0t.image-user-acceptance.v1"

POLICY_PATH = Path(".game-l10n/image-localization-policy.json")
SCOPE_PATH = Path(".game-l10n/image-ui-scope-attestation.json")
RESULT_ATTESTATION_PATH = Path(".game-l10n/image-localization-attestation.json")
USER_ACCEPTANCE_PATH = Path(".game-l10n/image-user-acceptance.json")

_UI_BINDING_KINDS = {
    "engine_ui_binding",
    "atlas_region_binding",
    "user_identified_ui_asset",
}
_DETECTION_METHODS = {
    "engine_metadata",
    "manual_transcription",
    "ocr_then_visual_confirmation",
}
_REVIEW_STATUSES = {"reviewed", "user_accepted_unreviewed"}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _safe_relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ConfigurationError("E_IMAGE_PATH_INVALID: paths must be nonempty POSIX-relative paths")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ConfigurationError("E_IMAGE_PATH_INVALID: path left its declared root")
    return path.as_posix()


def _policy_digest(policy: dict[str, Any]) -> str:
    return stable_hash(
        {
            "status": policy["status"],
            "request_revision": policy["request_revision"],
            "requested_scope": policy["requested_scope"],
        }
    )


def image_localization_policy(work_dir: Path) -> dict[str, Any]:
    path = work_dir / POLICY_PATH
    if not path.is_file():
        return {
            "schema": POLICY_SCHEMA,
            "status": "disabled",
            "request_revision": 0,
            "requested_scope": "",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("E_IMAGE_POLICY_INVALID: policy is unreadable") from exc
    required = {"schema", "status", "request_revision", "requested_scope"}
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or payload.get("schema") != POLICY_SCHEMA
        or payload.get("status") not in {"disabled", "requested"}
        or not isinstance(payload.get("request_revision"), int)
        or isinstance(payload.get("request_revision"), bool)
        or payload["request_revision"] < 0
        or not isinstance(payload.get("requested_scope"), str)
        or (payload["status"] == "requested" and not payload["requested_scope"].strip())
        or (payload["status"] == "disabled" and payload["requested_scope"] != "")
    ):
        raise ConfigurationError("E_IMAGE_POLICY_INVALID: policy shape is invalid")
    return payload


def record_image_localization_policy(
    work_dir: Path,
    *,
    requested: bool,
    user_requested: bool,
    requested_scope: str = "player_visible_ui_text_assets",
) -> dict[str, Any]:
    if not user_requested:
        raise ConfigurationError(
            "E_IMAGE_USER_REQUEST_REQUIRED: image localization may be enabled or disabled only "
            "after an explicit user request"
        )
    current = image_localization_policy(work_dir)
    normalized_scope = requested_scope.strip() if requested else ""
    if requested and not normalized_scope:
        raise ConfigurationError("E_IMAGE_SCOPE_REQUIRED: requested image scope is empty")
    payload = {
        "schema": POLICY_SCHEMA,
        "status": "requested" if requested else "disabled",
        "request_revision": int(current["request_revision"]) + 1,
        "requested_scope": normalized_scope,
    }
    _atomic_json(work_dir / POLICY_PATH, payload)
    return {
        "schema": POLICY_SCHEMA,
        "status": payload["status"],
        "request_revision": payload["request_revision"],
        "requested_scope": payload["requested_scope"],
    }


def require_image_localization_requested(work_dir: Path) -> dict[str, Any]:
    policy = image_localization_policy(work_dir)
    if policy["status"] != "requested":
        raise ConfigurationError(
            "E_IMAGE_USER_REQUEST_REQUIRED: text localization is the default; do not inspect "
            "images or run OCR without an explicit recorded user request"
        )
    return policy


def _load_workspace_json(work_dir: Path, path: Path, *, label: str) -> tuple[Path, dict[str, Any]]:
    resolved = path.resolve()
    if not is_within(resolved, work_dir.resolve()) or not resolved.is_file():
        raise ConfigurationError(f"E_{label}_PATH_INVALID: artifact must be a workspace file")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"E_{label}_INVALID: artifact is unreadable") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError(f"E_{label}_INVALID: artifact must be an object")
    return resolved, payload


def _validate_inventory(
    work_dir: Path,
    manifest_path: Path,
    game_roots: list[Path],
) -> tuple[Path, dict[str, Any], int]:
    resolved, payload = _load_workspace_json(
        work_dir, manifest_path, label="IMAGE_UI_INVENTORY"
    )
    required = {
        "schema",
        "method",
        "implementation_fingerprint",
        "coverage",
        "assets",
    }
    if set(payload) != required or payload.get("schema") != INVENTORY_SCHEMA:
        raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: schema or fields are invalid")
    if not all(
        isinstance(payload.get(key), str) and payload[key].strip()
        for key in ("method", "implementation_fingerprint")
    ):
        raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: method identity is missing")
    coverage = payload.get("coverage")
    coverage_fields = {
        "declared_complete",
        "candidate_asset_count",
        "confirmed_ui_text_asset_count",
        "uncertain_asset_count",
        "non_ui_asset_count",
    }
    if not isinstance(coverage, dict) or set(coverage) != coverage_fields:
        raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: coverage is invalid")
    counts = [
        coverage.get("candidate_asset_count"),
        coverage.get("confirmed_ui_text_asset_count"),
        coverage.get("uncertain_asset_count"),
        coverage.get("non_ui_asset_count"),
    ]
    if (
        coverage.get("declared_complete") is not True
        or any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in counts)
        or counts[0] != counts[1] + counts[2] + counts[3]
    ):
        raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: coverage accounting is incomplete")
    assets = payload.get("assets")
    if not isinstance(assets, list) or len(assets) != counts[1]:
        raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: confirmed asset count is inconsistent")
    roots = [item.resolve() for item in game_roots]
    if not roots:
        raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: no configured game root")
    seen_assets: set[str] = set()
    region_count = 0
    for asset in assets:
        asset_fields = {
            "id",
            "source_root_index",
            "relative_path",
            "source_sha256",
            "format",
            "width",
            "height",
            "ui_binding",
            "text_regions",
        }
        if not isinstance(asset, dict) or set(asset) != asset_fields:
            raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: asset shape is invalid")
        asset_id = asset.get("id")
        if not isinstance(asset_id, str) or not asset_id or asset_id in seen_assets:
            raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: asset IDs must be unique")
        seen_assets.add(asset_id)
        root_index = asset.get("source_root_index")
        if (
            not isinstance(root_index, int)
            or isinstance(root_index, bool)
            or not 0 <= root_index < len(roots)
        ):
            raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: source root index is invalid")
        relative = _safe_relative(asset.get("relative_path"))
        source = (roots[root_index] / Path(relative)).resolve()
        if not is_within(source, roots[root_index]) or not source.is_file():
            raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: source asset is missing")
        if not _is_sha256(asset.get("source_sha256")) or _sha256(source) != asset["source_sha256"]:
            raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: source asset digest changed")
        if not isinstance(asset.get("format"), str) or not asset["format"].strip():
            raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: image format is missing")
        width, height = asset.get("width"), asset.get("height")
        if any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0
            for item in (width, height)
        ):
            raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: image dimensions are invalid")
        binding = asset.get("ui_binding")
        if (
            not isinstance(binding, dict)
            or set(binding) != {"kind", "evidence_sha256"}
            or binding.get("kind") not in _UI_BINDING_KINDS
            or not _is_sha256(binding.get("evidence_sha256"))
        ):
            raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: definite UI binding is missing")
        regions = asset.get("text_regions")
        if not isinstance(regions, list) or not regions:
            raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: confirmed text regions are missing")
        seen_regions: set[str] = set()
        for region in regions:
            region_fields = {
                "id",
                "bbox",
                "source_text_sha256",
                "detection_method",
                "visually_confirmed",
                "player_visible",
            }
            if not isinstance(region, dict) or set(region) != region_fields:
                raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: text region shape is invalid")
            region_id = region.get("id")
            bbox = region.get("bbox")
            if not isinstance(region_id, str) or not region_id or region_id in seen_regions:
                raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: region IDs must be unique per asset")
            seen_regions.add(region_id)
            if (
                not isinstance(bbox, list)
                or len(bbox) != 4
                or any(not isinstance(item, int) or isinstance(item, bool) for item in bbox)
            ):
                raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: region bounds are invalid")
            x, y, region_width, region_height = bbox
            if (
                x < 0
                or y < 0
                or region_width <= 0
                or region_height <= 0
                or x + region_width > width
                or y + region_height > height
            ):
                raise ConfigurationError("E_IMAGE_UI_INVENTORY_INVALID: region left the image")
            if (
                not _is_sha256(region.get("source_text_sha256"))
                or region.get("detection_method") not in _DETECTION_METHODS
                or region.get("visually_confirmed") is not True
                or region.get("player_visible") is not True
            ):
                raise ConfigurationError(
                    "E_IMAGE_UI_INVENTORY_INVALID: only visually confirmed player-visible text is eligible"
                )
            region_count += 1
    return resolved, payload, region_count


def verify_image_ui_inventory(
    work_dir: Path,
    manifest_path: Path,
    *,
    game_roots: list[Path],
) -> dict[str, Any]:
    policy = require_image_localization_requested(work_dir)
    resolved, payload, region_count = _validate_inventory(
        work_dir, manifest_path, game_roots
    )
    target_count = len(payload["assets"])
    receipt = {
        "schema": SCOPE_SCHEMA,
        "status": "verified_targets" if target_count else "no_targets",
        "request_revision": policy["request_revision"],
        "policy_digest": _policy_digest(policy),
        "inventory_path": resolved.relative_to(work_dir.resolve()).as_posix(),
        "inventory_sha256": _sha256(resolved),
        "target_count": target_count,
        "region_count": region_count,
        "issue_codes": [],
    }
    _atomic_json(work_dir / SCOPE_PATH, receipt)
    return receipt


def _current_scope(work_dir: Path) -> dict[str, Any] | None:
    path = work_dir / SCOPE_PATH
    if not path.is_file():
        return None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    required = {
        "schema",
        "status",
        "request_revision",
        "policy_digest",
        "inventory_path",
        "inventory_sha256",
        "target_count",
        "region_count",
        "issue_codes",
    }
    policy = image_localization_policy(work_dir)
    inventory = (work_dir / str(receipt.get("inventory_path", ""))).resolve()
    if (
        not isinstance(receipt, dict)
        or set(receipt) != required
        or receipt.get("schema") != SCOPE_SCHEMA
        or receipt.get("status") not in {"verified_targets", "no_targets"}
        or receipt.get("request_revision") != policy.get("request_revision")
        or receipt.get("policy_digest") != _policy_digest(policy)
        or not is_within(inventory, work_dir.resolve())
        or not inventory.is_file()
        or not _is_sha256(receipt.get("inventory_sha256"))
        or _sha256(inventory) != receipt["inventory_sha256"]
        or not isinstance(receipt.get("target_count"), int)
        or not isinstance(receipt.get("region_count"), int)
        or receipt.get("issue_codes") != []
    ):
        return None
    return receipt


def _acceptance_digest(payload: dict[str, Any]) -> str:
    return stable_hash(
        {
            "request_revision": payload["request_revision"],
            "inventory_sha256": payload["inventory_sha256"],
            "asset_ids": payload["asset_ids"],
        }
    )


def record_image_unreviewed_acceptance(
    work_dir: Path,
    *,
    asset_ids: list[str],
    user_requested: bool,
) -> dict[str, Any]:
    """Record an explicit user decision for exact, already-scoped image assets."""

    if not user_requested:
        raise ConfigurationError(
            "E_IMAGE_USER_ACCEPTANCE_REQUIRED: only an explicit user decision may accept "
            "unreviewed image outputs"
        )
    scope = _current_scope(work_dir)
    if scope is None or scope["status"] != "verified_targets":
        raise ConfigurationError(
            "E_IMAGE_SCOPE_REQUIRED: verify a current nonempty UI-image inventory first"
        )
    if (
        not isinstance(asset_ids, list)
        or not asset_ids
        or any(not isinstance(item, str) or not item for item in asset_ids)
        or len(set(asset_ids)) != len(asset_ids)
    ):
        raise ConfigurationError(
            "E_IMAGE_USER_ACCEPTANCE_INVALID: asset IDs must be a nonempty unique list"
        )
    inventory_path = (work_dir / scope["inventory_path"]).resolve()
    inventory = json.loads(inventory_path.read_text(encoding="utf-8-sig"))
    scoped_ids = {item["id"] for item in inventory.get("assets", [])}
    if any(item not in scoped_ids for item in asset_ids):
        raise ConfigurationError(
            "E_IMAGE_USER_ACCEPTANCE_INVALID: acceptance contains an asset outside verified scope"
        )
    payload = {
        "schema": USER_ACCEPTANCE_SCHEMA,
        "request_revision": scope["request_revision"],
        "inventory_sha256": scope["inventory_sha256"],
        "asset_ids": asset_ids,
    }
    _atomic_json(work_dir / USER_ACCEPTANCE_PATH, payload)
    return {**payload, "acceptance_digest": _acceptance_digest(payload)}


def _current_user_acceptance(
    work_dir: Path, scope: dict[str, Any]
) -> tuple[set[str], str] | None:
    path = work_dir / USER_ACCEPTANCE_PATH
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    required = {"schema", "request_revision", "inventory_sha256", "asset_ids"}
    asset_ids = payload.get("asset_ids") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or payload.get("schema") != USER_ACCEPTANCE_SCHEMA
        or payload.get("request_revision") != scope["request_revision"]
        or payload.get("inventory_sha256") != scope["inventory_sha256"]
        or not isinstance(asset_ids, list)
        or not asset_ids
        or any(not isinstance(item, str) or not item for item in asset_ids)
        or len(set(asset_ids)) != len(asset_ids)
    ):
        return None
    return set(asset_ids), _acceptance_digest(payload)


def verify_image_localization_results(
    work_dir: Path,
    inventory_path: Path,
    result_path: Path,
    *,
    game_roots: list[Path],
) -> dict[str, Any]:
    require_image_localization_requested(work_dir)
    scope = _current_scope(work_dir)
    if scope is None or scope["status"] != "verified_targets":
        raise ConfigurationError(
            "E_IMAGE_SCOPE_REQUIRED: verify a current nonempty UI-image inventory first"
        )
    inventory_resolved, inventory, _ = _validate_inventory(
        work_dir, inventory_path, game_roots
    )
    if (
        _sha256(inventory_resolved) != scope["inventory_sha256"]
        or inventory_resolved.relative_to(work_dir.resolve()).as_posix()
        != scope["inventory_path"]
    ):
        raise ConfigurationError("E_IMAGE_SCOPE_STALE: result inventory is not the verified scope")
    result_resolved, result = _load_workspace_json(
        work_dir, result_path, label="IMAGE_LOCALIZATION_RESULT"
    )
    if (
        set(result) != {"schema", "inventory_sha256", "assets"}
        or result.get("schema") != RESULT_SCHEMA
        or result.get("inventory_sha256") != scope["inventory_sha256"]
        or not isinstance(result.get("assets"), list)
    ):
        raise ConfigurationError("E_IMAGE_LOCALIZATION_RESULT_INVALID: schema or binding is invalid")
    inventory_assets = inventory["assets"]
    if len(result["assets"]) != len(inventory_assets):
        raise ConfigurationError("E_IMAGE_LOCALIZATION_RESULT_INVALID: result coverage is incomplete")
    unreviewed_count = 0
    user_acceptance = _current_user_acceptance(work_dir, scope)
    output_digests: list[dict[str, str]] = []
    for source, localized in zip(inventory_assets, result["assets"], strict=True):
        fields = {
            "id",
            "source_sha256",
            "output_path",
            "output_sha256",
            "width",
            "height",
            "translated_region_ids",
            "review_status",
            "user_acceptance_evidence_sha256",
            "integrity",
        }
        if not isinstance(localized, dict) or set(localized) != fields:
            raise ConfigurationError("E_IMAGE_LOCALIZATION_RESULT_INVALID: result asset shape is invalid")
        if (
            localized.get("id") != source["id"]
            or localized.get("source_sha256") != source["source_sha256"]
            or localized.get("width") != source["width"]
            or localized.get("height") != source["height"]
            or localized.get("review_status") not in _REVIEW_STATUSES
        ):
            raise ConfigurationError("E_IMAGE_LOCALIZATION_RESULT_INVALID: result asset binding is invalid")
        expected_regions = [item["id"] for item in source["text_regions"]]
        if localized.get("translated_region_ids") != expected_regions:
            raise ConfigurationError("E_IMAGE_LOCALIZATION_RESULT_INVALID: text-region coverage is incomplete")
        review_status = localized["review_status"]
        acceptance = localized.get("user_acceptance_evidence_sha256")
        if review_status == "reviewed":
            if acceptance != "":
                raise ConfigurationError("E_IMAGE_LOCALIZATION_RESULT_INVALID: reviewed output has user override evidence")
        else:
            unreviewed_count += 1
            if (
                user_acceptance is None
                or source["id"] not in user_acceptance[0]
                or acceptance != user_acceptance[1]
            ):
                raise ConfigurationError(
                    "E_IMAGE_LOCALIZATION_RESULT_INVALID: unreviewed output requires a current "
                    "core-recorded user acceptance for this exact asset"
                )
        output_relative = _safe_relative(localized.get("output_path"))
        output = (work_dir / Path(output_relative)).resolve()
        if not is_within(output, work_dir.resolve()) or not output.is_file():
            raise ConfigurationError("E_IMAGE_LOCALIZATION_RESULT_INVALID: localized asset is missing")
        if not _is_sha256(localized.get("output_sha256")) or _sha256(output) != localized["output_sha256"]:
            raise ConfigurationError("E_IMAGE_LOCALIZATION_RESULT_INVALID: localized asset digest changed")
        integrity = localized.get("integrity")
        integrity_fields = {
            "format_valid",
            "dimensions_preserved",
            "alpha_preserved_or_not_applicable",
            "atlas_layout_preserved_or_not_applicable",
            "outside_text_regions_preserved",
            "evidence_sha256",
        }
        if (
            not isinstance(integrity, dict)
            or set(integrity) != integrity_fields
            or any(integrity.get(key) is not True for key in integrity_fields if key != "evidence_sha256")
            or not _is_sha256(integrity.get("evidence_sha256"))
        ):
            raise ConfigurationError("E_IMAGE_LOCALIZATION_RESULT_INVALID: image integrity proof is incomplete")
        output_digests.append(
            {
                "id": source["id"],
                "path": output_relative,
                "sha256": localized["output_sha256"],
            }
        )
    receipt = {
        "schema": RESULT_ATTESTATION_SCHEMA,
        "status": "complete",
        "request_revision": scope["request_revision"],
        "inventory_sha256": scope["inventory_sha256"],
        "result_path": result_resolved.relative_to(work_dir.resolve()).as_posix(),
        "result_sha256": _sha256(result_resolved),
        "target_count": len(result["assets"]),
        "reviewed_count": len(result["assets"]) - unreviewed_count,
        "user_accepted_unreviewed_count": unreviewed_count,
        "output_set_digest": stable_hash(output_digests),
        "outputs": output_digests,
        "issue_codes": [],
    }
    _atomic_json(work_dir / RESULT_ATTESTATION_PATH, receipt)
    return receipt


def public_image_localization_state(work_dir: Path) -> dict[str, Any]:
    try:
        policy = image_localization_policy(work_dir)
    except ConfigurationError:
        return {"status": "invalid", "issue_codes": ["IMAGE_POLICY_INVALID"]}
    if policy["status"] == "disabled":
        return {"status": "disabled", "request_revision": policy["request_revision"], "issue_codes": []}
    scope = _current_scope(work_dir)
    if scope is None:
        return {"status": "scope_required", "request_revision": policy["request_revision"], "issue_codes": []}
    if scope["status"] == "no_targets":
        return {
            "status": "no_targets",
            "request_revision": policy["request_revision"],
            "target_count": 0,
            "region_count": 0,
            "issue_codes": [],
        }
    attestation_path = work_dir / RESULT_ATTESTATION_PATH
    if not attestation_path.is_file():
        return {
            "status": "localization_required",
            "request_revision": policy["request_revision"],
            "target_count": scope["target_count"],
            "region_count": scope["region_count"],
            "issue_codes": [],
        }
    try:
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "invalid", "issue_codes": ["IMAGE_RESULT_ATTESTATION_INVALID"]}
    result = (work_dir / str(attestation.get("result_path", ""))).resolve()
    required = {
        "schema",
        "status",
        "request_revision",
        "inventory_sha256",
        "result_path",
        "result_sha256",
        "target_count",
        "reviewed_count",
        "user_accepted_unreviewed_count",
        "output_set_digest",
        "outputs",
        "issue_codes",
    }
    outputs = attestation.get("outputs")
    outputs_valid = isinstance(outputs, list) and stable_hash(outputs) == attestation.get(
        "output_set_digest"
    )
    if outputs_valid:
        for item in outputs:
            if (
                not isinstance(item, dict)
                or set(item) != {"id", "path", "sha256"}
                or not isinstance(item.get("id"), str)
                or not item["id"]
                or not _is_sha256(item.get("sha256"))
            ):
                outputs_valid = False
                break
            try:
                output_path = (work_dir / Path(_safe_relative(item.get("path")))).resolve()
            except ConfigurationError:
                outputs_valid = False
                break
            if (
                not is_within(output_path, work_dir.resolve())
                or not output_path.is_file()
                or _sha256(output_path) != item["sha256"]
            ):
                outputs_valid = False
                break
    if (
        not isinstance(attestation, dict)
        or set(attestation) != required
        or attestation.get("schema") != RESULT_ATTESTATION_SCHEMA
        or attestation.get("status") != "complete"
        or attestation.get("request_revision") != policy["request_revision"]
        or attestation.get("inventory_sha256") != scope["inventory_sha256"]
        or not is_within(result, work_dir.resolve())
        or not result.is_file()
        or not _is_sha256(attestation.get("result_sha256"))
        or _sha256(result) != attestation["result_sha256"]
        or not outputs_valid
        or attestation.get("issue_codes") != []
    ):
        return {"status": "invalid", "issue_codes": ["IMAGE_RESULT_ATTESTATION_INVALID"]}
    return {
        "status": "complete",
        "request_revision": policy["request_revision"],
        "target_count": attestation["target_count"],
        "reviewed_count": attestation["reviewed_count"],
        "user_accepted_unreviewed_count": attestation["user_accepted_unreviewed_count"],
        "issue_codes": [],
    }
