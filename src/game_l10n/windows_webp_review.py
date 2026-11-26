"""Stage and seal an independent visual review of an exact completed WebP draft."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ERROR = "E_WEBP_VISUAL_REVIEW_BINDING_INVALID"
SCHEMA = "parr0t.webp-visual-review.v1"


def _read(path: Path, limit: int = 8 * 1024 * 1024) -> bytes:
    from game_l10n.errors import ConfigurationError

    if path.resolve() != path or not path.is_file():
        raise ConfigurationError(ERROR)
    with path.open("rb") as stream:
        body = stream.read(limit + 1)
    if len(body) > limit:
        raise ConfigurationError(ERROR)
    return body


def _binding(work: Path, relay_path: Path) -> dict:
    from game_l10n.data_plane_task import _load_bound_data_plane_relay
    from game_l10n.errors import ConfigurationError
    from game_l10n.image_localization import _validate_inventory
    from game_l10n.image_region_pixels import _MAX_FILE
    from game_l10n.native_runtime import native_launch_evidence, require_native_workers_ended

    relay, draft = _load_bound_data_plane_relay(work, relay_path)
    if draft.get("purpose") != "image_cohort_draft" or "launch_contract" not in relay:
        raise ConfigurationError(ERROR)
    require_native_workers_ended(work, relay_path)
    if native_launch_evidence(work, relay_path)["issue_codes"]:
        raise ConfigurationError(ERROR)
    stage = work / ".game-l10n/data-plane" / draft["purpose"]
    receipt = json.loads(_read(stage / "receipt.json"))
    output = work / draft["output_image_root"]
    processing = json.loads(_read(output / "webp-encoding-status.json"))
    inventory = work / draft["input_image_inventory"]
    if (receipt.get("task_id") != draft["task_id"] or receipt.get("execution", {}).get("complete") is not True
            or processing.get("task_id") != draft["task_id"]
            or processing.get("status") != "pixel_checked_not_visually_reviewed"
            or hashlib.sha256(_read(inventory)).hexdigest() != draft["input_image_inventory_sha256"]):
        raise ConfigurationError(ERROR)
    _, report, _, sources = _validate_inventory(work, inventory, [Path(root) for root in draft["source_roots"]])
    assets = []
    for ordinal, asset in enumerate(report["assets"], 1):
        if asset["format"].casefold() not in {"webp", "image/webp"}:
            raise ConfigurationError(ERROR)
        original = sources.roots[asset["source_root_index"]] / asset["relative_path"]
        edited = output / f"asset-{ordinal:04d}.webp"
        assets.append({"ordinal": ordinal, "id": asset["id"], "original": str(original),
                       "original_sha256": asset["source_sha256"], "edited": edited.relative_to(work).as_posix(),
                       "edited_sha256": hashlib.sha256(_read(edited, _MAX_FILE)).hexdigest(),
                       "dimensions": [asset["width"], asset["height"]], "regions": asset["text_regions"]})
    if not assets or processing.get("encoded_webp", 0) + processing.get("reused_webp", 0) != len(assets):
        raise ConfigurationError(ERROR)
    return {"draft_task_id": draft["task_id"], "inventory_sha256": draft["input_image_inventory_sha256"], "assets": assets}


def _review_counts(work: Path, task: dict, path: Path) -> dict:
    from game_l10n.errors import ConfigurationError
    from game_l10n.image_region_pixels import _MAX_FILE

    evidence = task["webp_visual_review"]
    output = work / task["webp_visual_review_png_root"]
    pairs = json.loads(_read(output / "pairs.json"))
    report = json.loads(_read(path))
    assets = evidence["assets"]
    if (set(report) != {"schema", "task_id", "draft_task_id", "records"} or report["schema"] != SCHEMA
            or report["task_id"] != task["task_id"] or report["draft_task_id"] != evidence["draft_task_id"]
            or not isinstance(report["records"], list) or len(report["records"]) != len(assets)
            or pairs.get("task_id") != task["task_id"] or len(pairs.get("pairs", [])) != len(assets)):
        raise ConfigurationError(ERROR)
    counts = dict.fromkeys(("success", "error", "pending"), 0)
    statuses = {"approved": "success", "revise": "error", "unresolved": "pending"}
    for asset, row, pair in zip(assets, report["records"], pairs["pairs"], strict=True):
        ordinal = asset["ordinal"]
        if (not isinstance(row, dict) or set(row) != {"ordinal", "status"} or type(row["ordinal"]) is not int
                or row["ordinal"] != ordinal or row["status"] not in statuses or pair.get("ordinal") != ordinal):
            raise ConfigurationError(ERROR)
        for variant in ("source", "edited"):
            png = output / f"asset-{ordinal:04d}.{variant}.png"
            if hashlib.sha256(_read(png, _MAX_FILE)).hexdigest() != pair.get(variant + "_sha256"):
                raise ConfigurationError(ERROR)
        if hashlib.sha256(_read(work / asset["edited"], _MAX_FILE)).hexdigest() != asset["edited_sha256"]:
            raise ConfigurationError(ERROR)
        counts[statuses[row["status"]]] += 1
    return {"total": len(assets), **counts}


def _seal_review_evidence(work: Path, relay_path: Path) -> dict:
    from game_l10n.data_plane_task import (
        _atomic_json, _load_bound_data_plane_relay, _relay_execution_summary, data_plane_receipt_path,
    )
    from game_l10n.errors import ConfigurationError
    from game_l10n.native_runtime import native_launch_evidence, require_native_workers_ended

    relay, task = _load_bound_data_plane_relay(work, relay_path)
    if not task.get("webp_visual_review") or "launch_contract" not in relay:
        raise ConfigurationError(ERROR)
    require_native_workers_ended(work, relay_path)
    if native_launch_evidence(work, relay_path)["issue_codes"]:
        raise ConfigurationError(ERROR)
    receipt_path = data_plane_receipt_path(work, relay["worker_role"], purpose=task["purpose"])
    receipt_body = _read(receipt_path)
    receipt = json.loads(receipt_body)
    artifact = work / task["output_artifact"]
    if (receipt.get("task_id") != task["task_id"] or not receipt.get("execution", {}).get("complete")
            or _relay_execution_summary(work, relay) != receipt["execution"]):
        raise ConfigurationError(ERROR)
    report_digest = hashlib.sha256(_read(artifact)).hexdigest()
    recovering = receipt["status"] == "error" and receipt["issue_codes"] in (
        ["malformed_response"], ["malformed_response", "invalid_envelope_issue_codes"],
    ) and not receipt["artifact_paths"]
    if not recovering and (receipt["status"] not in {"success", "partial"}
            or task["output_artifact"] not in receipt["artifact_paths"]
            or _read(artifact.with_suffix(".sha256.txt"), 64) != report_digest.encode("ascii")):
        raise ConfigurationError(ERROR)
    counts = _review_counts(work, task, artifact)
    if hashlib.sha256(_read(artifact)).hexdigest() != report_digest:
        raise ConfigurationError(ERROR)
    result = {"schema": "parr0t.webp-visual-review-evidence.v1", "task_id": task["task_id"],
              "draft_task_id": task["webp_visual_review"]["draft_task_id"],
              "status": "approved" if counts["success"] == counts["total"] else "partial",
              "receipt_sha256": hashlib.sha256(receipt_body).hexdigest(), "report_sha256": report_digest,
              "counts": counts, "recovered_from_rejected_envelope": recovering}
    evidence = artifact.with_suffix(".evidence.json")
    if evidence.resolve() != evidence or (evidence.exists() and json.loads(_read(evidence)) != result):
        raise ConfigurationError(ERROR)
    _atomic_json(evidence, result)
    return result


def _make_pairs(work: Path, task: dict) -> None:
    from game_l10n.bound_webp_encode import _check_pixels, _dimensions, _png, _read_webp
    from game_l10n.data_plane_task import _atomic_json
    from game_l10n.errors import ConfigurationError
    from game_l10n.image_region_pixels import _MAX_FILE

    output = work / task["webp_visual_review_png_root"]
    if output.resolve() != output:
        raise ConfigurationError(ERROR)
    output.mkdir(parents=True, exist_ok=False)
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP") if key in os.environ}
    pairs = []
    for asset in task["webp_visual_review"]["assets"]:
        original = _read(Path(asset["original"]), _MAX_FILE)
        edited = _read(work / asset["edited"], _MAX_FILE)
        dimensions = tuple(asset["dimensions"])
        if (hashlib.sha256(original).hexdigest() != asset["original_sha256"]
                or hashlib.sha256(edited).hexdigest() != asset["edited_sha256"] or _dimensions(edited) != dimensions):
            raise ConfigurationError(ERROR)
        before = _read_webp(original, dimensions, env, output)
        after = _read_webp(edited, dimensions, env, output)
        width, height = dimensions
        _check_pixels(before, after, width, [region["bbox"] for region in asset["regions"]])
        ordinal = asset["ordinal"]
        pair = {"ordinal": ordinal}
        stride = width * 4
        for variant, pixels in (("source", before), ("edited", after)):
            scanlines = bytearray((stride + 1) * height)
            for row in range(height):
                offset = row * (stride + 1) + 1
                scanlines[offset:offset + stride] = pixels[row * stride:(row + 1) * stride]
            body = _png(scanlines, dimensions)
            (output / f"asset-{ordinal:04d}.{variant}.png").write_bytes(body)
            pair[variant + "_sha256"] = hashlib.sha256(body).hexdigest()
        pairs.append(pair)
    _atomic_json(output / "pairs.json", {"task_id": task["task_id"], "pairs": pairs})
    print(json.dumps({"pairs": len(pairs), "preview_dir": output.relative_to(work).as_posix()}))


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from game_l10n.data_plane_task import _load_bound_data_plane_relay
    from game_l10n.errors import ConfigurationError

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--relay", type=Path, required=True)
    args = parser.parse_args()
    work = args.workspace.resolve()
    _, task = _load_bound_data_plane_relay(work, (work / args.relay).resolve())
    script = Path(__file__).resolve()
    if (os.name != "nt" or not task.get("webp_visual_review")
            or not any(row["argv"] == [str(script), *sys.argv[1:]]
                       and row["sha256"] == hashlib.sha256(script.read_bytes()).hexdigest()
                       for row in task.get("offline_processing", []))):
        raise SystemExit(ERROR)
    try:
        _make_pairs(work, task)
    except (ConfigurationError, OSError, ValueError, KeyError, TypeError):
        raise SystemExit(ERROR) from None


if __name__ == "__main__":
    main()
