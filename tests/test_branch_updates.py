from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.update_branches import apply_updates, local_ref, plan_updates


class BranchUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.invalid"), ("commit.gpgsign", "false")):
            self.git("config", key, value)
        self.write("core.py", "# core\n")
        self.commit("Add fixture core")
        self.git("switch", "-c", "sdk-fixture-1.0")
        self.write("adapter.py", "# adapter\n")
        self.commit("Add fixture adapter")
        self.sdk = self.git("rev-parse", "HEAD")
        self.git("switch", "main")
        self.write("core.py", "# newer core\n")
        self.commit("Advance fixture core")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=True).stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> None:
        self.git("add", ".")
        self.git("commit", "-m", message)

    @staticmethod
    def proof(*args: object, **kwargs: object) -> dict:
        plans = kwargs["pinned_plans"]
        rows = [{**plan, "passed": True, "checks": {"tests": {"count": 2, "skipped": 0}}, "cached": False} for plan in plans]
        return {"branches": rows, "executed": len(rows), "reused": 0}

    def plan(self, **kwargs: object) -> dict:
        with patch("scripts.update_branches.audit_branches", side_effect=self.proof):
            return plan_updates(self.repo, self.root / "state", main_ref="main", refs=["refs/heads/sdk-fixture-1.0"], **kwargs)

    def equivalent_core(self) -> None:
        self.git("switch", "sdk-fixture-1.0")
        self.write("core.py", "# newer core\n")
        self.commit("Equivalent fixture core")
        self.git("switch", "main")

    def test_stale_core_is_update_ready_instead_of_skip_compatible(self) -> None:
        before = self.git("show-ref"), self.git("status", "--porcelain")
        report = self.plan()
        row = report["branches"][0]
        self.assertFalse(row["core_current"])
        self.assertEqual(row["action"], "update_ready")
        self.assertEqual(before, (self.git("show-ref"), self.git("status", "--porcelain")))

    def test_already_updated_branch_skips_validation_and_merge(self) -> None:
        self.git("switch", "sdk-fixture-1.0")
        self.git("merge", "main", "--no-edit")
        self.git("switch", "main")
        with patch("scripts.update_branches.audit_branches") as validator, patch("scripts.update_branches.merge_tree") as merge:
            report = plan_updates(self.repo, self.root / "state", main_ref="main", refs=["sdk-fixture-1.0"])
            validator.assert_not_called()
            merge.assert_not_called()
        self.assertEqual(report["branches"][0]["action"], "skip_up_to_date")
        self.assertEqual(report["validation_executed"], 0)

    def test_equal_core_can_skip_without_ancestry_but_promotion_can_require_it(self) -> None:
        self.equivalent_core()
        with patch("scripts.update_branches.merge_tree") as merge:
            report = self.plan()
            merge.assert_not_called()
        self.assertEqual(report["branches"][0]["action"], "skip_compatible")
        self.assertEqual(self.plan(require_ancestry=True)["branches"][0]["action"], "update_ready")

    def test_apply_preserves_checkout_and_creates_a_merge_with_exact_validated_tree(self) -> None:
        report = self.plan()
        expected_tree = report["branches"][0]["merge_tree"]
        before = self.git("rev-parse", "HEAD"), (self.repo / "core.py").read_bytes()
        result = apply_updates(self.repo, report)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(self.git("rev-parse", "sdk-fixture-1.0^{tree}"), expected_tree)
        self.assertEqual(len(self.git("rev-list", "--parents", "-n", "1", "sdk-fixture-1.0").split()), 3)
        self.assertEqual(before, (self.git("rev-parse", "HEAD"), (self.repo / "core.py").read_bytes()))
        with patch("scripts.update_branches.audit_branches") as validator:
            next_run = plan_updates(self.repo, self.root / "state", main_ref="main", refs=["sdk-fixture-1.0"])
            validator.assert_not_called()
        self.assertEqual(next_run["branches"][0]["action"], "skip_up_to_date")

    def test_core_overrides_are_held_without_expensive_checks(self) -> None:
        self.git("switch", "sdk-fixture-1.0")
        self.write("core.py", "# customized core\n")
        self.commit("Customize fixture core")
        self.git("switch", "main")
        with patch("scripts.update_branches.audit_branches") as validator:
            report = plan_updates(self.repo, self.root / "state", main_ref="main", refs=["sdk-fixture-1.0"])
            validator.assert_not_called()
        self.assertEqual(report["branches"][0]["action"], "manual_core_reconciliation")
        self.assertEqual(apply_updates(self.repo, report)["applied"], 0)

    def test_skips_or_failed_validation_hold_the_update(self) -> None:
        for passed, skipped in ((True, 1), (False, 0)):
            def incomplete(*args: object, **kwargs: object) -> dict:
                proof = self.proof(*args, **kwargs)
                proof["branches"][1]["passed"] = passed
                proof["branches"][1]["checks"]["tests"]["skipped"] = skipped
                return proof
            with self.subTest(passed=passed, skipped=skipped), patch("scripts.update_branches.audit_branches", side_effect=incomplete):
                report = plan_updates(self.repo, self.root / "state", main_ref="main", refs=["sdk-fixture-1.0"])
            self.assertEqual(report["branches"][0]["action"], "validation_held")
            self.assertEqual(apply_updates(self.repo, report)["applied"], 0)

    def test_file_directory_merge_conflict_is_held_before_validation(self) -> None:
        self.write("collision", "# core file\n")
        self.commit("Add fixture core file")
        self.git("switch", "sdk-fixture-1.0")
        self.write("collision/adapter.py", "# adapter file\n")
        self.commit("Add colliding fixture directory")
        self.git("switch", "main")
        with patch("scripts.update_branches.audit_branches") as validator:
            report = plan_updates(self.repo, self.root / "state", main_ref="main", refs=["sdk-fixture-1.0"])
            validator.assert_not_called()
        self.assertEqual(report["branches"][0]["action"], "manual_merge_review")

    def test_checked_out_dirty_target_is_untouched(self) -> None:
        report = self.plan()
        self.git("switch", "sdk-fixture-1.0")
        self.write("adapter.py", "# private uncommitted fixture edit\n")
        result = apply_updates(self.repo, report)
        self.assertEqual(result["applied"], 0)
        self.assertIn("CHECKED_OUT", result["branches"][0]["error"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.sdk)
        self.assertEqual((self.repo / "adapter.py").read_text(), "# private uncommitted fixture edit\n")

    def test_linked_worktree_target_is_held(self) -> None:
        report = self.plan()
        self.git("worktree", "add", str(self.root / "linked"), "sdk-fixture-1.0")
        result = apply_updates(self.repo, report)
        self.assertIn("CHECKED_OUT", result["branches"][0]["error"])
        self.assertEqual(result["applied"], 0)

    def test_target_or_main_drift_holds_without_overwrite(self) -> None:
        for target in ("main", "sdk-fixture-1.0"):
            report = self.plan()
            self.git("switch", target)
            self.git("commit", "--allow-empty", "-m", "Concurrent fixture advance")
            new_tip = self.git("rev-parse", "HEAD")
            self.git("switch", "main")
            result = apply_updates(self.repo, report)
            self.assertEqual(result["applied"], 0)
            self.assertIn("STALE", result["branches"][0]["error"])
            self.assertEqual(self.git("rev-parse", target), new_tip)

    def test_atomic_transaction_detects_main_race_after_commit_creation(self) -> None:
        report = self.plan()
        real_run = subprocess.run
        def racing(command: list[str], **kwargs: object):
            if "update-ref" in command:
                self.git("commit", "--allow-empty", "-m", "Race fixture main")
            return real_run(command, **kwargs)
        with patch("scripts.update_branches.subprocess.run", side_effect=racing):
            result = apply_updates(self.repo, report)
        self.assertEqual(result["applied"], 0)
        self.assertIn("STALE_REF", result["branches"][0]["error"])
        self.assertEqual(self.git("rev-parse", "sdk-fixture-1.0"), self.sdk)

    def test_core_deletion_requires_update_and_is_preserved(self) -> None:
        self.git("rm", "core.py")
        self.commit("Remove obsolete fixture core")
        report = self.plan()
        self.assertFalse(report["branches"][0]["core_current"])
        self.assertEqual(apply_updates(self.repo, report)["applied"], 1)
        self.assertEqual(self.git("ls-tree", "sdk-fixture-1.0", "core.py"), "")

    def test_equal_text_with_different_mode_still_requires_update(self) -> None:
        self.write("core.py", "# core\n")
        self.commit("Restore fixture core content")
        self.git("update-index", "--chmod=+x", "core.py")
        self.git("commit", "-m", "Make fixture core executable")
        report = self.plan()
        self.assertFalse(report["branches"][0]["core_current"])
        self.assertEqual(report["branches"][0]["action"], "update_ready")

    def test_signing_failure_does_not_retry_unsigned_or_update_refs(self) -> None:
        report = self.plan()
        self.git("config", "commit.gpgsign", "true")
        from scripts.update_branches import git as actual_git
        seen = []
        def fail_signing(repo: Path, *args: str):
            if args[0] == "commit-tree":
                seen.append(args)
                raise ValueError("E_FIXTURE_SIGNING_FAILED")
            return actual_git(repo, *args)
        with patch("scripts.update_branches.git", side_effect=fail_signing):
            result = apply_updates(self.repo, report)
        self.assertEqual(result["applied"], 0)
        self.assertEqual(len(seen), 1)
        self.assertIn("-S", seen[0])
        self.assertEqual(self.git("rev-parse", "sdk-fixture-1.0"), self.sdk)

    def test_real_validation_cache_apply_and_next_run_skip(self) -> None:
        self.write("tests/__init__.py", "")
        self.write("tests/test_core.py", "import unittest\nfrom pathlib import Path\nclass Core(unittest.TestCase):\n    def test_new_core(self): self.assertIn('newer core', Path('core.py').read_text())\n")
        self.commit("Add fixture core regression")
        self.git("switch", "sdk-fixture-1.0")
        self.write("tests/__init__.py", "")
        self.write("tests/test_adapter.py", "import unittest\nimport adapter\nfrom pathlib import Path\nclass Adapter(unittest.TestCase):\n    def test_new_core_integration(self): self.assertIn('newer core', Path('core.py').read_text())\n")
        self.write("docs/engine-faq.md", "<!-- parr0t-engine-faq:v1 -->\n# Engine exception runbook\n\n_No SDK-external engine exceptions are currently documented._\n")
        self.commit("Add fixture adapter regression")
        self.git("switch", "main")
        options = {"main_ref": "main", "refs": ["refs/heads/sdk-fixture-1.0"], "jobs": 1}
        first = plan_updates(self.repo, self.root / "real-state", **options)
        self.assertEqual(first["branches"][0]["action"], "update_ready")
        self.assertEqual(first["validation_executed"], 2)
        cached = plan_updates(self.repo, self.root / "real-state", **options)
        self.assertEqual(cached["validation_executed"], 0)
        self.assertEqual(cached["validation_reused"], 2)
        self.assertEqual(apply_updates(self.repo, cached)["applied"], 1)
        with patch("scripts.update_branches.audit_branches") as validator:
            complete = plan_updates(self.repo, self.root / "real-state", **options)
            validator.assert_not_called()
        self.assertEqual(complete["branches"][0]["action"], "skip_up_to_date")
        self.assertEqual(complete["validation_executed"], 0)

    def test_local_target_names_are_required(self) -> None:
        for name in ("origin/sdk-fixture-1.0", "refs/remotes/origin/sdk-fixture-1.0", "main", "../sdk-fixture"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                local_ref(name)
        self.assertEqual(local_ref("sdk-fixture-1.0"), "refs/heads/sdk-fixture-1.0")
