from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from game_l10n.cli import build_parser, main
from game_l10n.errors import ConfigurationError
from game_l10n.workspace import verify_sdk_promotion_commit


class SDKPromotionPreflightTests(unittest.TestCase):
    branch = "sdk-fixture-1.0"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.root / "core.txt").write_text("core\n", encoding="utf-8")
        self.git("add", "core.txt")
        self.git("commit", "-m", "Add fixture core")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("switch", "-c", self.branch)

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, text=True, encoding="utf-8",
            capture_output=True, check=True, shell=False,
        ).stdout

    def preflight(self, **kwargs: object) -> dict:
        return verify_sdk_promotion_commit(
            self.root, self.branch, working_tree=True, **kwargs,
        )

    def test_preflight_binds_base_before_files_exist_and_never_mutates_checkout(self) -> None:
        index = self.root / ".git" / "index"
        before = (index.read_bytes(), self.git("rev-parse", "HEAD"), self.git("status", "--porcelain"))
        result = self.preflight(planned_paths=["profiles/fixture tool.py"])
        self.assertEqual(result["status"], "scope_ready")
        self.assertEqual(result["base"], self.base)
        self.assertEqual(result["changed_paths"], ["profiles/fixture tool.py"])
        self.assertFalse((self.root / "profiles").exists())
        after = (index.read_bytes(), self.git("rev-parse", "HEAD"), self.git("status", "--porcelain"))
        self.assertEqual(before, after)

    def test_preflight_includes_committed_staged_unstaged_and_untracked_files(self) -> None:
        (self.root / "adapter.py").write_text("# fixture\n", encoding="utf-8")
        self.git("add", "adapter.py")
        self.git("commit", "-m", "Add fixture adapter")
        (self.root / "staged.py").write_text("# fixture\n", encoding="utf-8")
        self.git("add", "staged.py")
        (self.root / "adapter.py").write_text("# changed fixture\n", encoding="utf-8")
        (self.root / "untracked file.py").write_text("# fixture\n", encoding="utf-8")
        result = self.preflight(expected_main=self.base)
        self.assertEqual(result["changed_paths"], ["adapter.py", "staged.py", "untracked file.py"])

    def test_core_edits_are_rejected_before_commit_even_when_worktree_reverses_index(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "CORE_PATH"):
            self.preflight(planned_paths=["core.txt"])
        (self.root / "core.txt").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "CORE_PATH"):
            self.preflight()
        self.git("add", "core.txt")
        (self.root / "core.txt").write_text("core\n", encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "CORE_PATH"):
            self.preflight()
        self.git("commit", "-m", "Fixture staged core edit")
        with self.assertRaisesRegex(ConfigurationError, "CORE_PATH"):
            self.preflight()

    def test_core_rename_is_rejected(self) -> None:
        self.git("mv", "core.txt", "adapter.txt")
        with self.assertRaisesRegex(ConfigurationError, "CORE_PATH"):
            self.preflight()

    def test_preflight_rejects_wrong_checkout_and_unsafe_planned_paths(self) -> None:
        for path in ("../core.txt", "/core.txt", "C:/core.txt", "a/../core.txt", "a//b", ""):
            with self.subTest(path=path), self.assertRaisesRegex(ConfigurationError, "PROMOTION_PATH"):
                self.preflight(planned_paths=[path])
        self.git("switch", "main")
        with self.assertRaisesRegex(ConfigurationError, "PROMOTION_BRANCH"):
            self.preflight()

    def test_base_drift_is_rejected_by_preflight_and_final_verification(self) -> None:
        (self.root / "adapter.py").write_text("# fixture\n", encoding="utf-8")
        self.git("add", "adapter.py")
        self.git("commit", "-m", "Add fixture adapter")
        valid = verify_sdk_promotion_commit(self.root, self.branch, expected_main=self.base)
        self.assertEqual(valid["base"], self.base)
        self.git("switch", "main")
        (self.root / "new-core.txt").write_text("core addition\n", encoding="utf-8")
        self.git("add", "new-core.txt")
        self.git("commit", "-m", "Advance fixture main")
        self.git("switch", self.branch)
        for working_tree in (False, True):
            with self.subTest(working_tree=working_tree), self.assertRaisesRegex(ConfigurationError, "main changed"):
                verify_sdk_promotion_commit(
                    self.root, self.branch, working_tree=working_tree, expected_main=self.base,
                )
        with self.assertRaisesRegex(ConfigurationError, "PROMOTION_BASE"):
            self.preflight()

    def test_cli_preflight_emits_reusable_base_and_rejects_conflicting_modes(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            code = main([
                "verify-sdk-promotion", "--sdk-root", str(self.root), "--branch", self.branch,
                "--working-tree", "--planned-path", "adapter.py", "--expected-main", self.base,
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["base"], self.base)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args([
                "verify-sdk-promotion", "--sdk-root", str(self.root), "--branch", self.branch,
                "--working-tree", "--commit", "HEAD",
            ])

    def test_mode_and_base_arguments_cannot_silently_weaken_checks(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "PROMOTION_PATH"):
            verify_sdk_promotion_commit(self.root, self.branch, planned_paths=["adapter.py"])
        with self.assertRaisesRegex(ConfigurationError, "PROMOTION_COMMIT"):
            verify_sdk_promotion_commit(self.root, self.branch, "main", working_tree=True)
        with self.assertRaisesRegex(ConfigurationError, "full commit hash"):
            self.preflight(expected_main="main")
