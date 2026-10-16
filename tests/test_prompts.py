from __future__ import annotations

import json
import unittest

from game_l10n.models import CharacterProfile, ContextPack, GlossaryEntry, ProjectContext, Segment
from game_l10n.prompts import REVIEW_ISSUE_CODES, review_judge_prompt, translation_prompt


class PromptTests(unittest.TestCase):
    def test_compact_review_uses_focused_context_without_losing_story_or_voice(self) -> None:
        system, user = review_judge_prompt(
            ProjectContext("FIXTURE", "src", "dst"),
            Segment("fixture-id", "SOURCE_TERM", speaker="A", scene="SCENE_A"),
            "TARGET_TERM",
            [
                GlossaryEntry("SOURCE_TERM", "TARGET_TERM"),
                GlossaryEntry("UNRELATED", "UNRELATED_TARGET"),
            ],
            CharacterProfile("A", voice="VOICE_A"),
            ContextPack(
                story_summary="STORY",
                scene_summaries={"SCENE_A": {"tone": "TENSE"}},
                character_guides={"A": {"voice": "VOICE_A"}},
                relationship_timeline=("A RELATIONSHIP", "B UNRELATED"),
                reference_cues=(
                    {
                        "kind": "parody",
                        "scene": "SCENE_A",
                        "evidence_line_ids": ["fixture-id"],
                        "recognition_cue": "FIXTURE_RECOGNITION_PATTERN",
                        "translation_goal": "PRESERVE_FIXTURE_EFFECT",
                        "confidence": "high",
                    },
                    {
                        "kind": "wordplay",
                        "scene": "SCENE_B",
                        "evidence_line_ids": ["unrelated-id"],
                        "recognition_cue": "UNRELATED_PATTERN",
                        "translation_goal": "UNRELATED_GOAL",
                        "confidence": "high",
                    },
                ),
            ),
        )
        payload = json.loads(user)

        self.assertEqual(payload["narrative_context"]["story_summary"], "STORY")
        self.assertEqual(payload["character"]["voice"], "VOICE_A")
        self.assertEqual(len(payload["glossary"]), 1)
        self.assertEqual(
            payload["narrative_context"]["relationship_timeline"], ["A RELATIONSHIP"]
        )
        self.assertEqual(
            payload["narrative_context"]["relevant_reference_cues"][0]["recognition_cue"],
            "FIXTURE_RECOGNITION_PATTERN",
        )
        self.assertEqual(len(payload["narrative_context"]["relevant_reference_cues"]), 1)
        self.assertIn("compact", system)
        self.assertIn("Do not return, quote, or rewrite", system)
        self.assertIn("mechanically literal", system)

    def test_single_target_keeps_full_context_as_read_only_evidence(self) -> None:
        system, user = translation_prompt(
            ProjectContext("FIXTURE", "src", "dst"),
            Segment(
                "fixture-id",
                "SOURCE_TARGET",
                speaker="fixture-speaker",
                scene="fixture-scene",
                previous="SOURCE_PREVIOUS",
                next="SOURCE_NEXT",
                metadata={"text_type": "quest_title"},
            ),
            [],
            CharacterProfile("fixture-speaker", voice="FIXTURE_VOICE"),
            ContextPack(
                story_summary="FIXTURE_STORY",
                scene_summaries={"fixture-scene": {"tone": "FIXTURE_TONE"}},
                character_guides={"fixture-speaker": {"voice": "FIXTURE_VOICE"}},
                relationship_timeline=("FIXTURE_RELATIONSHIP",),
            ),
        )
        payload = json.loads(user)

        self.assertEqual(payload["translation_target"]["source"], "SOURCE_TARGET")
        self.assertEqual(
            payload["context_only_do_not_translate"]["previous"], "SOURCE_PREVIOUS"
        )
        self.assertEqual(payload["narrative_context"]["story_summary"], "FIXTURE_STORY")
        self.assertEqual(payload["character"]["voice"], "FIXTURE_VOICE")
        self.assertEqual(
            payload["context_only_do_not_translate"]["metadata"]["text_type"],
            "quest_title",
        )
        self.assertIn("not a content generator", system)
        self.assertIn("exactly one target", system)
        self.assertIn("idiomatic", system)

    def test_optional_context_identity_nulls_are_normalized_before_prompting(self) -> None:
        segment = Segment(
            "fixture-id",
            "SOURCE_TARGET",
            speaker=None,  # type: ignore[arg-type]
            scene=None,  # type: ignore[arg-type]
            previous=None,  # type: ignore[arg-type]
            next=None,  # type: ignore[arg-type]
        )
        _, user = translation_prompt(
            ProjectContext("FIXTURE", "src", "dst"),
            segment,
            [],
            None,
        )
        context = json.loads(user)["context_only_do_not_translate"]

        self.assertEqual(segment.speaker, "")
        self.assertEqual(segment.scene, "")
        self.assertEqual(context["speaker"], "")
        self.assertEqual(context["scene"], "")
        self.assertEqual(context["previous"], "")
        self.assertEqual(context["next"], "")

    def test_review_issue_codes_cover_effect_not_only_literal_meaning(self) -> None:
        self.assertIn("literalism", REVIEW_ISSUE_CODES)
        self.assertIn("reference_loss", REVIEW_ISSUE_CODES)
        self.assertIn("tone_rhythm", REVIEW_ISSUE_CODES)
        self.assertIn("function", REVIEW_ISSUE_CODES)
        self.assertIn("knowledge_order", REVIEW_ISSUE_CODES)
        self.assertIn("slot_composition", REVIEW_ISSUE_CODES)

    def test_ui_slot_contract_is_preserved_for_translation_and_review(self) -> None:
        segment = Segment(
            "ui-state",
            "SOURCE_STATE",
            metadata={
                "text_type": "setting_state",
                "ui_slot_role": "state_value",
                "ui_state_semantics": "enabled",
                "ui_composed_with_ids": ["ui-label"],
                "ui_render_pattern": "{label}: {value}",
                "ui_fixed_target_prefix": "TARGET_LABEL: ",
            },
        )
        translation_system, translation_user = translation_prompt(
            ProjectContext("FIXTURE", "src", "dst"), segment, [], None
        )
        review_system, review_user = review_judge_prompt(
            ProjectContext("FIXTURE", "src", "dst"), segment, "TARGET_STATE", [], None
        )

        self.assertEqual(
            json.loads(translation_user)["context_only_do_not_translate"]["metadata"][
                "ui_slot_role"
            ],
            "state_value",
        )
        self.assertEqual(
            json.loads(review_user)["context_only_do_not_translate"]["metadata"][
                "ui_state_semantics"
            ],
            "enabled",
        )
        self.assertIn("translate only this slot", translation_system)
        self.assertIn("slot_composition", review_system)

    def test_functional_and_knowledge_context_are_focused_by_evidence(self) -> None:
        system, user = translation_prompt(
            ProjectContext("FIXTURE", "src", "dst"),
            Segment("ui-1", "Apply", metadata={"text_type": "button"}),
            [],
            None,
            ContextPack(
                context_mode="hybrid",
                functional_summary="UI FLOW",
                knowledge_summary="GUIDE FLOW",
                global_style_rules=("CONCISE",),
                terminology_rules=("APPLY IS AN ACTION",),
                screen_flows={
                    "OPTIONS": {"evidence_line_ids": ["ui-1"], "purpose": "SETTINGS"},
                    "OTHER": {"evidence_line_ids": ["ui-2"], "purpose": "OTHER"},
                },
                knowledge_nodes={
                    "APPLY HELP": {"evidence_line_ids": ["ui-1"], "summary": "HOW TO APPLY"},
                    "OTHER HELP": {"evidence_line_ids": ["ui-2"], "summary": "OTHER"},
                },
                reference_cues=(
                    {
                        "kind": "wordplay",
                        "scene": "",
                        "evidence_line_ids": ["ui-1"],
                        "recognition_cue": "FIXTURE CUE",
                        "translation_goal": "KEEP EFFECT",
                        "confidence": "high",
                    },
                ),
            ),
        )
        payload = json.loads(user)

        self.assertEqual(payload["localization_context"]["mode"], "hybrid")
        self.assertEqual(
            payload["localization_context"]["relevant_reference_cues"][0]["kind"],
            "wordplay",
        )
        self.assertEqual(len(payload["functional_context"]["relevant_screens"]), 1)
        self.assertEqual(len(payload["knowledge_context"]["relevant_nodes"]), 1)
        self.assertIsNone(payload["narrative_context"])
        self.assertIn("functional text", system)

    def test_translation_drops_duplicated_context_metadata_and_focuses_evidence(self) -> None:
        _, user = translation_prompt(
            ProjectContext("FIXTURE", "src", "dst"),
            Segment(
                "fixture-id",
                "SOURCE_TERM",
                speaker="A",
                scene="SCENE_A",
                metadata={
                    "text_type": "dialogue",
                    "focused_context_pack": {"bulk": "X" * 100_000},
                    "target_context_attachment": {"bulk": "Y" * 100_000},
                },
            ),
            [
                GlossaryEntry("SOURCE_TERM", "TARGET_TERM"),
                GlossaryEntry("UNRELATED", "UNRELATED_TARGET"),
            ],
            CharacterProfile("A", voice="VOICE_A"),
            ContextPack(
                story_summary="STORY",
                relationship_timeline=("A RELATIONSHIP", "B UNRELATED"),
            ),
        )
        payload = json.loads(user)
        metadata = payload["context_only_do_not_translate"]["metadata"]

        self.assertEqual(metadata, {"text_type": "dialogue"})
        self.assertEqual(payload["glossary"][0]["source"], "SOURCE_TERM")
        self.assertEqual(len(payload["glossary"]), 1)
        self.assertEqual(
            payload["narrative_context"]["relationship_timeline"],
            ["A RELATIONSHIP"],
        )
        self.assertEqual(list(payload)[-1], "translation_target")
        self.assertLess(len(user), 5_000)


if __name__ == "__main__":
    unittest.main()
