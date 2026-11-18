from __future__ import annotations

import json
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


def context_result_provenance(claim: dict) -> dict:
    from game_l10n.content_source_policy import direct_model_provenance

    binding = claim["binding"]
    provider = str(claim["provider"])
    return direct_model_provenance(
        claim["content_source_policy"],
        str(binding.get("runtime_model", provider)),
        route=str(binding.get("semantic_route", provider)),
    )


def _result_manifest(path: Path, rows: list[dict]) -> dict | None:
    candidates: list[Path] = []
    parent = path.parent
    if parent.name == "results":
        candidates.append(parent.parent / "tasks" / "manifest.json")
    if parent.name.endswith("-results"):
        candidates.append(
            parent.with_name(parent.name.removesuffix("-results")) / "manifest.json"
        )
    for ancestor in list(path.parents)[:5]:
        candidates.extend(
            [
                ancestor / "manifest.json",
                ancestor / "tasks" / "manifest.json",
            ]
        )
    target_ids = [row.get("target_id") for row in rows]
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        try:
            manifest = json.loads(candidate.read_text(encoding="utf-8"))
            tasks = [
                task
                for task in manifest["tasks"]
                if task.get("file") == path.name
            ]
            if not tasks:
                continue
            task_rows = [
                json.loads(line)
                for line in (candidate.parent / path.name)
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            task_target_ids = [row.get("target_id") for row in task_rows]
            target_set = set(target_ids)
            if (
                len(target_set) == len(target_ids)
                and target_set <= set(task_target_ids)
                and [target_id for target_id in task_target_ids if target_id in target_set]
                == target_ids
            ):
                return manifest
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            continue
    return None


def _semantic_fixture_bindings(path: Path) -> tuple[Path, dict[str, str]]:
    from game_l10n.model_policy import native_role_bindings
    from game_l10n.native_runtime import RUNTIME_PATH, native_runtime

    for ancestor in path.resolve().parents:
        if not (
            (ancestor / "game-l10n.json").is_file()
            or (ancestor / RUNTIME_PATH).is_file()
        ):
            continue
        models: dict[str, str] = {}
        if (ancestor / "game-l10n.json").is_file():
            config = json.loads((ancestor / "game-l10n.json").read_text(encoding="utf-8"))
            models.update({
                provider["name"]: provider["model"]
                for provider in config.get("providers", [])
                if isinstance(provider.get("model"), str) and provider["model"]
            })
            models.update(native_role_bindings(config))
        runtime = native_runtime(ancestor)
        if runtime is not None:
            models.update(runtime["models"])
        return ancestor, models
    return path.parent, {}


def semantic_result_rows(
    path: Path,
    rows: list[dict],
    *,
    manifest: dict | None = None,
    task_kind: str = "translation",
    route: str | None = None,
    model: str | None = None,
) -> list[dict]:
    from game_l10n.content_source_policy import (
        content_source_policy,
        direct_model_provenance,
    )

    copied = [dict(row) for row in rows]
    provenance_rows = [
        row
        for row in copied
        if {"target_id", "status", "text"} <= set(row)
        and "content_provenance" not in row
        and not (
            row.get("status") == "error"
            and row.get("provider") == "native-control"
        )
    ]
    if not provenance_rows:
        return copied
    manifest = manifest if manifest is not None else _result_manifest(path, copied)
    if manifest is not None:
        from game_l10n.native_pass import _bound_manifest_content_source_policy
        policy = _bound_manifest_content_source_policy(manifest)
    else:
        policy = None
    if manifest is not None and not isinstance(policy, dict):
        return copied
    work_dir, configured_models = _semantic_fixture_bindings(path)
    if manifest is None:
        policy = content_source_policy(
            work_dir,
            task_kind=task_kind,
            route=f"native-pass:{'review' if task_kind in {'triage', 'strong_review'} else task_kind}",
        )
    route_binding = (manifest or {}).get("route_binding") or {}
    for row in provenance_rows:
        row_route = route_binding.get("provider") or route or row.get("provider")
        if not isinstance(row_route, str) or not row_route or row_route == "native-control":
            raise ValueError(
                "Semantic result fixture requires an explicit route or manifest route binding"
            )
        row_model = route_binding.get("model") or model or configured_models.get(row_route)
        if not isinstance(row_model, str) or not row_model:
            raise ValueError(
                f"Semantic result fixture route {row_route!r} requires an exact model "
                "from its manifest, runtime, configuration, or explicit model= binding"
            )
        row.setdefault("provider", row_route)
        row["content_provenance"] = direct_model_provenance(
            policy, row_model, route=row_route
        )
    return copied


def write_semantic_result_rows(
    path: Path,
    rows: list[dict],
    **bindings: object,
) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)
    bound = semantic_result_rows(path, rows, **bindings)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in bound),
        encoding="utf-8",
    )
