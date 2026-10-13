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

    def test_review_issue_codes_cover_effect_not_only_literal_meaning(self) -> None:
        self.assertIn("literalism", REVIEW_ISSUE_CODES)
        self.assertIn("reference_loss", REVIEW_ISSUE_CODES)
        self.assertIn("tone_rhythm", REVIEW_ISSUE_CODES)


if __name__ == "__main__":
    unittest.main()
