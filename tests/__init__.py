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
    manifest = manifest or _result_manifest(path, copied)
    if manifest is not None:
        from game_l10n.native_pass import _bound_manifest_content_source_policy
        policy = _bound_manifest_content_source_policy(manifest)
        route_binding = manifest.get("route_binding") or {}
        bound_route = route_binding.get("provider") or route or (
            "native-luna"
            if manifest.get("pass_kind", "translation") == "translation"
            else "native-sol"
        )
        bound_model = route_binding.get("model") or model or bound_route
    else:
        bound_route = route or next(
            (
                str(row["provider"])
                for row in copied
                if isinstance(row.get("provider"), str)
                and row["provider"] != "native-control"
            ),
            "native-luna" if task_kind == "translation" else "native-sol",
        )
        bound_model = model or bound_route
        policy = content_source_policy(
            path.parent,
            task_kind=task_kind,
            route=f"native-pass:{'review' if task_kind in {'triage', 'strong_review'} else task_kind}",
        )
    if not isinstance(policy, dict):
        return copied
    for row in provenance_rows:
        row_route = (
            str(row.get("provider") or bound_route)
            if manifest is None
            else str(bound_route)
        )
        row_model = (
            str(model or row_route)
            if manifest is None
            else str(bound_model)
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
