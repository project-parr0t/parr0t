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


def hash_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def dependency_tree_digest(target: Path) -> str:
    """Bind validation evidence to every installed dependency byte."""
    ignored = {".parr0t-dependencies.json", ".parr0t-dependencies.failure.json"}
    records = []
    for parent, directories, files in os.walk(target, followlinks=False):
        parent_path = Path(parent)
        for name in [*directories, *files]:
            path = parent_path / name
            if path.is_symlink():
                raise ValueError("E_BRANCH_DEPENDENCY_SYMLINK")
        for name in files:
            path = parent_path / name
            relative = path.relative_to(target).as_posix()
            if relative in ignored:
                continue
            records.append([relative, path.stat().st_size, hash_file(path)])
    return digest(sorted(records))


def external_environment_digest(names: list[str]) -> tuple[str, list[str]]:
    records, missing = [], []
    for name in names:
        raw = os.environ.get(name, "")
        path = Path(raw)
        if not raw or not path.is_absolute() or not path.is_file():
            missing.append(name)
            continue
        records.append([name, path.stat().st_size, hash_file(path)])
    return (digest(records) if records else "none"), missing


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


def is_validation_requirement(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return name == "requirements.txt" or (
        name.endswith(".txt")
        and (name.startswith("requirements-") or name.endswith("-requirements.txt"))
    )


def validation_environment_files(repo: Path, files: dict[str, str]) -> tuple[list[str], str | None]:
    manifests = [
        path for path in sorted(files)
        if path.startswith("profiles/") and PurePosixPath(path).name == "sdk-validation.json"
    ]
    if not manifests:
        return [], None
    if len(manifests) != 1:
        return [], "E_BRANCH_VALIDATION_MANIFEST_DUPLICATE"
    try:
        manifest = json.loads(git(repo, "cat-file", "blob", files[manifests[0]].rsplit(" ", 1)[-1]))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return [], "E_BRANCH_VALIDATION_MANIFEST_INVALID"
    if not isinstance(manifest, dict) or set(manifest) != {"schema", "required_environment_files"}:
        return [], "E_BRANCH_VALIDATION_MANIFEST_INVALID"
    required = manifest.get("required_environment_files")
    if manifest.get("schema") != "parr0t.sdk-validation.v1" or not isinstance(required, list):
        return [], "E_BRANCH_VALIDATION_MANIFEST_INVALID"
    if not all(isinstance(name, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", name) for name in required):
        return [], "E_BRANCH_VALIDATION_MANIFEST_INVALID"
    if len(required) != len(set(required)):
        return [], "E_BRANCH_VALIDATION_MANIFEST_INVALID"
    return sorted(required), None


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
        validation_requirements = [
            {"path": path, "blob": effective[path].rsplit(" ", 1)[-1]}
            for path in sorted(effective)
            if is_validation_requirement(path)
        ]
        required_environment_files, validation_configuration_error = validation_environment_files(repo, effective)
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
            "validation_requirements": validation_requirements,
            "required_environment_files": required_environment_files,
            "validation_configuration_error": validation_configuration_error,
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


def provision_requirements(repo: Path, state: Path, plan: dict, python: str, *, retry_failed: bool = False) -> tuple[list[Path], str]:
    """Install one branch's declared Python test requirements outside Git."""
    requirements = plan.get("validation_requirements", [])
    if not requirements:
        return [], "none"
    python_identity = subprocess.run(
        [python, "-c", "import json,sys;print(json.dumps([sys.executable,sys.version]))"],
        capture_output=True, check=True, text=True, shell=False,
    ).stdout
    key = digest(["parr0t.branch-dependencies.v1", requirements, python_identity])
    target = state / "dependencies" / key
    marker = target / ".parr0t-dependencies.json"
    failure_marker = state / "dependency-failures" / f"{key}.json"
    try:
        evidence = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        evidence = None
    if isinstance(evidence, dict) and evidence.get("key") == key:
        current_content = dependency_tree_digest(target)
        if evidence.get("content_digest") != current_content:
            raise ValueError("E_BRANCH_DEPENDENCY_STATE_CHANGED")
        return [target], digest([key, current_content])
    if not retry_failed:
        try:
            failure = json.loads(failure_marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            failure = None
        if isinstance(failure, dict) and failure.get("key") == key:
            raise ValueError(failure.get("error", "E_BRANCH_DEPENDENCY_INSTALL_FAILED"))
    if target.exists():
        raise ValueError("E_BRANCH_DEPENDENCY_STATE_UNTRUSTED")

    materialized = state / "requirements" / key
    materialized.mkdir(parents=True, exist_ok=True)
    requirement_paths = []
    for row in requirements:
        destination = materialized.joinpath(*PurePosixPath(row["path"]).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(git(repo, "cat-file", "blob", row["blob"]))
        requirement_paths.append(destination)
    staging_root = state / "dependency-staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=key[:12] + "-", dir=staging_root))
    command = [python, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "--target", str(staging)]
    for requirement in requirement_paths:
        command.extend(["--requirement", str(requirement)])
    environment = os.environ.copy()
    temporary = state / "dependency-tmp" / key
    temporary.mkdir(parents=True, exist_ok=True)
    for variable in ("TMP", "TEMP", "TMPDIR"):
        environment[variable] = str(temporary)
    try:
        result = subprocess.run(
            command, env=environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", shell=False, timeout=300,
        )
    except subprocess.TimeoutExpired:
        failure_marker.parent.mkdir(parents=True, exist_ok=True)
        failure_marker.write_text(json.dumps({
            "schema": "parr0t.branch-dependencies.failure.v1",
            "key": key,
            "error": "E_BRANCH_DEPENDENCY_INSTALL_TIMEOUT",
        }, indent=2) + "\n", encoding="utf-8")
        raise ValueError("E_BRANCH_DEPENDENCY_INSTALL_TIMEOUT") from None
    (state / f"dependency-install-{key}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        failure_marker.parent.mkdir(parents=True, exist_ok=True)
        failure_marker.write_text(json.dumps({
            "schema": "parr0t.branch-dependencies.failure.v1",
            "key": key,
            "error": "E_BRANCH_DEPENDENCY_INSTALL_FAILED",
        }, indent=2) + "\n", encoding="utf-8")
        raise ValueError("E_BRANCH_DEPENDENCY_INSTALL_FAILED")
    content_digest = dependency_tree_digest(staging)
    staging_marker = staging / ".parr0t-dependencies.json"
    staging_marker.write_text(json.dumps({
        "schema": "parr0t.branch-dependencies.v1",
        "key": key,
        "content_digest": content_digest,
        "requirements": requirements,
        "python": python_identity.strip(),
    }, indent=2) + "\n", encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging.replace(target)
    failure_marker.unlink(missing_ok=True)
    return [target], digest([key, content_digest])


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


def run_checks(snapshot: Path, python: str, sdk: bool, log: Path,
               test_paths: list[str] | None = None,
               dependency_paths: list[Path] | None = None) -> dict:
    environment = os.environ.copy()
    python_paths = [str(snapshot / "src"), *(str(path) for path in dependency_paths or [])]
    if environment.get("PYTHONPATH"):
        python_paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(python_paths)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # A Codex/Windows process can inherit an AppContainer TEMP directory that
    # subprocesses are allowed to name but cannot create files in.  Branch
    # tests use TemporaryDirectory extensively, so bind their temporary files
    # to the external audit state instead of the repository or inherited host
    # location.  The per-validation directory also prevents parallel branches
    # from sharing mutable test state.
    # Keep this component short.  Legacy .NET/Win32 tools used by SDK tests can
    # still inherit MAX_PATH limits, and the full 64-character validation key
    # left too little room for their nested temporary outputs.  Twelve hex
    # characters retain per-validation separation while preserving path budget.
    temporary = log.parent / "tmp" / log.stem[:12]
    temporary.mkdir(parents=True, exist_ok=True)
    for variable in ("TMP", "TEMP", "TMPDIR"):
        environment[variable] = str(temporary)
    # Use the trusted current validators, including on branches that edited them.
    privacy = ("import runpy,sys; from pathlib import Path; "
               "scope=runpy.run_path(sys.argv[1]); fn=scope['main']; "
               "fn.__globals__['ROOT']=Path(sys.argv[2]); sys.exit(fn())")
    commands = [
        ("privacy", [python, "-c", privacy, str(ROOT / "scripts/check_repo_privacy.py"), str(snapshot)]),
    ]
    if sdk:
        commands.append(("engine_faq", [python, str(ROOT / "scripts/check_engine_faq.py"), str(snapshot / "docs/engine-faq.md")]))
    test_command = [python, "-m", "unittest", "-v", *test_paths] if test_paths is not None else [
        python, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v",
    ]
    commands.append(("tests", test_command if test_paths != [] else None))
    checks, transcript = {}, []
    for name, command in commands:
        started = time.monotonic()
        if command is None:
            checks[name] = {
                "passed": False,
                "seconds": 0.0,
                "count": 0,
                "skipped": 0,
                "complete": False,
                "scope": "adapter",
                "issue_code": "E_BRANCH_VALIDATION_TESTS_INCOMPLETE",
            }
            transcript.append(f"[{name}]\nNo branch-owned adapter tests were declared.\n")
            continue
        result = subprocess.run(command, cwd=snapshot, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False, timeout=600)
        output = result.stdout + result.stderr
        checks[name] = {"passed": result.returncode == 0, "seconds": round(time.monotonic() - started, 3)}
        if name == "tests":
            count = re.search(r"Ran (\d+) tests? in", output)
            summary = re.search(r"^(?:OK|FAILED)(?: \(([^)]*)\))?$", output, re.MULTILINE)
            skipped = re.search(r"(?:^|, )skipped=(\d+)(?:, |$)", summary[1] or "") if summary else None
            skipped_count = int(skipped[1]) if skipped else 0 if summary else None
            checks[name].update({
                "count": int(count[1]) if count else 0,
                "skipped": skipped_count,
                "complete": bool(count and int(count[1]) > 0 and summary and skipped_count == 0),
                "scope": "adapter" if test_paths is not None else "full",
            })
            if not checks[name]["complete"]:
                checks[name]["passed"] = False
                checks[name]["issue_code"] = (
                    "E_BRANCH_VALIDATION_TESTS_SKIPPED"
                    if isinstance(skipped_count, int) and skipped_count > 0
                    else "E_BRANCH_VALIDATION_TESTS_INCOMPLETE"
                )
        transcript.append(f"[{name}]\n{output}")
    log.write_text("\n".join(transcript), encoding="utf-8")
    return {"passed": all(check["passed"] for check in checks.values()), "checks": checks, "log": str(log)}


def audit_branches(repo: Path, state: Path, *, main_ref: str = "origin/main", refs: list[str] | None = None,
                   python: str = sys.executable, environment_key: str = "", jobs: int = 2, force: bool = False,
                   pinned_plans: list[dict] | None = None,
                   provision_dependencies: bool = False) -> dict:
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
    dependency_bindings: dict[str, tuple[list[Path], str]] = {}
    dependency_errors: dict[str, str] = {}
    for plan in plans:
        external_key, missing_environment_files = external_environment_digest(
            plan.get("required_environment_files", [])
        )
        plan["external_tool_key"] = external_key
        if plan.get("validation_configuration_error"):
            dependency_errors[plan["content_digest"]] = plan["validation_configuration_error"]
            paths, package_key = [], "invalid"
        elif missing_environment_files:
            dependency_errors[plan["content_digest"]] = (
                "E_BRANCH_EXTERNAL_TOOL_MISSING:" + ",".join(missing_environment_files)
            )
            paths, package_key = [], "missing-external-tool"
        elif plan.get("validation_requirements") and not provision_dependencies:
            dependency_errors[plan["content_digest"]] = "E_BRANCH_DEPENDENCIES_NOT_PROVISIONED"
            paths, package_key = [], "not-provisioned"
        elif provision_dependencies and plan.get("validation_requirements"):
            try:
                paths, package_key = provision_requirements(
                    repo, state, plan, python, retry_failed=force,
                )
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                dependency_errors[plan["content_digest"]] = str(error)
                paths, package_key = [], "failed"
        else:
            paths, package_key = [], "ambient"
        plan["dependency_state_digest"] = dependency_tree_digest(paths[0]) if paths else "none"
        dependency_bindings[plan["content_digest"]] = (
            paths,
            digest(["parr0t.branch-validation-environment.v1", package_key, external_key]),
        )
    pending, results = {}, {}
    for plan in plans:
        dependency_paths, dependency_key = dependency_bindings.get(plan["content_digest"], ([], "failed"))
        plan["dependency_mode"] = "isolated" if dependency_paths else "ambient"
        plan["dependency_key"] = dependency_key
        key = digest([SCHEMA, plan["content_digest"], plan["sdk"], validators, environment, dependency_key])
        plan["validation_key"] = key
        if plan["content_digest"] in dependency_errors:
            results[key] = {
                "passed": False,
                "checks": {},
                "error": dependency_errors[plan["content_digest"]],
                "cached": False,
            }
            continue
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
            and checks["tests"]["skipped"] == 0
            and checks["tests"].get("complete") is True
            and "error" not in hit
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
            # The pinned main snapshot proves the shared core once. A clean SDK
            # overlay then runs only its branch-owned tests against that exact
            # validated core. Core-diverged branches remain manual-review
            # targets and retain a full-suite audit.
            test_paths = plan["adapter_tests"] if plan["sdk"] and not plan["core_overrides"] else None
            dependency_paths = dependency_bindings[plan["content_digest"]][0]
            result = run_checks(
                snapshot, python, plan["sdk"], state / f"{key}.log",
                test_paths, dependency_paths,
            )
            if dependency_paths and dependency_tree_digest(dependency_paths[0]) != plan.get("dependency_state_digest"):
                result.update({"passed": False, "error": "E_BRANCH_DEPENDENCY_STATE_CHANGED"})
            current_external_key, missing = external_environment_digest(
                plan.get("required_environment_files", [])
            )
            if missing or current_external_key != plan.get("external_tool_key"):
                result.update({"passed": False, "error": "E_BRANCH_EXTERNAL_TOOL_CHANGED"})
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
        result = {**results[plan["validation_key"]]}
        result["checks"] = {
            name: {**check} for name, check in result.get("checks", {}).items()
        }
        skipped = result.get("checks", {}).get("tests", {}).get("skipped", 0)
        if skipped:
            result["passed"] = False
            result["error"] = "E_BRANCH_VALIDATION_TESTS_SKIPPED"
            result["checks"]["tests"].update(
                passed=False,
                complete=False,
                issue_code="E_BRANCH_VALIDATION_TESTS_SKIPPED",
            )
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
              "checks_passed": all(
                  row["passed"] and row.get("checks", {}).get("tests", {}).get("skipped") == 0
                  for row in rows
              ),
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
    parser.add_argument("--provision-dependencies", action="store_true",
                        help="Install required branch-declared Python test dependencies into the external state directory")
    args = parser.parse_args(argv)
    report = audit_branches(args.repo, args.state_dir, main_ref=args.main_ref, refs=args.ref,
                            python=args.python, environment_key=args.environment_key, jobs=args.jobs, force=args.force,
                            provision_dependencies=args.provision_dependencies)
    print(json.dumps(report, indent=2))
    return 0 if report["checks_passed"] and report["all_compatible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
