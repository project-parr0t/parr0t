"""Audit pinned branch snapshots without updating branches or repeating valid checks."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "parr0t.branch-audit.v1"


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, shell=False)
    if result.returncode:
        raise ValueError(f"Git audit failed: {args[0]}")
    return result.stdout


def tree(repo: Path, commit: str) -> dict[str, str]:
    result = {}
    for entry in git(repo, "ls-tree", "-r", "-z", commit).decode("utf-8").split("\0"):
        if entry:
            identity, path = entry.split("\t", 1)
            result[path] = identity
    return result


def plan_branches(repo: Path, main_ref: str, refs: list[str] | None = None) -> list[dict]:
    # Resolve all refs once. Every later operation uses immutable commit IDs.
    inventory = {}
    output = git(repo, "for-each-ref", "--format=%(refname)%09%(objectname)%09%(symref)",
                 "refs/heads", "refs/remotes").decode()
    for line in output.splitlines():
        name, commit, symbolic = line.split("\t")
        if not symbolic:
            inventory[name] = commit
    main_commit = git(repo, "rev-parse", "--verify", f"{main_ref}^{{commit}}").decode().strip()
    selected = refs if refs is not None else [name for name in inventory if name.startswith("refs/remotes/origin/")]
    pinned = [(main_ref, main_commit)]
    for ref in selected:
        commit = inventory.get(ref) or git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
        if commit != main_commit or ref not in {main_ref, "refs/remotes/origin/main"}:
            pinned.append((ref, commit))
    trees = {}
    def read_tree(commit: str) -> dict[str, str]:
        if commit not in trees:
            trees[commit] = tree(repo, commit)
        return trees[commit]
    main_files = read_tree(main_commit)
    plans = []
    for ref, commit in pinned:
        sdk = ref.rsplit("/", 1)[-1].startswith("sdk-")
        files = read_tree(commit)
        if commit == main_commit:
            base, overrides, additions = main_commit, [], []
            owned = main_files.keys()
        else:
            base = git(repo, "merge-base", main_commit, commit).decode().strip()
            base_files = read_tree(base)
            changed = {path for path in files.keys() | base_files.keys() if files.get(path) != base_files.get(path)}
            owned = main_files.keys() | base_files.keys()
            overrides = sorted(path for path in changed & owned if files.get(path) != main_files.get(path))
            additions = sorted(changed - owned)
        overlay = sdk and not overrides
        effective = dict(main_files if overlay else files)
        if overlay:
            effective.update({path: files[path] for path in additions})
        plans.append({
            "ref": ref, "commit": commit, "main_commit": main_commit, "merge_base": base,
            "sdk": sdk, "core_overrides": overrides,
            "core_current": all(files.get(path) == main_files.get(path) for path in owned),
            "snapshot_kind": "latest_core_with_sdk" if overlay else "branch_tree",
            "source_commit": main_commit if overlay else commit,
            "additions": additions if overlay else [],
            "adapter_tests": sorted(path for path in files if path.startswith("tests/")
                                    and PurePosixPath(path).name.startswith("test")
                                    and path.endswith(".py") and path not in main_files),
            "content_digest": digest(effective),
            "main_content_digest": digest(main_files),
        })
    return plans


def environment_digest(python: str, environment_key: str) -> str:
    probe = """import importlib.metadata as m,json,platform,sys
print(json.dumps([sys.version,sys.executable,platform.platform(),sorted((d.metadata.get('Name',''),d.version,str(d.locate_file(''))) for d in m.distributions())]))"""
    result = subprocess.run([python, "-c", probe], capture_output=True, check=True, text=True)
    return digest([result.stdout, environment_key, {key: os.environ.get(key, "") for key in
                  ("PATH", "PYTHONPATH", "CSC", "CECIL", "GAME_L10N_CSC", "GAME_L10N_CECIL")}])


def validator_digest() -> str:
    return digest({path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in (
        "scripts/check_branches.py", "scripts/check_repo_privacy.py", "scripts/check_engine_faq.py",
    )})


def extract_snapshot(repo: Path, commit: str, destination: Path, paths: list[str] | None = None) -> None:
    archive = git(repo, "archive", "--format=zip", commit)
    selected = set(paths) if paths is not None else None
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        for item in source.infolist():
            if item.is_dir() or (selected is not None and item.filename not in selected):
                continue
            path = PurePosixPath(item.filename)
            if path.is_absolute() or any(part in {"..", ".git"} or ":" in part or "\\" in part for part in path.parts):
                raise ValueError("Unsafe archive path")
            mode = item.external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise ValueError("Symlinks require manual branch validation")
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read(item))


def initialize_snapshot(snapshot: Path) -> None:
    # Activation tests require a clean Git runtime. This metadata belongs only
    # to the disposable snapshot, never to an audited branch or user checkout.
    git(snapshot, "init", "-b", "main")
    git(snapshot, "-c", "core.autocrlf=false", "add", "--force", "--all")
    git(snapshot, "-c", "core.hooksPath=", "-c", "commit.gpgsign=false",
        "-c", "user.name=Branch Audit Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "Synthetic branch audit snapshot")


def run_checks(snapshot: Path, python: str, sdk: bool, log: Path) -> dict:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(snapshot / "src") + os.pathsep + environment.get("PYTHONPATH", "")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # Use the trusted current validators, including on branches that edited them.
    privacy = ("import runpy,sys; from pathlib import Path; "
               "scope=runpy.run_path(sys.argv[1]); fn=scope['main']; "
               "fn.__globals__['ROOT']=Path(sys.argv[2]); sys.exit(fn())")
    commands = [
        ("privacy", [python, "-c", privacy, str(ROOT / "scripts/check_repo_privacy.py"), str(snapshot)]),
    ]
    if sdk:
        commands.append(("engine_faq", [python, str(ROOT / "scripts/check_engine_faq.py"), str(snapshot / "docs/engine-faq.md")]))
    commands.append(("tests", [python, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"]))
    checks, transcript = {}, []
    for name, command in commands:
        started = time.monotonic()
        result = subprocess.run(command, cwd=snapshot, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False, timeout=600)
        output = result.stdout + result.stderr
        checks[name] = {"passed": result.returncode == 0, "seconds": round(time.monotonic() - started, 3)}
        if name == "tests":
            count = re.search(r"Ran (\d+) tests? in", output)
            skipped = re.search(r"^OK \(skipped=(\d+)", output, re.MULTILINE)
            checks[name].update({"count": int(count[1]) if count else 0, "skipped": int(skipped[1]) if skipped else 0})
            if not count or int(count[1]) == 0:
                checks[name]["passed"] = False
        transcript.append(f"[{name}]\n{output}")
    log.write_text("\n".join(transcript), encoding="utf-8")
    return {"passed": all(check["passed"] for check in checks.values()), "checks": checks, "log": str(log)}


def audit_branches(repo: Path, state: Path, *, main_ref: str = "origin/main", refs: list[str] | None = None,
                   python: str = sys.executable, environment_key: str = "", jobs: int = 2, force: bool = False,
                   pinned_plans: list[dict] | None = None) -> dict:
    repo, state = repo.resolve(), state.resolve()
    if state.is_relative_to(repo):
        raise ValueError("Audit state must be outside the repository")
    state.mkdir(parents=True, exist_ok=True)
    cache_path = state / "cache.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(cache, dict) or cache.get("schema") != SCHEMA or not isinstance(cache.get("results"), dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    cached = cache.get("results", {})
    validators, environment = validator_digest(), environment_digest(python, environment_key)
    plans = plan_branches(repo, main_ref, refs) if pinned_plans is None else pinned_plans
    pending, results = {}, {}
    for plan in plans:
        key = digest([SCHEMA, plan["content_digest"], plan["sdk"], validators, environment])
        plan["validation_key"] = key
        hit = cached.get(key)
        required = {"privacy", "tests"} | ({"engine_faq"} if plan["sdk"] else set())
        checks = hit.get("checks", {}) if isinstance(hit, dict) else {}
        valid = (
            isinstance(hit, dict) and hit.get("passed") is True
            and isinstance(checks, dict) and set(checks) == required
            and all(isinstance(check, dict) and check.get("passed") is True for check in checks.values())
            and type(checks.get("tests", {}).get("count")) is int
            and checks["tests"]["count"] > 0
            and type(checks["tests"].get("skipped")) is int
            and checks["tests"]["skipped"] >= 0
        )
        if not force and valid:
            results[key] = {**hit, "cached": True}
        else:
            pending.setdefault(key, plan)
    def execute(item: tuple[str, dict]) -> tuple[str, dict]:
        key, plan = item
        snapshot = Path(tempfile.mkdtemp(prefix=key[:12] + "-", dir=state))
        try:
            extract_snapshot(repo, plan["source_commit"], snapshot)
            if plan["additions"]:
                extract_snapshot(repo, plan["commit"], snapshot, plan["additions"])
            initialize_snapshot(snapshot)
            result = run_checks(snapshot, python, plan["sdk"], state / f"{key}.log")
            if plan["sdk"] and not plan["adapter_tests"]:
                result.update({"passed": False, "error": "E_SDK_TESTS_MISSING"})
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            result = {"passed": False, "checks": {}, "error": type(error).__name__}
        return key, {**result, "cached": False}
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
        for key, result in executor.map(execute, pending.items()):
            results[key] = result
    rows = []
    for plan in plans:
        result = results[plan["validation_key"]]
        skipped = result.get("checks", {}).get("tests", {}).get("skipped", 0)
        if plan["core_overrides"]:
            compatibility = "core_diverged"
        elif not result["passed"] or skipped:
            compatibility = "unverified"
        elif plan["merge_base"] == plan["main_commit"]:
            compatibility = "up_to_date"
        elif plan["core_current"]:
            compatibility = "up_to_date_compatible"
        else:
            compatibility = "update_ready"
        action = ("skip_update" if compatibility in {"up_to_date", "up_to_date_compatible"}
                  else "update_required" if compatibility == "update_ready" else "manual_review")
        rows.append({**plan, **result, "compatibility": compatibility, "update_action": action})
    cache_path.write_text(json.dumps({"schema": SCHEMA, "results": {**cached, **{key: {k: v for k, v in value.items() if k != "cached"} for key, value in results.items()}}}, indent=2) + "\n", encoding="utf-8")
    report = {"schema": SCHEMA, "main_commit": plans[0]["main_commit"], "branches": rows,
              "executed": len(pending), "reused": sum(row["cached"] for row in rows),
              "checks_passed": all(row["passed"] for row in rows),
              "all_compatible": all(row["update_action"] == "skip_update" for row in rows)}
    (state / "last-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--main-ref", default="origin/main")
    parser.add_argument("--ref", action="append", help="Repeat for a subset; default: all origin tracking branches")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--environment-key", default="", help="Invalidate when external tools or editable dependencies change")
    parser.add_argument("--jobs", type=int, choices=range(1, 9), default=2)
    parser.add_argument("--force", action="store_true", help="Ignore previous passing validation")
    args = parser.parse_args(argv)
    report = audit_branches(args.repo, args.state_dir, main_ref=args.main_ref, refs=args.ref,
                            python=args.python, environment_key=args.environment_key, jobs=args.jobs, force=args.force)
    print(json.dumps(report, indent=2))
    return 0 if report["checks_passed"] and report["all_compatible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
