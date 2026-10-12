from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.context_analysis import ContextAnalyzer, analyze_with_cache
from game_l10n.models import Segment
from game_l10n.providers import ProviderRegistry, ScriptedProvider


def successful_context(**kwargs):
    payload = json.loads(kwargs["user"])
    ids = [item["id"] for item in payload["lines"]]
    return {
        "status": "success",
        "reason": "",
        "story_summary": "FLOW:" + ",".join(ids),
        "global_style_rules": ["CONSISTENT"],
        "scenes": [
            {"scene": "S", "summary": "SCENE", "tone": "TONE", "relationship_state": "STATE"}
        ],
        "characters": [
            {
                "name": "A",
                "personality": "RESERVED",
                "voice": "SHORT",
                "formality": "FORMAL",
                "address_rules": "TITLE",
                "speech_rules": ["RULE"],
                "relationships": "REL",
                "evidence_line_ids": ids,
            }
        ],
        "relationship_timeline": ["EVENT"],
    }


class ContextAnalysisTests(unittest.TestCase):
    def test_reads_every_line_and_uses_provider_fallback(self) -> None:
        refusing = ScriptedProvider(
            "refuse", lambda **_: {"status": "refused", "reason": "policy"}
        )
        success = ScriptedProvider("success", successful_context)
        registry = ProviderRegistry({"refuse": refusing, "success": success})
        analyzer = ContextAnalyzer(registry, ["refuse", "success"], max_chunk_chars=100)
        segments = [Segment(str(index), "X" * 45, speaker="A", scene="S") for index in range(5)]
        pack = analyzer.analyze(segments)
        evidence = pack.character_guides["A"]["evidence_line_ids"]
        self.assertEqual(evidence, [str(index) for index in range(5)])
        self.assertGreater(success.call_count, 1)
        self.assertEqual(refusing.call_count, success.call_count)

    def test_identical_context_is_cached(self) -> None:
        provider = ScriptedProvider("context", successful_context)
        analyzer = ContextAnalyzer(ProviderRegistry({"context": provider}), ["context"])
        segments = [Segment("1", "SOURCE", speaker="A", scene="S")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, first_cached = analyze_with_cache(
                analyzer=analyzer,
                segments=segments,
                output_path=root / "context.json",
                state_path=root / "state.json",
            )
            second, second_cached = analyze_with_cache(
                analyzer=analyzer,
                segments=segments,
                output_path=root / "context.json",
                state_path=root / "state.json",
            )
        self.assertFalse(first_cached)
        self.assertTrue(second_cached)
        self.assertEqual(first.story_summary, second.story_summary)
        self.assertEqual(provider.call_count, 1)


if __name__ == "__main__":
    unittest.main()
