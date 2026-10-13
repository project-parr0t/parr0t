from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.context_analysis import ContextAnalyzer, analyze_with_cache, infer_context_mode
from game_l10n.errors import ProviderError
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
        "reference_cues": [],
    }


def successful_functional_context(**kwargs):
    payload = json.loads(kwargs["user"])
    ids = [item["id"] for item in payload["lines"]]
    return {
        "status": "success",
        "reason": "",
        "functional_summary": "UI FLOW",
        "global_style_rules": ["CONCISE"],
        "terminology_rules": ["KEEP ACTION VERBS STABLE"],
        "screens": [
            {
                "screen": "OPTIONS",
                "purpose": "CHANGE SETTINGS",
                "actions": ["APPLY"],
                "states": ["ENABLED"],
                "constraints": ["SHORT LABEL"],
                "evidence_line_ids": ids,
            }
        ],
        "features": [],
        "reference_cues": [],
    }


def successful_knowledge_context(**kwargs):
    payload = json.loads(kwargs["user"])
    ids = [item["id"] for item in payload["lines"]]
    return {
        "status": "success",
        "reason": "",
        "knowledge_summary": "ORDERED GUIDE",
        "global_style_rules": ["INSTRUCTIONAL"],
        "terminology_rules": ["USE ONE TERM"],
        "knowledge_nodes": [
            {
                "node": "BASICS",
                "kind": "guide",
                "summary": "LEARN FIRST",
                "prerequisites": ["INSTALL"],
                "terminology": ["FEATURE"],
                "constraints": ["KEEP ORDER"],
                "evidence_line_ids": ids,
            }
        ],
        "reference_cues": [],
    }


class ContextAnalysisTests(unittest.TestCase):
    def test_auto_mode_treats_quest_metadata_as_narrative_context(self) -> None:
        self.assertEqual(
            infer_context_mode([Segment("title-1", "SOURCE_TITLE", metadata={"text_type": "quest_title"})]),
            "narrative",
        )

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

    def test_operational_failure_uses_fallback_and_records_successful_route(self) -> None:
        failing = ScriptedProvider("capacity", lambda **_: (_ for _ in ()).throw(RuntimeError("capacity")))
        success = ScriptedProvider("success", successful_context)
        analyzer = ContextAnalyzer(
            ProviderRegistry({"capacity": failing, "success": success}),
            ["capacity", "success"],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analyze_with_cache(
                analyzer=analyzer,
                segments=[Segment("synthetic-1", "SOURCE", speaker="A", scene="S")],
                output_path=root / "context.json",
                state_path=root / "state.json",
            )
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(state["status"], "success")
        self.assertEqual(state["execution"]["completed_parts"], 1)
        self.assertEqual(state["execution"]["fallback_part_count"], 1)
        self.assertEqual(state["execution"]["parts"][0]["provider"], "success")

    def test_all_context_routes_failing_writes_no_success_artifact(self) -> None:
        failing = ScriptedProvider("capacity", lambda **_: (_ for _ in ()).throw(RuntimeError("capacity")))
        analyzer = ContextAnalyzer(ProviderRegistry({"capacity": failing}), ["capacity"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "context.json"
            state = root / "state.json"
            with self.assertRaisesRegex(ProviderError, "All context-analysis routes failed"):
                analyze_with_cache(
                    analyzer=analyzer,
                    segments=[Segment("synthetic-1", "SOURCE", speaker="A", scene="S")],
                    output_path=output,
                    state_path=state,
                )
            self.assertFalse(output.exists())
            self.assertFalse(state.exists())

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

    def test_preserves_supported_reference_cues_and_segment_metadata(self) -> None:
        captured = {}

        def context_with_cue(**kwargs):
            payload = json.loads(kwargs["user"])
            captured.update(payload["lines"][0])
            result = successful_context(**kwargs)
            result["reference_cues"] = [
                {
                    "kind": "parody",
                    "scene": "S",
                    "evidence_line_ids": ["fixture-title"],
                    "recognition_cue": "FIXTURE_RECOGNITION_PATTERN",
                    "translation_goal": "PRESERVE_FIXTURE_EFFECT",
                    "confidence": "high",
                }
            ]
            return result

        provider = ScriptedProvider("context", context_with_cue)
        analyzer = ContextAnalyzer(ProviderRegistry({"context": provider}), ["context"])
        pack = analyzer.analyze(
            [
                Segment(
                    "fixture-title",
                    "SOURCE_TITLE",
                    speaker="A",
                    scene="S",
                    metadata={"text_type": "quest_title"},
                )
            ]
        )

        self.assertEqual(captured["metadata"], {"text_type": "quest_title"})
        self.assertEqual(pack.reference_cues[0]["kind"], "parody")

    def test_context_gate_rejects_missing_named_character_coverage(self) -> None:
        def incomplete(**_kwargs):
            payload = successful_context(user='{"lines": [{"id": "fixture-1"}]}')
            payload["characters"] = []
            return payload

        provider = ScriptedProvider("context", incomplete)
        analyzer = ContextAnalyzer(ProviderRegistry({"context": provider}), ["context"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ProviderError, "character coverage"):
                analyze_with_cache(
                    analyzer=analyzer,
                    segments=[Segment("fixture-1", "SOURCE", speaker="A", scene="S")],
                    output_path=root / "context.json",
                    state_path=root / "state.json",
                )

    def test_auto_mode_uses_functional_context_without_narrative_requirements(self) -> None:
        captured = {}

        def functional(**kwargs):
            captured["schema_name"] = kwargs["schema_name"]
            captured["payload"] = json.loads(kwargs["user"])
            return successful_functional_context(**kwargs)

        provider = ScriptedProvider("context", functional)
        analyzer = ContextAnalyzer(ProviderRegistry({"context": provider}), ["context"])
        segments = [
            Segment("ui-1", "Apply", metadata={"text_type": "button"}),
            Segment("ui-2", "Enabled", metadata={"text_type": "status_label"}),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pack, _ = analyze_with_cache(
                analyzer=analyzer,
                segments=segments,
                output_path=root / "context.json",
                state_path=root / "state.json",
            )

        self.assertEqual(pack.context_mode, "functional")
        self.assertFalse(pack.story_summary)
        self.assertEqual(pack.functional_summary, "UI FLOW")
        self.assertEqual(captured["schema_name"], "game_functional_context")
        self.assertEqual(captured["payload"]["context_mode"], "functional")

    def test_knowledge_mode_preserves_prerequisites_and_evidence(self) -> None:
        provider = ScriptedProvider("context", successful_knowledge_context)
        analyzer = ContextAnalyzer(ProviderRegistry({"context": provider}), ["context"])
        segments = [Segment("guide-1", "Install first", metadata={"text_type": "tutorial_step"})]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pack, _ = analyze_with_cache(
                analyzer=analyzer,
                segments=segments,
                output_path=root / "context.json",
                state_path=root / "state.json",
            )

        self.assertEqual(pack.context_mode, "knowledge")
        self.assertEqual(pack.knowledge_nodes["BASICS"]["prerequisites"], ["INSTALL"])

    def test_hybrid_mode_gates_only_domains_present_in_source(self) -> None:
        def hybrid(**kwargs):
            payload = json.loads(kwargs["user"])
            functional_ids = [
                item["id"]
                for item in payload["lines"]
                if "functional" in item["context_domains"]
            ]
            return {
                **successful_context(**kwargs),
                "functional_summary": "MENU FLOW",
                "knowledge_summary": "",
                "terminology_rules": ["CONSISTENT BUTTONS"],
                "screens": [
                    {
                        "screen": "MENU",
                        "purpose": "NAVIGATE",
                        "actions": ["OPEN"],
                        "states": [],
                        "constraints": [],
                        "evidence_line_ids": functional_ids,
                    }
                ],
                "features": [],
                "knowledge_nodes": [],
            }

        provider = ScriptedProvider("context", hybrid)
        analyzer = ContextAnalyzer(ProviderRegistry({"context": provider}), ["context"])
        segments = [
            Segment("line-1", "Hello", speaker="A", scene="S"),
            Segment("ui-1", "Open", metadata={"text_type": "button"}),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pack, _ = analyze_with_cache(
                analyzer=analyzer,
                segments=segments,
                output_path=root / "context.json",
                state_path=root / "state.json",
            )

        self.assertEqual(pack.context_mode, "hybrid")
        self.assertEqual(pack.screen_flows["MENU"]["evidence_line_ids"], ["ui-1"])
        self.assertFalse(pack.knowledge_summary)

    def test_functional_gate_rejects_uncited_source_lines(self) -> None:
        def incomplete(**kwargs):
            payload = successful_functional_context(**kwargs)
            payload["screens"][0]["evidence_line_ids"] = ["ui-1"]
            return payload

        provider = ScriptedProvider("context", incomplete)
        analyzer = ContextAnalyzer(
            ProviderRegistry({"context": provider}), ["context"], context_mode="functional"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ProviderError, "functional evidence coverage"):
                analyze_with_cache(
                    analyzer=analyzer,
                    segments=[Segment("ui-1", "Apply"), Segment("ui-2", "Cancel")],
                    output_path=root / "context.json",
                    state_path=root / "state.json",
                )


if __name__ == "__main__":
    unittest.main()
