from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.errors import ProviderError
from game_l10n.models import ContextPack, GlossaryEntry, ProjectContext, Segment
from game_l10n.orchestrator import LocalizationOrchestrator, OrchestratorConfig
from game_l10n.providers import ProviderRegistry, ScriptedProvider


class OrchestratorTests(unittest.TestCase):
    def test_deferred_run_resumes_after_completed_primary_stage(self) -> None:
        first_events: list[str] = []

        def target_id(kwargs) -> str:
            return json.loads(kwargs["user"])["translation_target"]["id"]

        def first_luna(**kwargs):
            segment_id = target_id(kwargs)
            first_events.append(f"luna:{segment_id}")
            if segment_id == "fixture-1":
                return {"status": "refused", "text": "", "reason": "sensitive details"}
            return {"status": "success", "text": "TARGET_2", "reason": ""}

        def interrupted_deepseek(**kwargs):
            first_events.append(f"deepseek:{target_id(kwargs)}")
            raise KeyboardInterrupt("synthetic interruption")

        config = OrchestratorConfig(
            ["luna", "deepseek"],
            ["sol", "deepseek"],
            retry_refusals_once=False,
            defer_fallbacks_until_primary_complete=True,
            preserve_source_on_failure=True,
        )
        project = ProjectContext("TEST", "src", "dst")
        context = ContextPack(story_summary="STORY")
        segments = [Segment("fixture-1", "SOURCE_1"), Segment("fixture-2", "SOURCE_2")]

        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            first_registry = ProviderRegistry(
                {
                    "luna": ScriptedProvider("luna", first_luna),
                    "deepseek": ScriptedProvider("deepseek", interrupted_deepseek),
                    "sol": ScriptedProvider("sol", lambda **_: {}),
                }
            )
            first = LocalizationOrchestrator(
                registry=first_registry,
                config=config,
                project=project,
                context_pack=context,
                work_dir=work_dir,
            )
            with self.assertRaises(KeyboardInterrupt):
                first.translate_many(segments)

            second_events: list[str] = []

            def second_luna(**kwargs):
                second_events.append(f"luna:{target_id(kwargs)}")
                return {"status": "success", "text": "UNEXPECTED", "reason": ""}

            def second_deepseek(**kwargs):
                segment_id = target_id(kwargs)
                second_events.append(f"deepseek:{segment_id}")
                return {"status": "success", "text": "TARGET_1", "reason": ""}

            def sol(**kwargs):
                payload = json.loads(kwargs["user"])
                second_events.append(f"sol:{payload['translation_target']['id']}")
                return {
                    "decision": "approved",
                    "text": payload["candidate_translation"],
                    "meaning_preserved": True,
                    "issues": [],
                    "reason": "",
                }

            second_registry = ProviderRegistry(
                {
                    "luna": ScriptedProvider("luna", second_luna),
                    "deepseek": ScriptedProvider("deepseek", second_deepseek),
                    "sol": ScriptedProvider("sol", sol),
                }
            )
            second = LocalizationOrchestrator(
                registry=second_registry,
                config=config,
                project=project,
                context_pack=context,
                work_dir=work_dir,
            )
            results = second.translate_many(segments)

            self.assertEqual(list((work_dir / ".game-l10n" / "deferred").glob("*.json")), [])

        self.assertEqual(
            first_events,
            ["luna:fixture-1", "luna:fixture-2", "deepseek:fixture-1"],
        )
        self.assertEqual(
            second_events,
            ["deepseek:fixture-1", "sol:fixture-1", "sol:fixture-2"],
        )
        self.assertEqual([result.status for result in results], ["reviewed", "reviewed"])

    def test_control_plane_does_not_receive_provider_refusal_explanation(self) -> None:
        refusal = ScriptedProvider(
            "luna",
            lambda **_: {
                "status": "refused",
                "text": "",
                "reason": "PRIVATE_DIALOGUE and provider explanation",
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            orchestrator = LocalizationOrchestrator(
                registry=ProviderRegistry({"luna": refusal}),
                config=OrchestratorConfig(
                    ["luna"],
                    [],
                    require_review=False,
                    retry_refusals_once=False,
                    defer_fallbacks_until_primary_complete=True,
                    preserve_source_on_failure=True,
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=work_dir,
            )
            result = orchestrator.translate_many(
                [Segment("fixture-1", "PRIVATE_DIALOGUE")]
            )[0]
            audit = (work_dir / ".game-l10n" / "audit.jsonl").read_text(encoding="utf-8")

        self.assertEqual(result.status, "source_preserved")
        self.assertEqual(result.attempts[0].reason, "provider_refusal")
        self.assertNotIn("provider explanation", audit)
        self.assertNotIn("PRIVATE_DIALOGUE", audit)

    def test_deferred_review_keeps_same_speaker_style_memory(self) -> None:
        anchor_counts: list[int] = []

        def translator(**kwargs):
            segment_id = json.loads(kwargs["user"])["translation_target"]["id"]
            return {"status": "success", "text": f"TARGET_{segment_id}", "reason": ""}

        def reviewer(**kwargs):
            payload = json.loads(kwargs["user"])
            anchor_counts.append(len(payload["approved_same_speaker_style_anchors"]))
            return {
                "decision": "approved",
                "text": payload["candidate_translation"],
                "meaning_preserved": True,
                "issues": [],
                "reason": "",
            }

        providers = [ScriptedProvider("luna", translator), ScriptedProvider("sol", reviewer)]
        registry = ProviderRegistry({provider.name: provider for provider in providers})
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    ["luna"],
                    ["sol"],
                    max_workers=4,
                    defer_fallbacks_until_primary_complete=True,
                    use_style_memory=True,
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
            )
            results = orchestrator.translate_many(
                [
                    Segment("fixture-1", "SOURCE_1", speaker="fixture-speaker"),
                    Segment("fixture-2", "SOURCE_2", speaker="fixture-speaker"),
                ]
            )

        self.assertEqual([result.status for result in results], ["reviewed", "reviewed"])
        self.assertEqual(anchor_counts, [0, 1])

    def test_deferred_fallback_finishes_primary_pass_before_fallback(self) -> None:
        events: list[str] = []

        def target_id(user: str) -> str:
            return json.loads(user)["translation_target"]["id"]

        def luna_handler(**kwargs):
            segment_id = target_id(kwargs["user"])
            events.append(f"luna:translate:{segment_id}")
            if segment_id == "fixture-1":
                return {"status": "refused", "text": "", "reason": "policy"}
            return {"status": "success", "text": f"TARGET_{segment_id}", "reason": ""}

        def deepseek_handler(**kwargs):
            segment_id = target_id(kwargs["user"])
            events.append(f"deepseek:translate:{segment_id}")
            return {"status": "success", "text": f"TARGET_{segment_id}", "reason": ""}

        def sol_handler(**kwargs):
            payload = json.loads(kwargs["user"])
            segment_id = payload["translation_target"]["id"]
            events.append(f"sol:review:{segment_id}")
            return {
                "decision": "approved",
                "text": payload["candidate_translation"],
                "meaning_preserved": True,
                "issues": [],
                "reason": "",
            }

        providers = [
            ScriptedProvider("luna", luna_handler),
            ScriptedProvider("deepseek", deepseek_handler),
            ScriptedProvider("sol", sol_handler),
        ]
        registry = ProviderRegistry({provider.name: provider for provider in providers})
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    ["luna", "deepseek"],
                    ["sol", "deepseek"],
                    retry_refusals_once=False,
                    defer_fallbacks_until_primary_complete=True,
                    preserve_source_on_failure=True,
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
            )
            results = orchestrator.translate_many(
                [Segment("fixture-1", "SOURCE_1"), Segment("fixture-2", "SOURCE_2")]
            )

        self.assertEqual(
            events,
            [
                "luna:translate:fixture-1",
                "luna:translate:fixture-2",
                "deepseek:translate:fixture-1",
                "sol:review:fixture-1",
                "sol:review:fixture-2",
            ],
        )
        self.assertEqual([result.status for result in results], ["reviewed", "reviewed"])
        self.assertTrue(
            any(item.stage == "translation-fallback-queued" for item in results[0].attempts)
        )

    def test_review_refusal_is_isolated_and_exhaustion_preserves_source(self) -> None:
        events: list[str] = []

        def payload(kwargs):
            return json.loads(kwargs["user"])

        def luna_handler(**kwargs):
            segment_id = payload(kwargs)["translation_target"]["id"]
            events.append(f"luna:translate:{segment_id}")
            return {"status": "success", "text": f"TARGET_{segment_id}", "reason": ""}

        def sol_handler(**kwargs):
            data = payload(kwargs)
            segment_id = data["translation_target"]["id"]
            events.append(f"sol:review:{segment_id}")
            if segment_id == "fixture-1":
                return {
                    "decision": "refused",
                    "text": "",
                    "meaning_preserved": False,
                    "issues": [],
                    "reason": "policy",
                }
            return {
                "decision": "approved",
                "text": data["candidate_translation"],
                "meaning_preserved": True,
                "issues": [],
                "reason": "",
            }

        def deepseek_handler(**kwargs):
            segment_id = payload(kwargs)["translation_target"]["id"]
            events.append(f"deepseek:review:{segment_id}")
            raise ProviderError("quota exhausted")

        providers = [
            ScriptedProvider("luna", luna_handler),
            ScriptedProvider("sol", sol_handler),
            ScriptedProvider("deepseek", deepseek_handler),
        ]
        registry = ProviderRegistry({provider.name: provider for provider in providers})
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    ["luna", "deepseek"],
                    ["sol", "deepseek"],
                    retry_refusals_once=False,
                    defer_fallbacks_until_primary_complete=True,
                    preserve_source_on_failure=True,
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
            )
            results = orchestrator.translate_many(
                [Segment("fixture-1", "SOURCE_1"), Segment("fixture-2", "SOURCE_2")]
            )

        self.assertEqual(
            events,
            [
                "luna:translate:fixture-1",
                "luna:translate:fixture-2",
                "sol:review:fixture-1",
                "sol:review:fixture-2",
                "deepseek:review:fixture-1",
            ],
        )
        self.assertEqual(results[0].status, "source_preserved")
        self.assertEqual(results[0].text, "SOURCE_1")
        self.assertEqual(results[1].status, "reviewed")
        self.assertTrue(any(item.stage == "review-fallback-queued" for item in results[0].attempts))
        self.assertIn("provider_refusal", {item.reason for item in results[0].attempts})
        self.assertIn("quota_exhausted", {item.reason for item in results[0].attempts})

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
