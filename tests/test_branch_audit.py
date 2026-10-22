from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.check_branches import (
    audit_branches,
    environment_digest,
    plan_branches,
    provision_requirements,
    run_checks,
)


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
    def passed(snapshot: Path, python: str, sdk: bool, log: Path,
               test_paths: list[str] | None = None,
               dependency_paths: list[Path] | None = None) -> dict:
        checks = {"privacy": {"passed": True},
                  "tests": {"passed": True, "count": 2, "skipped": 0, "complete": True}}
        if sdk:
            checks["engine_faq"] = {"passed": True}
        return {"passed": True, "checks": checks}

    def audit(self, **kwargs: object) -> dict:
        with patch("scripts.check_branches.environment_digest", return_value="fixture-environment"):
            return audit_branches(self.repo, self.state, main_ref="main", refs=["sdk-fixture-1.0"], jobs=1, **kwargs)

    def test_older_sdk_is_tested_with_new_core_without_changing_any_branch(self) -> None:
        before = self.git("show-ref"), self.git("status", "--porcelain")
        def check(snapshot: Path, python: str, sdk: bool, log: Path,
                  test_paths: list[str] | None = None,
                  dependency_paths: list[Path] | None = None) -> dict:
            self.assertEqual((snapshot / "core.py").read_text(), "# newer core\n")
            self.assertEqual((snapshot / "adapter.py").exists(), sdk)
            self.assertEqual(test_paths, ["tests/test_adapter.py"] if sdk else None)
            self.assertEqual(subprocess.check_output(["git", "status", "--porcelain"], cwd=snapshot), b"")
            self.assertTrue((snapshot / ".git").is_dir())
            return self.passed(snapshot, python, sdk, log)
        with patch("scripts.check_branches.run_checks", side_effect=check) as runner:
            result = self.audit()
            self.assertEqual(runner.call_count, 2)
        self.assertEqual(result["branches"][1]["compatibility"], "update_ready")
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
        def check(snapshot: Path, python: str, sdk: bool, log: Path,
                  test_paths: list[str] | None = None,
                  dependency_paths: list[Path] | None = None) -> dict:
            if sdk:
                self.assertEqual((snapshot / "core.py").read_text(), "# branch core customization\n")
                self.assertIsNone(test_paths)
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
        self.assertFalse(result["checks_passed"])
        self.assertFalse(result["branches"][1]["passed"])
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

    def test_skipped_test_is_an_explicit_incomplete_failure(self) -> None:
        snapshot = self.root / "snapshot"
        (snapshot / "tests").mkdir(parents=True)
        (snapshot / "tests/__init__.py").write_text("")
        (snapshot / "tests/test_fixture.py").write_text(
            "import unittest\nclass Fixture(unittest.TestCase):\n"
            "    @unittest.skip('fixture')\n    def test_skipped(self): pass\n"
            "    @unittest.expectedFailure\n    def test_expected(self): self.fail('fixture')\n"
        )
        result = run_checks(snapshot, sys.executable, False, self.root / "run.log")
        self.assertFalse(result["passed"])
        self.assertEqual(result["checks"]["tests"]["skipped"], 1)
        self.assertFalse(result["checks"]["tests"]["complete"])
        self.assertEqual(result["checks"]["tests"]["issue_code"], "E_BRANCH_VALIDATION_TESTS_SKIPPED")

    def test_failed_unittest_summary_keeps_its_skip_count(self) -> None:
        snapshot = self.root / "snapshot"
        (snapshot / "tests").mkdir(parents=True)
        (snapshot / "tests/__init__.py").write_text("")
        (snapshot / "tests/test_fixture.py").write_text(
            "import unittest\nclass Fixture(unittest.TestCase):\n"
            "    @unittest.skip('fixture')\n    def test_skipped(self): pass\n"
            "    def test_failed(self): self.fail('fixture')\n"
        )
        result = run_checks(snapshot, sys.executable, False, self.root / "failed.log")
        self.assertFalse(result["passed"])
        self.assertEqual(result["checks"]["tests"]["skipped"], 1)
        self.assertEqual(result["checks"]["tests"]["issue_code"], "E_BRANCH_VALIDATION_TESTS_SKIPPED")

    def test_runner_rebinds_subprocess_temp_directory_to_audit_state(self) -> None:
        snapshot = self.root / "snapshot"
        (snapshot / "tests").mkdir(parents=True)
        (snapshot / "tests/__init__.py").write_text("")
        (snapshot / "tests/test_temp.py").write_text(
            "import os,tempfile,unittest\nfrom pathlib import Path\n"
            "class Temp(unittest.TestCase):\n"
            "    def test_writable_temp(self):\n"
            "        with tempfile.TemporaryDirectory() as path:\n"
            "            target=Path(path)/'proof'; target.write_text('ok'); self.assertEqual(target.read_text(),'ok')\n"
            "        self.assertEqual(Path(tempfile.gettempdir()),Path(os.environ['TEMP']))\n"
        )
        log = self.root / "audit" / "fixture.log"
        log.parent.mkdir()
        with patch.dict("os.environ", {"TMP": str(self.root / "denied"), "TEMP": str(self.root / "denied")}, clear=False):
            result = run_checks(snapshot, sys.executable, False, log)
        self.assertTrue(result["passed"])
        self.assertTrue((log.parent / "tmp" / log.stem).is_dir())

    def test_adapter_test_scope_does_not_repeat_core_suite(self) -> None:
        snapshot = self.root / "snapshot"
        (snapshot / "tests").mkdir(parents=True)
        (snapshot / "tests/__init__.py").write_text("")
        (snapshot / "docs").mkdir()
        (snapshot / "docs/engine-faq.md").write_text(
            "<!-- parr0t-engine-faq:v1 -->\n# Engine exception runbook\n\n_No SDK-external engine exceptions are currently documented._\n"
        )
        (snapshot / "tests/test_core.py").write_text(
            "import unittest\nclass Core(unittest.TestCase):\n"
            "    def test_must_not_run(self): self.fail('repeated core suite')\n"
        )
        (snapshot / "tests/test_adapter.py").write_text(
            "import unittest\nclass Adapter(unittest.TestCase):\n"
            "    def test_adapter(self): self.assertTrue(True)\n"
        )
        log = self.root / "audit" / "adapter.log"
        log.parent.mkdir()
        result = run_checks(snapshot, sys.executable, True, log, ["tests/test_adapter.py"])
        self.assertTrue(result["passed"])
        self.assertEqual(result["checks"]["tests"]["scope"], "adapter")
        self.assertEqual(result["checks"]["tests"]["count"], 1)

    def test_empty_adapter_scope_does_not_fall_back_to_the_core_suite(self) -> None:
        snapshot = self.root / "snapshot"
        (snapshot / "tests").mkdir(parents=True)
        (snapshot / "tests/__init__.py").write_text("")
        (snapshot / "docs").mkdir()
        (snapshot / "docs/engine-faq.md").write_text(
            "<!-- parr0t-engine-faq:v1 -->\n# Engine exception runbook\n\n"
            "_No SDK-external engine exceptions are currently documented._\n"
        )
        (snapshot / "tests/test_core.py").write_text(
            "import unittest\nclass Core(unittest.TestCase):\n"
            "    def test_must_not_run(self): self.fail('repeated core suite')\n"
        )
        log = self.root / "audit" / "empty-adapter.log"
        log.parent.mkdir()
        result = run_checks(snapshot, sys.executable, True, log, [])
        self.assertFalse(result["passed"])
        self.assertEqual(result["checks"]["tests"]["scope"], "adapter")
        self.assertEqual(result["checks"]["tests"]["count"], 0)

    def test_native_tool_configuration_changes_invalidate_environment(self) -> None:
        with patch("scripts.check_branches.subprocess.run", return_value=type("Probe", (), {"stdout": "fixture"})()):
            with patch.dict("os.environ", {"GAME_L10N_CSC": "fixture-compiler-a"}):
                first = environment_digest(sys.executable, "")
            with patch.dict("os.environ", {"GAME_L10N_CSC": "fixture-compiler-b"}):
                second = environment_digest(sys.executable, "")
        self.assertNotEqual(first, second)

    def test_nested_sdk_requirements_are_discovered_and_provisioned_outside_repo(self) -> None:
        self.git("switch", "sdk-fixture-1.0")
        self.write("profiles/fixture-requirements.txt", "# no third-party fixture packages\n")
        self.commit("Declare fixture validation environment")
        self.git("switch", "main")
        plan = plan_branches(self.repo, "main", ["sdk-fixture-1.0"])[1]
        self.assertEqual(
            [row["path"] for row in plan["validation_requirements"]],
            ["profiles/fixture-requirements.txt"],
        )
        actual_run = subprocess.run
        def inspect_environment(command: list[str], **kwargs: object):
            if command[:3] == [sys.executable, "-m", "pip"]:
                environment = kwargs["env"]
                self.assertEqual(environment["TMP"], environment["TEMP"])
                self.assertTrue(Path(environment["TMP"]).is_relative_to(self.state))
            return actual_run(command, **kwargs)
        with patch("scripts.check_branches.subprocess.run", side_effect=inspect_environment):
            paths, key = provision_requirements(self.repo, self.state, plan, sys.executable)
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].is_relative_to(self.state))
        self.assertTrue((paths[0] / ".parr0t-dependencies.json").is_file())
        self.assertEqual(provision_requirements(self.repo, self.state, plan, sys.executable), (paths, key))
        (paths[0] / "unexpected-file").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "STATE_CHANGED"):
            provision_requirements(self.repo, self.state, plan, sys.executable)

    def test_declared_requirements_cannot_fall_back_to_ambient_packages(self) -> None:
        self.git("switch", "sdk-fixture-1.0")
        self.write("profiles/fixture-requirements.txt", "# exact isolated environment required\n")
        self.commit("Declare fixture validation dependency")
        self.git("switch", "main")
        with patch("scripts.check_branches.run_checks", side_effect=self.passed) as runner:
            result = self.audit(force=True)
        self.assertEqual(runner.call_count, 1)
        branch = result["branches"][1]
        self.assertFalse(branch["passed"])
        self.assertEqual(branch["error"], "E_BRANCH_DEPENDENCIES_NOT_PROVISIONED")

    def test_sdk_external_tools_are_machine_checked_before_adapter_tests(self) -> None:
        self.git("switch", "sdk-fixture-1.0")
        self.write("profiles/sdk-validation.json", json.dumps({
            "schema": "parr0t.sdk-validation.v1",
            "required_environment_files": ["PARR0T_FIXTURE_TOOL"],
        }))
        self.commit("Declare fixture external validator")
        self.git("switch", "main")
        with patch.dict("os.environ", {}, clear=False), patch(
            "scripts.check_branches.run_checks", side_effect=self.passed
        ) as runner:
            os.environ.pop("PARR0T_FIXTURE_TOOL", None)
            result = self.audit(force=True)
        self.assertEqual(runner.call_count, 1)
        branch = result["branches"][1]
        self.assertFalse(branch["passed"])
        self.assertEqual(branch["error"], "E_BRANCH_EXTERNAL_TOOL_MISSING:PARR0T_FIXTURE_TOOL")

    def test_sdk_external_tool_bytes_invalidate_only_its_evidence(self) -> None:
        self.git("switch", "sdk-fixture-1.0")
        self.write("profiles/sdk-validation.json", json.dumps({
            "schema": "parr0t.sdk-validation.v1",
            "required_environment_files": ["PARR0T_FIXTURE_TOOL"],
        }))
        self.commit("Declare fixture external validator")
        self.git("switch", "main")
        tool = self.root / "fixture-tool"
        tool.write_text("first", encoding="utf-8")
        with patch.dict("os.environ", {"PARR0T_FIXTURE_TOOL": str(tool)}), patch(
            "scripts.check_branches.run_checks", side_effect=self.passed
        ):
            first = self.audit()
            tool.write_text("second", encoding="utf-8")
            second = self.audit()
        self.assertEqual(second["executed"], 1)
        self.assertTrue(second["branches"][0]["cached"])
        self.assertNotEqual(
            first["branches"][1]["validation_key"],
            second["branches"][1]["validation_key"],
        )

    def test_sdk_external_tool_change_during_tests_holds_result(self) -> None:
        self.git("switch", "sdk-fixture-1.0")
        self.write("profiles/sdk-validation.json", json.dumps({
            "schema": "parr0t.sdk-validation.v1",
            "required_environment_files": ["PARR0T_FIXTURE_TOOL"],
        }))
        self.commit("Declare fixture external validator")
        self.git("switch", "main")
        tool = self.root / "fixture-tool"
        tool.write_text("first", encoding="utf-8")
        def mutate(*args: object) -> dict:
            if args[2]:
                tool.write_text("changed-during-validation", encoding="utf-8")
            return self.passed(*args)
        with patch.dict("os.environ", {"PARR0T_FIXTURE_TOOL": str(tool)}), patch(
            "scripts.check_branches.run_checks", side_effect=mutate
        ):
            result = self.audit(force=True)
        self.assertFalse(result["branches"][1]["passed"])
        self.assertEqual(result["branches"][1]["error"], "E_BRANCH_EXTERNAL_TOOL_CHANGED")

    def test_dependency_install_failure_is_cached_until_forced(self) -> None:
        self.git("switch", "sdk-fixture-1.0")
        self.write("profiles/fixture-requirements.txt", "fixture-package==1\n")
        self.commit("Declare failing fixture dependency")
        self.git("switch", "main")
        plan = plan_branches(self.repo, "main", ["sdk-fixture-1.0"])[1]
        actual_run = subprocess.run
        pip_calls = 0
        def fail_pip(command: list[str], **kwargs: object):
            nonlocal pip_calls
            if command[:3] == [sys.executable, "-m", "pip"]:
                pip_calls += 1
                return subprocess.CompletedProcess(command, 1, "", "fixture failure")
            return actual_run(command, **kwargs)
        with patch("scripts.check_branches.subprocess.run", side_effect=fail_pip):
            with self.assertRaisesRegex(ValueError, "INSTALL_FAILED"):
                provision_requirements(self.repo, self.state, plan, sys.executable)
            with self.assertRaisesRegex(ValueError, "INSTALL_FAILED"):
                provision_requirements(self.repo, self.state, plan, sys.executable)
        self.assertEqual(pip_calls, 1)
        dependencies = self.state / "dependencies"
        self.assertFalse(dependencies.exists() and any(dependencies.iterdir()))

    def test_dependency_change_during_tests_holds_result(self) -> None:
        self.git("switch", "sdk-fixture-1.0")
        self.write("profiles/fixture-requirements.txt", "# empty fixture environment\n")
        self.commit("Declare fixture validation dependency")
        self.git("switch", "main")
        def mutate(snapshot: Path, python: str, sdk: bool, log: Path,
                   test_paths: list[str] | None = None,
                   dependency_paths: list[Path] | None = None) -> dict:
            if sdk:
                self.assertEqual(len(dependency_paths or []), 1)
                (dependency_paths[0] / "changed-during-validation").write_text("changed", encoding="utf-8")
            return self.passed(snapshot, python, sdk, log, test_paths, dependency_paths)
        with patch("scripts.check_branches.run_checks", side_effect=mutate):
            result = self.audit(force=True, provision_dependencies=True)
        self.assertFalse(result["branches"][1]["passed"])
        self.assertEqual(result["branches"][1]["error"], "E_BRANCH_DEPENDENCY_STATE_CHANGED")

    def test_invalid_sdk_validation_manifest_is_a_branch_hold(self) -> None:
        self.git("switch", "sdk-fixture-1.0")
        self.write("profiles/sdk-validation.json", "{}")
        self.commit("Add invalid fixture validation manifest")
        self.git("switch", "main")
        with patch("scripts.check_branches.run_checks", side_effect=self.passed) as runner:
            result = self.audit(force=True)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(result["branches"][1]["error"], "E_BRANCH_VALIDATION_MANIFEST_INVALID")

    def test_audit_state_cannot_pollute_the_repository(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside"):
            audit_branches(self.repo, self.repo / "state")
