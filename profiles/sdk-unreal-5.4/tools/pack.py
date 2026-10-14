from __future__ import annotations

import json
import sys
from pathlib import Path

from unreal_profile import (
    apply_reviewed_results,
    copy_overlay,
    file_sha256,
    load_locmeta,
    load_locres,
    require_external_tool,
    require_safe_component,
    require_safe_relative,
    require_workspace_path,
    run_logged,
    same_file_content,
    write_json,
)


SCHEMA = "parr0t.unreal-5.4.pack/v1"


def _load_request(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise RuntimeError("unsupported_pack_request")
    return payload


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: pack.py <request-json> <output-directory>")

    request_path = require_workspace_path(Path(sys.argv[1]).resolve(), "request")
    output = require_workspace_path(Path(sys.argv[2]).resolve(), "output")
    request = _load_request(request_path)

    extraction_root = require_workspace_path(
        Path(str(request["extraction_root"])).resolve(), "extraction_root"
    )
    reviewed_path = require_workspace_path(
        Path(str(request["reviewed_path"])).resolve(), "reviewed_path"
    )
    project_name = require_safe_component(str(request["project_name"]), "project_name")
    localization_target = require_safe_component(
        str(request.get("localization_target", "Game")), "localization_target"
    )
    target_locale = require_safe_component(str(request["target_locale"]), "target_locale")
    pak_name = require_safe_component(str(request["pak_name"]), "pak_name")
    if not pak_name.casefold().endswith("_p.pak"):
        raise RuntimeError("pak_name_must_be_additive_patch")
    path_hash_seed = str(request["path_hash_seed"])
    if not path_hash_seed.isdecimal():
        raise RuntimeError("path_hash_seed_invalid")
    compression = str(request.get("compression", "Zlib"))
    if compression not in {"Zlib", "None"}:
        raise RuntimeError("compression_invalid")

    source_locres_path = extraction_root / "source-localization" / "source.locres"
    source_locmeta_path = extraction_root / "source-localization" / "source.locmeta"
    if not source_locres_path.is_file() or not source_locmeta_path.is_file():
        raise RuntimeError("source_localization_missing")

    locres = load_locres(source_locres_path)
    translated, preserved = apply_reviewed_results(locres, reviewed_path)
    locmeta = load_locmeta(source_locmeta_path)
    cultures = list(locmeta.compiled_cultures or [])
    if target_locale not in cultures:
        cultures.append(target_locale)
    locmeta.compiled_cultures = cultures

    if output.exists() and any(output.iterdir()):
        raise RuntimeError("output_directory_must_be_empty")
    output.mkdir(parents=True, exist_ok=True)
    staging = output / "staging"
    locale_root = staging / project_name / "Content" / "Localization" / localization_target
    culture_root = locale_root / target_locale
    culture_root.mkdir(parents=True, exist_ok=True)
    locres.write(culture_root / f"{localization_target}.locres")
    locmeta.write(str(locale_root / f"{localization_target}.locmeta"))
    locmeta.writer.close()

    overlays = request.get("overlays", [])
    if not isinstance(overlays, list):
        raise RuntimeError("overlays_must_be_list")
    destinations: set[str] = set()
    for item in overlays:
        if not isinstance(item, dict):
            raise RuntimeError("overlay_entry_invalid")
        source = require_workspace_path(Path(str(item["source"])).resolve(), "overlay_source")
        destination = require_safe_relative(str(item["destination"]), "overlay_destination")
        folded = destination.casefold()
        if folded in destinations:
            raise RuntimeError("overlay_destination_duplicate")
        destinations.add(folded)
        copy_overlay(source, staging / Path(destination))

    repak = require_external_tool("GAME_L10N_REPAK", "repak")
    patch_pak = output / pak_name
    command = [str(repak), "pack", str(staging), str(patch_pak), "--version", "V11"]
    if compression != "None":
        command += ["--compression", compression]
    command += ["--path-hash-seed", path_hash_seed, "--quiet"]
    run_logged(command, output / "repak-pack.log")

    roundtrip = output / "roundtrip"
    run_logged(
        [
            str(repak),
            "unpack",
            str(patch_pak),
            "--output",
            str(roundtrip),
            "--force",
            "--quiet",
        ],
        output / "repak-roundtrip.log",
    )
    staged_files = sorted(path for path in staging.rglob("*") if path.is_file())
    roundtrip_files = sorted(path for path in roundtrip.rglob("*") if path.is_file())
    if len(staged_files) != len(roundtrip_files):
        raise RuntimeError("pak_roundtrip_count_mismatch")
    for staged in staged_files:
        unpacked = roundtrip / staged.relative_to(staging)
        if not unpacked.is_file() or not same_file_content(staged, unpacked):
            raise RuntimeError("pak_roundtrip_content_mismatch")

    report = {
        "schema": "parr0t.unreal-5.4.pack-report/v1",
        "engine": "unreal",
        "engine_version": "5.4",
        "package_format": "pak-v11-additive",
        "pak_name": pak_name,
        "pak_bytes": patch_pak.stat().st_size,
        "pak_sha256": file_sha256(patch_pak),
        "roundtrip_files": len(staged_files),
        "translated": translated,
        "source_preserved": preserved,
        "overlay_files": len(overlays),
        "target_locale": target_locale,
    }
    write_json(output / "pack-report.json", report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
