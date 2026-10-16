from __future__ import annotations

import unittest
from pathlib import Path

from game_l10n.cli import STATUS_ACTION_CONTRACTS


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
            "Do not inspect images for text",
            "fallback_required",
            "parr0t.control-envelope.v2",
            "docs/sdk-selection.md",
            "docs/control-plane-contract.md",
            "docs/translation-exchange.md",
            "docs/runtime-approval.md",
            "docs/filesystem-packaging.md",
            "docs/instance-release.md",
        ):
            self.assertIn(required, agents)

        self.assertLess(agents.count("docs/instance-release.md"), 3)
        self.assertLess(agents.count("docs/runtime-approval.md"), 3)

    def test_status_contract_routes_exist_in_the_repository(self) -> None:
        for action, contracts in STATUS_ACTION_CONTRACTS.items():
            for relative in contracts:
                with self.subTest(action=action, relative=relative):
                    self.assertTrue((ROOT / relative).is_file())


if __name__ == "__main__":
    unittest.main()
