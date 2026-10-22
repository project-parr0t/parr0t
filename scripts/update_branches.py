"""Plan SDK core updates; apply only explicit local targets with checked evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.check_branches import audit_branches, digest, git, plan_branches, tree

SCHEMA = "parr0t.branch-update.v1"


def local_ref(name: str) -> str:
    branch = name.removeprefix("refs/heads/")
    if not re.fullmatch(r"sdk-[a-z0-9][a-z0-9.-]*", branch):
        raise ValueError("Update targets must be explicit local sdk-* branch names")
    return "refs/heads/" + branch


def complete_validation(proof: object) -> bool:
    """Accept only a complete, non-skipped test attestation."""
    if not isinstance(proof, dict) or proof.get("passed") is not True:
        return False
    tests = proof.get("checks", {}).get("tests", {})
    return (
        isinstance(tests, dict)
        and tests.get("complete") is True
        and type(tests.get("count")) is int
        and tests["count"] > 0
        and tests.get("skipped") == 0
    )


def checked_out_worktrees(repo: Path) -> dict[str, Path]:
    """Return checked-out local refs and their exact linked worktree paths."""
    result: dict[str, Path] = {}
    worktree: Path | None = None
    for raw in git(repo, "worktree", "list", "--porcelain", "-z").split(b"\0"):
        if not raw:
            worktree = None
            continue
        line = raw.decode("utf-8")
        if line.startswith("worktree "):
            worktree = Path(line.removeprefix("worktree "))
        elif line.startswith("branch ") and worktree is not None:
            result[line.removeprefix("branch ")] = worktree
    return result


def require_clean_worktree(worktree: Path, target: str, expected: str) -> None:
    if git(worktree, "symbolic-ref", "-q", "HEAD").decode().strip() != target:
        raise ValueError("E_BRANCH_UPDATE_WRONG_WORKTREE_BRANCH")
    if git(worktree, "rev-parse", "--verify", "HEAD").decode().strip() != expected:
        raise ValueError("E_BRANCH_UPDATE_STALE_TARGET")
    if git(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise ValueError("E_BRANCH_UPDATE_DIRTY_WORKTREE")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD"):
        marker_path = Path(git(worktree, "rev-parse", "--git-path", marker).decode().strip())
        if not marker_path.is_absolute():
            marker_path = worktree / marker_path
        if marker_path.exists():
            raise ValueError("E_BRANCH_UPDATE_OPERATION_IN_PROGRESS")


def update_clean_worktree(worktree: Path, target: str, expected: str, commit: str, merged: str) -> None:
    """Fast-forward one exact clean checked-out branch without switching it."""
    require_clean_worktree(worktree, target, expected)
    result = subprocess.run(
        ["git", "-C", str(worktree), "merge", "--ff-only", "--no-edit", commit],
        capture_output=True, shell=False,
    )
    if result.returncode:
        raise ValueError("E_BRANCH_UPDATE_WORKTREE_FF_FAILED")
    if git(worktree, "rev-parse", "--verify", "HEAD").decode().strip() != commit:
        raise ValueError("E_BRANCH_UPDATE_WORKTREE_HEAD_MISMATCH")
    if git(worktree, "rev-parse", "--verify", "HEAD^{tree}").decode().strip() != merged:
        raise ValueError("E_BRANCH_UPDATE_WORKTREE_TREE_MISMATCH")
    if git(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise ValueError("E_BRANCH_UPDATE_WORKTREE_NOT_CLEAN")


def merge_tree(repo: Path, plan: dict) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-tree", "--write-tree", plan["commit"], plan["main_commit"]],
        capture_output=True, shell=False,
    )
    if result.returncode:
        raise ValueError("E_BRANCH_UPDATE_CONFLICT")
    merged = result.stdout.splitlines()[0].decode("ascii")
    if digest(tree(repo, merged)) != plan["content_digest"]:
        raise ValueError("E_BRANCH_UPDATE_TREE: Git merge differs from validated snapshot")
    return merged


def plan_updates(repo: Path, state: Path, *, main_ref: str = "origin/main", refs: list[str] | None = None,
                 python: str = sys.executable, environment_key: str = "", jobs: int = 2,
                 require_ancestry: bool = False, provision_dependencies: bool = False) -> dict:
    repo, state = repo.resolve(), state.resolve()
    if state.is_relative_to(repo):
        raise ValueError("Update state must be outside the repository")
    state.mkdir(parents=True, exist_ok=True)
    # One metadata inventory, reused by validation. No repeated ref resolution
    # or full suites for branches whose update decision is already conclusive.
    plans = plan_branches(repo, main_ref, refs)
    rows, pending = [], []
    for plan in plans[1:]:
        if not plan["sdk"]:
            continue
        row = {**plan, "action": "validation_required"}
        rows.append(row)
        if plan["core_overrides"]:
            row["action"] = "manual_core_reconciliation"
        elif plan["merge_base"] == plan["main_commit"]:
            row["action"] = "skip_up_to_date"
        else:
            if not plan["core_current"] or require_ancestry:
                try:
                    row["merge_tree"] = merge_tree(repo, plan)
                except ValueError as error:
                    row.update(action="manual_merge_review", error=str(error))
                    continue
            pending.append(plan)
    validation = None
    if pending:
        validation = audit_branches(
            repo, state, main_ref=main_ref, python=python,
            environment_key=environment_key, jobs=jobs, pinned_plans=[plans[0], *pending],
            provision_dependencies=provision_dependencies,
        )
        evidence = {row["ref"]: row for row in validation["branches"]}
        core_proof = evidence.get(plans[0]["ref"])
        core_ok = complete_validation(core_proof)
        for row in rows:
            if row["action"] != "validation_required":
                continue
            proof = evidence.get(row["ref"])
            if proof is None:
                row.update(action="validation_held", error="E_BRANCH_VALIDATION_EVIDENCE_MISSING")
                continue
            row["validation"] = proof
            if not core_ok or not complete_validation(proof):
                row["action"] = "validation_held"
            elif row["core_current"] and not require_ancestry:
                row["action"] = "skip_compatible"
            else:
                row["action"] = "update_ready"
    report = {
        "schema": SCHEMA, "main_ref": main_ref, "main_commit": plans[0]["main_commit"],
        "require_ancestry": require_ancestry, "branches": rows,
        "validation_executed": validation["executed"] if validation else 0,
        "validation_reused": validation["reused"] if validation else 0,
        "applied": 0,
    }
    (state / "update-plan.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def apply_updates(repo: Path, report: dict) -> dict:
    """Apply only this in-memory, freshly validated plan; never load plan JSON."""
    main_ref = git(repo, "rev-parse", "--symbolic-full-name", report["main_ref"]).decode().strip()
    if not main_ref.startswith(("refs/heads/", "refs/remotes/")):
        raise ValueError("An apply operation requires a named main ref")
    for row in report["branches"]:
        if row["action"] != "update_ready":
            continue
        try:
            target = local_ref(row["ref"])
            if git(repo, "rev-parse", "--verify", target).decode().strip() != row["commit"]:
                raise ValueError("E_BRANCH_UPDATE_STALE_TARGET")
            if git(repo, "rev-parse", "--verify", main_ref).decode().strip() != report["main_commit"]:
                raise ValueError("E_BRANCH_UPDATE_STALE_MAIN")
            # Reconfirm the exact merge tree before signing a commit. No
            # checkout, reset, rebase, stash, or user-file mutation is needed.
            merged = merge_tree(repo, row)
            if merged != row["merge_tree"]:
                raise ValueError("E_BRANCH_UPDATE_STALE_TREE")
            signing = subprocess.run(
                ["git", "-C", str(repo), "config", "--bool", "--get", "commit.gpgsign"],
                capture_output=True, text=True, shell=False,
            )
            if signing.returncode not in {0, 1}:
                raise ValueError("E_BRANCH_UPDATE_SIGNING_CONFIG")
            validation = row.get("validation", {})
            validation_key = validation.get("validation_key")
            if not isinstance(validation_key, str) or not validation_key:
                raise ValueError("E_BRANCH_UPDATE_VALIDATION_KEY_MISSING")
            if not complete_validation(validation):
                raise ValueError("E_BRANCH_UPDATE_VALIDATION_INCOMPLETE")
            if validation.get("content_digest") != row.get("content_digest"):
                raise ValueError("E_BRANCH_UPDATE_VALIDATION_CONTENT_MISMATCH")
            if validation.get("main_commit") != report.get("main_commit"):
                raise ValueError("E_BRANCH_UPDATE_VALIDATION_MAIN_MISMATCH")
            message = (
                f"Merge core {report['main_commit'][:12]} into {target.removeprefix('refs/heads/')}\n\n"
                f"Parr0t-Core: {report['main_commit']}\n"
                f"Parr0t-Validated-Tree: {merged}\n"
                f"Parr0t-Validation-Key: {validation_key}"
            )
            args = ["commit-tree", merged, "-p", row["commit"], "-p", report["main_commit"],
                    "-m", message]
            if signing.stdout.strip() == "true":
                args.append("-S")
            commit = git(repo, *args).decode().strip()
            # An explicitly requested clean linked worktree can be advanced by
            # the same validated fast-forward. Dirty worktrees are held. This
            # removes the former manual-merge escape hatch without checking out,
            # stashing, resetting, or resolving anything automatically.
            occupied = checked_out_worktrees(repo)
            if target in occupied:
                if git(repo, "rev-parse", "--verify", main_ref).decode().strip() != report["main_commit"]:
                    raise ValueError("E_BRANCH_UPDATE_STALE_MAIN")
                update_clean_worktree(occupied[target], target, row["commit"], commit, merged)
                mode = "checked_out_clean_worktree"
            else:
                # Git verifies both expected refs atomically. A stale plan cannot
                # overwrite another process's main or SDK advance.
                transaction = (f"start\nverify {main_ref} {report['main_commit']}\n"
                               f"update {target} {commit} {row['commit']}\nprepare\ncommit\n")
                applied = subprocess.run(["git", "-C", str(repo), "update-ref", "--stdin"],
                                         input=transaction.encode("ascii"), capture_output=True, shell=False)
                if applied.returncode:
                    raise ValueError("E_BRANCH_UPDATE_STALE_REF")
                mode = "ref_transaction"
            row.update(action="updated", new_commit=commit, apply_mode=mode)
            report["applied"] += 1
        except ValueError as error:
            row.update(action="apply_held", error=str(error))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--main-ref", default="origin/main")
    parser.add_argument("--branch", action="append", help="Local sdk-* target; repeatable. Omit for origin SDK planning")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--environment-key", default="")
    parser.add_argument("--jobs", type=int, choices=range(1, 9), default=2)
    parser.add_argument("--require-ancestry", action="store_true", help="Require main ancestry, including for equivalent core content")
    parser.add_argument("--provision-dependencies", action="store_true",
                        help="Install required branch-declared Python test dependencies into the external state directory")
    parser.add_argument("--apply", action="store_true",
                        help="Create local merge commits and advance named local SDK refs or their exact clean worktrees")
    args = parser.parse_args(argv)
    if args.apply and not args.branch:
        parser.error("--apply requires explicit --branch targets")
    refs = [local_ref(branch) for branch in args.branch] if args.branch else None
    report = plan_updates(args.repo, args.state_dir, main_ref=args.main_ref, refs=refs,
                          python=args.python, environment_key=args.environment_key, jobs=args.jobs,
                          require_ancestry=args.require_ancestry,
                          provision_dependencies=args.provision_dependencies)
    if args.apply:
        report = apply_updates(args.repo.resolve(), report)
        (args.state_dir / "update-result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    held = {"manual_core_reconciliation", "manual_merge_review", "validation_held", "apply_held"}
    return 1 if any(row["action"] in held for row in report["branches"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
