from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from unreal_profile import (
    SUPPORTED_ENGINE_VERSION,
    classify_manifest_paths,
    detect_engine_version,
    extract_segments,
    find_game_layout,
    localization_paths,
    parse_manifest,
    require_external_tool,
    run_logged,
    select_source_locres,
    target_locale_exists,
    write_json,
)


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: unpack.py <game-root> <output-directory>")

    game_root = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    if not game_root.is_dir():
        raise RuntimeError("game_root_missing")
    if output == game_root or game_root in output.parents:
        raise RuntimeError("output_must_not_be_inside_game_root")

    retoc = require_external_tool("GAME_L10N_RETOC", "retoc")
    repak = require_external_tool("GAME_L10N_REPAK", "repak")
    layout = find_game_layout(game_root)
    engine_version = detect_engine_version(layout["shipping_exe"])
    if engine_version != SUPPORTED_ENGINE_VERSION:
        raise RuntimeError(f"unsupported_engine_version:{engine_version}")

    source_locale = os.environ.get("GAME_L10N_SOURCE_LOCALE", "en")
    target_locale = os.environ.get("GAME_L10N_TARGET_LOCALE", "ko")
    manifest_paths = parse_manifest(layout["manifest"])
    if target_locale_exists(manifest_paths, target_locale):
        output.mkdir(parents=True, exist_ok=True)
        summary = {
            "schema": "parr0t.unreal-5.4.extract-summary/v1",
            "engine": "unreal",
            "engine_version": engine_version,
            "target_locale_existing": True,
            "static_complete": False,
        }
        write_json(output / "summary.json", summary)
        print(json.dumps(summary, sort_keys=True))
        return 3

    output.mkdir(parents=True, exist_ok=True)
    logs = output / "logs"
    run_logged([str(retoc), "info", str(layout["utoc"])], logs / "retoc-info.log")
    run_logged([str(retoc), "verify", str(layout["utoc"])], logs / "retoc-verify.log")

    loose = output / "loose"
    run_logged(
        [
            str(repak),
            "unpack",
            str(layout["pak"]),
            "--output",
            str(loose),
            "--force",
            "--quiet",
        ],
        logs / "repak-unpack.log",
    )
    source_locres = select_source_locres(loose, source_locale)
    source_dir = output / "source-localization"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_copy = source_dir / "source.locres"
    shutil.copy2(source_locres, source_copy)
    locmeta_candidates = sorted(source_locres.parents[1].glob("*.locmeta"))
    if len(locmeta_candidates) != 1:
        raise RuntimeError(f"source_locmeta_ambiguous:{len(locmeta_candidates)}")
    shutil.copy2(locmeta_candidates[0], source_dir / "source.locmeta")

    segment_count, locres_meta = extract_segments(source_copy, output / "segments.jsonl")
    classified = classify_manifest_paths(manifest_paths, layout["project_name"])
    write_json(output / "targets.json", classified)
    write_json(
        output / "source-map.json",
        {
            "schema": "parr0t.unreal-5.4.source-map/v1",
            "engine_version": engine_version,
            "project_name": layout["project_name"],
            "source_locale": source_locale,
            "source_locres_relative": source_locres.relative_to(loose).as_posix(),
            **locres_meta,
        },
    )
    summary = {
        "schema": "parr0t.unreal-5.4.extract-summary/v1",
        "engine": "unreal",
        "engine_version": engine_version,
        "package_format": "iostore-with-pak-companion",
        "container_verified": True,
        "target_locale_existing": False,
        "segments": segment_count,
        "localization_resource_count": len(localization_paths(manifest_paths)),
        "static_complete": False,
        "declared_exclusions": [
            "non_locres_user_visible_assets",
            "runtime_generated_text",
        ],
    }
    write_json(output / "summary.json", summary)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
