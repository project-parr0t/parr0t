from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.check_branches import audit_branches, environment_digest, run_checks


class BranchAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.write("core.py", "# core\n")
        self.write("tests/__init__.py", "")
        self.write("tests/test_core.py", "import unittest\nclass Core(unittest.TestCase):\n    def test_fixture(self): self.assertTrue(True)\n")
        self.commit("Add fixture core")
        self.git("switch", "-c", "sdk-fixture-1.0")
        self.write("adapter.py", "# adapter\n")
        self.write("docs/engine-faq.md", "# Engine exceptions\n\nNo manual exceptions.\n")
        self.write("tests/test_adapter.py", "# synthetic adapter tests\n")
        self.commit("Add fixture adapter")
        self.git("switch", "main")
        self.write("core.py", "# newer core\n")
        self.commit("Advance fixture core")
        self.state = self.root / "state"

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=True).stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.repo / path
        target.parent.mkdir(exist_ok=True, parents=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> None:
        self.git("add", ".")
        self.git("commit", "-m", message)

    @staticmethod
    def passed(snapshot: Path, python: str, sdk: bool, log: Path) -> dict:
        checks = {"privacy": {"passed": True}, "tests": {"passed": True, "count": 2, "skipped": 0}}
        if sdk:
            checks["engine_faq"] = {"passed": True}
        return {"passed": True, "checks": checks}

    def audit(self, **kwargs: object) -> dict:
        with patch("scripts.check_branches.environment_digest", return_value="fixture-environment"):
            return audit_branches(self.repo, self.state, main_ref="main", refs=["sdk-fixture-1.0"], jobs=1, **kwargs)

    def test_older_sdk_is_tested_with_new_core_without_changing_any_branch(self) -> None:
        before = self.git("show-ref"), self.git("status", "--porcelain")
        def check(snapshot: Path, python: str, sdk: bool, log: Path) -> dict:
            self.assertEqual((snapshot / "core.py").read_text(), "# newer core\n")
            self.assertEqual((snapshot / "adapter.py").exists(), sdk)
            self.assertEqual(subprocess.check_output(["git", "status", "--porcelain"], cwd=snapshot), b"")
            self.assertTrue((snapshot / ".git").is_dir())
            return self.passed(snapshot, python, sdk, log)
        with patch("scripts.check_branches.run_checks", side_effect=check) as runner:
            result = self.audit()
            self.assertEqual(runner.call_count, 2)
        self.assertEqual(result["branches"][1]["compatibility"], "up_to_date_compatible")
        self.assertEqual(before, (self.git("show-ref"), self.git("status", "--porcelain")))

    def test_identical_validated_content_skips_even_after_empty_commit(self) -> None:
        with patch("scripts.check_branches.run_checks", side_effect=self.passed):
            self.audit()
        self.git("switch", "sdk-fixture-1.0")
        self.git("commit", "--allow-empty", "-m", "Metadata-only fixture commit")
        with patch("scripts.check_branches.run_checks") as runner:
            result = self.audit()
            runner.assert_not_called()
        self.assertEqual(result["executed"], 0)
        self.assertEqual(result["reused"], 2)

    def test_main_change_invalidates_overlay_but_not_unchanged_branch_source(self) -> None:
        with patch("scripts.check_branches.run_checks", side_effect=self.passed):
            first = self.audit()
            self.write("core.py", "# changed again\n")
            self.commit("Change fixture core again")
            second = self.audit()
        self.assertEqual(second["executed"], 2)
        self.assertNotEqual(first["branches"][1]["validation_key"], second["branches"][1]["validation_key"])

    def test_adapter_change_invalidates_only_that_adapter(self) -> None:
        with patch("scripts.check_branches.run_checks", side_effect=self.passed):
            self.audit()
            self.git("switch", "sdk-fixture-1.0")
            self.write("adapter.py", "# changed adapter\n")
            self.commit("Change fixture adapter")
            result = self.audit()
        self.assertEqual(result["executed"], 1)
        self.assertTrue(result["branches"][0]["cached"])

    def test_core_override_is_reported_without_overwriting_it(self) -> None:
        self.git("switch", "sdk-fixture-1.0")
        self.write("core.py", "# branch core customization\n")
        self.commit("Customize fixture core")
        def check(snapshot: Path, python: str, sdk: bool, log: Path) -> dict:
            if sdk:
                self.assertEqual((snapshot / "core.py").read_text(), "# branch core customization\n")
            return self.passed(snapshot, python, sdk, log)
        with patch("scripts.check_branches.run_checks", side_effect=check):
            result = self.audit()
        row = result["branches"][1]
        self.assertEqual(row["core_overrides"], ["core.py"])
        self.assertEqual(row["compatibility"], "core_diverged")
        self.assertEqual(row["update_action"], "manual_review")

    def test_equivalent_core_content_is_compatible_without_ancestry_update(self) -> None:
        self.git("switch", "sdk-fixture-1.0")
        self.write("core.py", "# newer core\n")
        self.commit("Equivalent fixture core change")
        with patch("scripts.check_branches.run_checks", side_effect=self.passed):
            result = self.audit()
        self.assertEqual(result["branches"][1]["compatibility"], "up_to_date_compatible")

    def test_validator_or_environment_changes_invalidate_all_proofs(self) -> None:
        with patch("scripts.check_branches.run_checks", side_effect=self.passed):
            self.audit()
            with patch("scripts.check_branches.validator_digest", return_value="new-validator"):
                self.assertEqual(self.audit()["executed"], 2)
            with patch("scripts.check_branches.environment_digest", return_value="new-environment"):
                result = audit_branches(self.repo, self.state, main_ref="main", refs=["sdk-fixture-1.0"], jobs=1)
                self.assertEqual(result["executed"], 2)

    def test_malformed_cache_and_failed_checks_never_skip_validation(self) -> None:
        self.state.mkdir()
        (self.state / "cache.json").write_text("broken", encoding="utf-8")
        with patch("scripts.check_branches.run_checks", return_value={"passed": False, "checks": {}}):
            self.assertFalse(self.audit()["checks_passed"])
        with patch("scripts.check_branches.run_checks", side_effect=self.passed):
            self.assertEqual(self.audit()["executed"], 2)
        cache = json.loads((self.state / "cache.json").read_text())
        for value in cache["results"].values():
            value["checks"] = {}
        (self.state / "cache.json").write_text(json.dumps(cache))
        with patch("scripts.check_branches.run_checks", side_effect=self.passed):
            self.assertEqual(self.audit()["executed"], 2)

    def test_skipped_tests_do_not_authorize_skipping_update(self) -> None:
        def partial(*args: object) -> dict:
            result = self.passed(*args)
            result["checks"]["tests"]["skipped"] = 1
            return result
        with patch("scripts.check_branches.run_checks", side_effect=partial):
            result = self.audit()
        self.assertFalse(result["all_compatible"])
        self.assertEqual(result["branches"][1]["compatibility"], "unverified")

    def test_real_runner_rejects_zero_tests_and_uses_trusted_privacy_checker(self) -> None:
        snapshot = self.root / "snapshot"
        (snapshot / "tests").mkdir(parents=True)
        (snapshot / "tests/__init__.py").write_text("")
        result = run_checks(snapshot, sys.executable, False, self.root / "run.log")
        self.assertFalse(result["passed"])
        (snapshot / "private.csv").write_text("fixture")
        result = run_checks(snapshot, sys.executable, False, self.root / "run.log")
        self.assertFalse(result["checks"]["privacy"]["passed"])

    def test_skip_count_is_preserved_alongside_expected_failures(self) -> None:
        snapshot = self.root / "snapshot"
        (snapshot / "tests").mkdir(parents=True)
        (snapshot / "tests/__init__.py").write_text("")
        (snapshot / "tests/test_fixture.py").write_text(
            "import unittest\nclass Fixture(unittest.TestCase):\n"
            "    @unittest.skip('fixture')\n    def test_skipped(self): pass\n"
            "    @unittest.expectedFailure\n    def test_expected(self): self.fail('fixture')\n"
        )
        result = run_checks(snapshot, sys.executable, False, self.root / "run.log")
        self.assertTrue(result["passed"])
        self.assertEqual(result["checks"]["tests"]["skipped"], 1)

    def test_native_tool_configuration_changes_invalidate_environment(self) -> None:
        with patch("scripts.check_branches.subprocess.run", return_value=type("Probe", (), {"stdout": "fixture"})()):
            with patch.dict("os.environ", {"GAME_L10N_CSC": "fixture-compiler-a"}):
                first = environment_digest(sys.executable, "")
            with patch.dict("os.environ", {"GAME_L10N_CSC": "fixture-compiler-b"}):
                second = environment_digest(sys.executable, "")
        self.assertNotEqual(first, second)

    def test_audit_state_cannot_pollute_the_repository(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside"):
            audit_branches(self.repo, self.repo / "state")
