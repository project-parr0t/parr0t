from __future__ import annotations

import unittest
from pathlib import Path

from game_l10n.cli import STATUS_ACTION_CONTRACTS, _status_contract_set


ROOT = Path(__file__).resolve().parents[1]


class InstructionSurfaceTests(unittest.TestCase):
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

    def test_core_contract_routes_details_instead_of_copying_them(self) -> None:
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

        for required in (
            "Resolve each `required_contracts` entry against `contract_base`",
            "before accessing game-root contents",
            "game-l10n enter-control-plane",
            "game-l10n verify-sdk-promotion",
            "Do not inspect images for text",
            "fallback_required",
            "parr0t.control-envelope.v2",
            "docs/sdk-selection.md",
            "docs/control-plane-contract.md",
            "docs/translation-exchange.md",
            "docs/runtime-approval.md",
            "docs/filesystem-packaging.md",
            "docs/instance-release.md",
            "no model controllers or worker delegation",
        ):
            self.assertIn(required, agents)

        self.assertLess(agents.count("docs/instance-release.md"), 3)
        self.assertLess(agents.count("docs/runtime-approval.md"), 3)

    def test_public_entry_prompts_keep_the_parent_out_of_game_content(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        prompts = (ROOT / "docs/prompt-examples.md").read_text(encoding="utf-8")

        self.assertEqual(readme.count("text-free control-plane"), 3)
        self.assertIn("Before listing, opening, searching, or reading", prompts)
        self.assertIn("existing-locale detection and engine/version discovery", prompts)
        self.assertIn("establish_text_free_control_plane", STATUS_ACTION_CONTRACTS)

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
