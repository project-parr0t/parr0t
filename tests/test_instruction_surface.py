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
        self.assertIn("--requirement \"$requirements\"", workflow)

    def test_review_control_is_documented_as_a_deterministic_router(self) -> None:
        root = Path(__file__).resolve().parents[1]
        contract = (root / "docs" / "control-plane-contract.md").read_text(
            encoding="utf-8"
        )
        agents = (root / "AGENTS.md").read_text(encoding="utf-8")
        skill = (root / "skills" / "game-localization" / "SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("Deterministic review router", contract)
        self.assertIn("checkpoint_and_stop", contract)
        self.assertIn("must not create an\nad-hoc manifest", contract)
        self.assertIn("Keep review control deterministic", agents)
        self.assertIn("complete low-cost triage", agents)
        self.assertIn("selective strong review", agents)
        self.assertIn("Review control is a deterministic router", skill)

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
            "docs/branch-update-contract.md",
            "docs/control-plane-contract.md",
            "docs/detail-contract.md",
            "docs/translation-exchange.md",
            "docs/runtime-approval.md",
            "docs/filesystem-packaging.md",
            "docs/instance-release.md",
            "no model controllers or worker delegation",
            "binary-splits only failures",
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

    def test_detail_contract_cannot_override_core_or_serialize_a_batch(self) -> None:
        detail = (ROOT / "docs/detail-contract.md").read_text(encoding="utf-8")
        execution = (ROOT / "docs/execution-budget.md").read_text(encoding="utf-8")
        review = (ROOT / "docs/review-pipeline.md").read_text(encoding="utf-8")

        self.assertIn("supplemental-only", detail)
        self.assertIn("only executable detail contract", detail)
        self.assertIn("one semantic batch", detail)
        self.assertIn("E_BULK_EXECUTION_BUDGET_REQUIRED", detail)
        self.assertIn("one semantic model/API batch call", execution)
        self.assertIn("must not create or submit one prompt per candidate", review)
        self.assertIn("must not open the full ordered `source_artifact`", detail)
        self.assertIn("must not make another full game-tree copy", execution)
        self.assertIn("must not reopen the complete ordered source", review)
        self.assertIn("up to 2,048 candidates", review)
        self.assertIn("half the prior bound", detail)
        self.assertIn("Large-inventory startup budget", execution)
        self.assertIn("validates only tasks in the wave", execution)

    def test_runtime_and_worker_cost_claims_stay_evidence_bound(self) -> None:
        coverage = (ROOT / "docs/coverage-gates.md").read_text(encoding="utf-8")
        deepseek = (ROOT / "docs/codex-app-deepseek.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("must not be reported as “working in game”", coverage)
        self.assertIn("must not open the full ordered source", deepseek)

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

    def test_small_correction_and_work_lane_contracts_are_bounded(self) -> None:
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        minimal = (ROOT / "docs" / "minimal-corrections.md").read_text(
            encoding="utf-8"
        )
        execution = (ROOT / "docs" / "execution-budget.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("Bind every write to the current work lane", agents)
        self.assertIn("combined set must remain at or below 32", minimal)
        self.assertIn("one ordered semantic batch", minimal)
        self.assertIn("normal ceiling is two semantic calls", minimal)
        self.assertIn("A supervision or analysis lane", execution)
        self.assertIn("enumerate the outgoing commits", execution)


if __name__ == "__main__":
    unittest.main()
