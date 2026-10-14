from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SUPPORTED_ENGINE_VERSION = "5.4"
TOOLS_ROOT = Path(__file__).resolve().parent
WORKSPACE = Path(os.environ.get("GAME_L10N_WORKDIR", TOOLS_ROOT.parent)).resolve()
VENDOR_ROOT = TOOLS_ROOT / "vendor"


def add_vendor_path() -> None:
    if not VENDOR_ROOT.is_dir():
        raise RuntimeError("pylocres_vendor_missing")
    value = str(VENDOR_ROOT)
    if value not in sys.path:
        sys.path.insert(0, value)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    return count


def require_external_tool(env_name: str, executable: str) -> Path:
    configured = os.environ.get(env_name)
    resolved = Path(configured).resolve() if configured else None
    if resolved is None:
        found = shutil.which(executable)
        resolved = Path(found).resolve() if found else None
    if resolved is None or not resolved.is_file():
        raise RuntimeError(f"external_tool_missing:{env_name}")
    return resolved


def run_logged(command: list[str], log_path: Path) -> str:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        command,
        cwd=WORKSPACE,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    log_path.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode:
        raise RuntimeError(
            f"external_tool_failed:{Path(command[0]).name}:{completed.returncode}"
        )
    return completed.stdout


def _select_candidate(paths: list[Path], env_name: str, error_name: str) -> Path:
    configured = os.environ.get(env_name)
    if configured:
        selected = Path(configured).resolve()
        if selected not in [path.resolve() for path in paths]:
            raise RuntimeError(f"configured_{error_name}_not_discovered")
        return selected
    if len(paths) != 1:
        raise RuntimeError(f"{error_name}_ambiguous:{len(paths)}")
    return paths[0]


def find_game_layout(game_root: Path) -> dict[str, Any]:
    manifests = sorted(game_root.glob("Manifest_UFSFiles_*.txt"))
    shipping = sorted(game_root.glob("*/Binaries/Win64/*-Win64-Shipping.exe"))
    if len(manifests) != 1 or len(shipping) != 1:
        raise RuntimeError("unsupported_game_layout")
    project_root = shipping[0].parents[2]
    paks_root = project_root / "Content" / "Paks"
    utocs = sorted(
        path for path in paks_root.glob("*.utoc") if path.name.casefold() != "global.utoc"
    )
    paks = sorted(paks_root.glob("*.pak"))
    return {
        "manifest": manifests[0],
        "shipping_exe": shipping[0],
        "project_name": project_root.name,
        "paks_root": paks_root,
        "utoc": _select_candidate(utocs, "GAME_L10N_UNREAL_UTOC", "utoc"),
        "pak": _select_candidate(paks, "GAME_L10N_UNREAL_PAK", "pak"),
    }


def detect_engine_version(shipping_exe: Path) -> str:
    match = re.search(rb"\+\+UE5\+Release-(\d+\.\d+)", shipping_exe.read_bytes())
    if not match:
        raise RuntimeError("engine_version_not_detected")
    return match.group(1).decode("ascii")


def parse_manifest(path: Path) -> list[str]:
    values: list[str] = []
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for raw in handle:
            value = raw.rstrip("\r\n").split("\t", 1)[0].replace("\\", "/")
            if value:
                values.append(value)
    return values


def localization_paths(paths: list[str]) -> list[str]:
    return sorted(
        path
        for path in paths
        if "/content/localization/" in path.casefold()
        and Path(path).suffix.casefold() in {".locres", ".locmeta"}
    )


def target_locale_exists(paths: list[str], locale: str) -> bool:
    aliases = {locale.casefold()}
    if locale.casefold() == "ko":
        aliases.update({"ko-kr", "korean"})
    for path in localization_paths(paths):
        parts = {part.casefold() for part in PurePosixPath(path).parts}
        if parts.intersection(aliases):
            return True
    return False


def select_source_locres(loose_root: Path, locale: str) -> Path:
    configured = os.environ.get("GAME_L10N_UNREAL_LOCRES")
    candidates = sorted(loose_root.rglob("*.locres"))
    if configured:
        selected = (loose_root / require_safe_relative(configured, "source_locres")).resolve()
        if selected not in [path.resolve() for path in candidates]:
            raise RuntimeError("configured_source_locres_not_discovered")
        return selected
    selected = [
        path
        for path in candidates
        if "localization" in {part.casefold() for part in path.parts}
        and locale.casefold() in {part.casefold() for part in path.parts}
        and "engine" not in {part.casefold() for part in path.parts}
    ]
    if len(selected) != 1:
        raise RuntimeError(f"source_locres_ambiguous:{len(selected)}")
    return selected[0]


def stable_id(namespace: str, key: str) -> str:
    digest = hashlib.sha256((namespace + "\0" + key).encode("utf-8")).hexdigest()[:24]
    return f"ue-locres-{digest}"


def load_locres(path: Path):
    add_vendor_path()
    from pylocres import LocresFile

    value = LocresFile()
    value.read(path)
    return value


def load_locmeta(path: Path):
    add_vendor_path()
    from pylocres import LocmetaFile

    value = LocmetaFile()
    value.read(str(path))
    value.reader.close()
    return value


def extract_segments(source_locres: Path, output_path: Path) -> tuple[int, dict[str, Any]]:
    locres = load_locres(source_locres)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    order = 0
    for namespace in locres:
        for entry in namespace:
            order += 1
            target_id = stable_id(namespace.name, entry.key)
            if target_id in seen:
                raise RuntimeError("stable_id_collision")
            seen.add(target_id)
            rows.append(
                {
                    "id": target_id,
                    "source": entry.translation,
                    "speaker": "",
                    "scene": namespace.name,
                    "metadata": {
                        "format": "unreal_locres",
                        "namespace": namespace.name,
                        "key": entry.key,
                        "source_hash": entry.hash,
                        "source_order": order,
                        "locres_version": int(locres.version),
                    },
                }
            )
    return write_jsonl(output_path, rows), {
        "locres_version": int(locres.version),
        "namespace_count": len(locres),
    }


def load_reviewed_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.casefold() == ".jsonl":
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "segments"):
            if isinstance(payload.get(key), list):
                return payload[key]
    raise RuntimeError("unsupported_reviewed_payload")


def apply_reviewed_results(locres: Any, reviewed_path: Path) -> tuple[int, int]:
    entries = {
        stable_id(namespace.name, entry.key): entry
        for namespace in locres
        for entry in namespace
    }
    rows = load_reviewed_rows(reviewed_path)
    seen: set[str] = set()
    translated = 0
    preserved = 0
    for row in rows:
        target_id = str(row.get("id") or row.get("segment_id") or "")
        if not target_id or target_id in seen or target_id not in entries:
            raise RuntimeError("reviewed_id_invalid")
        status = str(row.get("status", ""))
        if status not in {"reviewed", "source_preserved"}:
            raise RuntimeError(f"reviewed_status_invalid:{status}")
        text = row.get("text")
        if not isinstance(text, str):
            raise RuntimeError("reviewed_text_missing")
        entries[target_id].translation = text
        seen.add(target_id)
        if status == "source_preserved":
            preserved += 1
        else:
            translated += 1
    if seen != set(entries):
        raise RuntimeError(f"reviewed_set_incomplete:{len(entries) - len(seen)}")
    return translated, preserved


def classify_manifest_paths(paths: list[str], project_name: str) -> dict[str, list[str]]:
    prefix = f"{project_name}/Content/".casefold()
    content = [path for path in paths if path.casefold().startswith(prefix)]
    font_re = re.compile(r"(?i)(?:/font/|font|\.ttf$|\.otf$|\.ufont$)")
    image_re = re.compile(r"(?i)(title|tutorial|warning|credits|menu|text)")
    return {
        "localization_resources": localization_paths(paths),
        "font_assets": sorted(path for path in content if font_re.search(path)),
        "image_text_candidates": sorted(
            path
            for path in content
            if image_re.search(Path(path).stem)
            and Path(path).suffix.casefold() in {".uasset", ".uexp", ".ubulk", ".png"}
        ),
    }


def require_workspace_path(path: Path, field: str) -> Path:
    resolved = path.resolve()
    if resolved != WORKSPACE and WORKSPACE not in resolved.parents:
        raise RuntimeError(f"{field}_outside_workspace")
    return resolved


def require_safe_component(value: str, field: str) -> str:
    if not value or value in {".", ".."} or any(char in value for char in ("/", "\\", ":")):
        raise RuntimeError(f"{field}_invalid")
    if any(ord(char) < 32 for char in value):
        raise RuntimeError(f"{field}_invalid")
    return value


def require_safe_relative(value: str, field: str) -> str:
    normalized = value.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise RuntimeError(f"{field}_invalid")
    if ":" in normalized or any(ord(char) < 32 for char in normalized):
        raise RuntimeError(f"{field}_invalid")
    return pure.as_posix()


def copy_overlay(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise RuntimeError("overlay_source_missing")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def same_file_content(left: Path, right: Path) -> bool:
    return left.stat().st_size == right.stat().st_size and file_sha256(left) == file_sha256(right)
