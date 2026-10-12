from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from game_l10n.models import ContextPack, GlossaryEntry, ProjectContext, Segment
from game_l10n.orchestrator import LocalizationOrchestrator, OrchestratorConfig
from game_l10n.providers import ProviderRegistry, ScriptedProvider


class OrchestratorTests(unittest.TestCase):
    def test_translation_and_review_refusals_fall_back(self) -> None:
        first_translator = ScriptedProvider(
            "t-refuse", lambda **_: {"status": "refused", "text": "", "reason": "policy"}
        )
        second_translator = ScriptedProvider(
            "t-ok", lambda **_: {"status": "success", "text": "TARGET {name}", "reason": ""}
        )
        first_reviewer = ScriptedProvider(
            "r-refuse",
            lambda **_: {
                "decision": "refused",
                "text": "",
                "meaning_preserved": False,
                "issues": [],
                "reason": "policy",
            },
        )
        second_reviewer = ScriptedProvider(
            "r-ok",
            lambda **_: {
                "decision": "approved",
                "text": "TARGET {name}",
                "meaning_preserved": True,
                "issues": [],
                "reason": "",
            },
        )
        registry = ProviderRegistry(
            {provider.name: provider for provider in [first_translator, second_translator, first_reviewer, second_reviewer]}
        )
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    translator_chain=["t-refuse", "t-ok"], reviewer_chain=["r-refuse", "r-ok"]
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(
                    story_summary="STORY",
                    character_guides={"A": {"formality": "formal", "speech_rules": ["RULE"]}},
                ),
                work_dir=Path(directory),
            )
            result = orchestrator.translate_one(Segment("1", "SOURCE {name}", speaker="A"))

        self.assertEqual(result.status, "reviewed")
        self.assertEqual(result.translator, "t-ok")
        self.assertEqual(result.reviewer, "r-ok")
        routed = [
            (item.stage, item.provider, item.status)
            for item in result.attempts
            if item.stage.startswith("translate") or item.stage.startswith("review")
        ]
        self.assertEqual(
            routed,
            [
                ("translate", "t-refuse", "refused"),
                ("translate-refusal-retry", "t-refuse", "refused"),
                ("translate", "t-ok", "success"),
                ("review", "r-refuse", "refused"),
                ("review-refusal-retry", "r-refuse", "refused"),
                ("review", "r-ok", "approved"),
            ],
        )

    def test_integrity_failure_moves_to_next_translator(self) -> None:
        bad = ScriptedProvider(
            "bad", lambda **_: {"status": "success", "text": "TARGET", "reason": ""}
        )
        good = ScriptedProvider(
            "good", lambda **_: {"status": "success", "text": "TARGET {slot}", "reason": ""}
        )
        review = ScriptedProvider(
            "review",
            lambda **_: {
                "decision": "approved",
                "text": "TARGET {slot}",
                "meaning_preserved": True,
                "issues": [],
                "reason": "",
            },
        )
        registry = ProviderRegistry({item.name: item for item in [bad, good, review]})
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(["bad", "good"], ["review"]),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
            )
            result = orchestrator.translate_one(Segment("1", "SOURCE {slot}"))
        self.assertEqual(result.status, "reviewed")
        self.assertEqual(result.translator, "good")
        self.assertTrue(any(item.stage == "integrity" for item in result.attempts))

    def test_glossary_and_script_intrusion_are_rejected(self) -> None:
        bad = ScriptedProvider(
            "bad",
            lambda **_: {"status": "success", "text": "WRONG <script>alert(1)</script>", "reason": ""},
        )
        registry = ProviderRegistry({"bad": bad})
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(["bad"], [], require_review=False),
                project=ProjectContext("TEST", "src", "dst"),
                glossary=[GlossaryEntry("TERM", "FIXED")],
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
            )
            result = orchestrator.translate_one(Segment("1", "TERM"))
        self.assertEqual(result.status, "failed")
        issue_text = " ".join(issue for record in result.attempts for issue in record.issues)
        self.assertIn("glossary-missing", issue_text)
        self.assertIn("script-intrusion", issue_text)


if __name__ == "__main__":
    unittest.main()
