from __future__ import annotations

import unittest
from pathlib import Path

from game_l10n.cli import STATUS_ACTION_CONTRACTS, _status_contract_set


ROOT = Path(__file__).resolve().parents[1]


class InstructionSurfaceTests(unittest.TestCase):
    def test_ci_installs_sdk_requirements_before_collecting_tests(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(
            encoding="utf-8"
        )
        dependency_install = workflow.index("requirements-*.txt")
        test_collection = workflow.index("python -m unittest discover")

        self.assertLess(dependency_install, test_collection)
        self.assertIn("-name 'requirements.txt'", workflow)
        self.assertIn("--requirement \"$requirements\"", workflow)

    def test_always_loaded_instructions_stay_compact(self) -> None:
        limits = {
            "AGENTS.md": 1200,
            "skills/game-localization/SKILL.md": 700,
            "docs/agent-usage.md": 1100,
        }

        for relative, limit in limits.items():
            text = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(relative=relative):
                self.assertLessEqual(len(text.split()), limit)


    def test_status_contract_routes_exist_in_the_repository(self) -> None:
        for action, contracts in STATUS_ACTION_CONTRACTS.items():
            for relative in contracts:
                with self.subTest(action=action, relative=relative):
                    self.assertTrue((ROOT / relative).is_file())

    def test_status_contract_set_is_stable_and_generation_aware(self) -> None:
        first = _status_contract_set(
            str(ROOT), "run_primary_translation_review_pipeline", 3
        )
        second = _status_contract_set(
            str(ROOT), "run_primary_translation_review_pipeline", 3
        )
        next_generation = _status_contract_set(
            str(ROOT), "run_primary_translation_review_pipeline", 4
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first["digest"]), 64)
        self.assertEqual(first["digest"], next_generation["digest"])
        self.assertNotEqual(
            first["control_generation"], next_generation["control_generation"]
        )
        self.assertEqual(
            first["reload_policy"],
            "digest_change_or_new_control_generation_only",
        )


if __name__ == "__main__":
    unittest.main()
