from __future__ import annotations

import json
import unittest

from game_l10n.models import CharacterProfile, ContextPack, ProjectContext, Segment
from game_l10n.prompts import translation_prompt


class PromptTests(unittest.TestCase):
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
        self.assertIn("not a content generator", system)
        self.assertIn("exactly one target", system)


if __name__ == "__main__":
    unittest.main()
