from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from game_l10n.context_analysis import (
    CONTEXT_DISPOSITION_DETERMINISTIC_UI,
    CONTEXT_DISPOSITION_SEMANTIC_REQUIRED,
    CONTEXT_DISPOSITION_UNCERTAIN_UI,
    ContextAnalyzer,
    analyze_with_cache,
    build_context_preflight,
    classify_context_disposition,
    infer_context_mode,
)
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
    @staticmethod
    def _context_free_ui_metadata(**extra):
        return {
            "context_domains": ["functional"],
            "text_type": "button",
            "ui_slot_role": "standalone",
            "storage_role": "display_only",
            "context_preflight": {
                "schema": "parr0t.context-preflight-authority/v1",
                "disposition": "deterministic_ui",
                "reason_code": "static_context_free_ui",
                "source_sha256": hashlib.sha256(b"SOURCE").hexdigest(),
                "evidence_sha256": "a" * 64,
            },
            **extra,
        }

    def test_button_is_ui_but_not_context_free_without_positive_authority(self) -> None:
        disposition, reason = classify_context_disposition(
            Segment("ui-1", "SOURCE", metadata={"text_type": "button"})
        )

        self.assertEqual(disposition, CONTEXT_DISPOSITION_UNCERTAIN_UI)
        self.assertEqual(reason, "ui_context_not_proven")

    def test_dynamic_ui_authority_is_still_semantic_required(self) -> None:
        disposition, _ = classify_context_disposition(
            Segment(
                "ui-1",
                "SOURCE",
                metadata=self._context_free_ui_metadata(
                    ui_slot_role="state_value", ui_state_semantics="enabled"
                ),
            )
        )

        self.assertEqual(disposition, CONTEXT_DISPOSITION_SEMANTIC_REQUIRED)

    def test_stale_ui_authority_cannot_skip_context(self) -> None:
        disposition, reason = classify_context_disposition(
            Segment(
                "ui-1",
                "CHANGED_SOURCE",
                metadata=self._context_free_ui_metadata(),
            )
        )

        self.assertEqual(disposition, CONTEXT_DISPOSITION_UNCERTAIN_UI)
        self.assertEqual(reason, "ui_context_not_proven")

    def test_context_free_ui_is_removed_before_semantic_calls(self) -> None:
        provider = ScriptedProvider("context", successful_functional_context)
        analyzer = ContextAnalyzer(ProviderRegistry({"context": provider}), ["context"])
        segments = [
            Segment(
                "ui-1",
                "SOURCE",
                metadata=self._context_free_ui_metadata(),
            )
        ]
        preflight = build_context_preflight(segments)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pack, cached = analyze_with_cache(
                analyzer=analyzer,
                segments=segments,
                output_path=root / "context.json",
                state_path=root / "state.json",
            )
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))

        self.assertFalse(cached)
        self.assertEqual(provider.call_count, 0)
        self.assertEqual(pack.context_mode, "functional")
        self.assertEqual(
            preflight["counts"][CONTEXT_DISPOSITION_DETERMINISTIC_UI], 1
        )
        self.assertEqual(state["execution"]["total_parts"], 0)
        self.assertTrue(state["attestation"]["functional_coverage"]["complete"])

    def test_ui_composition_without_resolved_context_remains_uncertain(self) -> None:
        metadata = self._context_free_ui_metadata(
            ui_composed_with_ids=["ui-missing"]
        )
        preflight = build_context_preflight(
            [Segment("ui-1", "SOURCE", metadata=metadata)]
        )

        self.assertEqual(
            preflight["overrides"][0]["disposition"],
            CONTEXT_DISPOSITION_UNCERTAIN_UI,
        )

    def test_independent_context_chunks_run_in_parallel_and_merge_in_order(self) -> None:
        lock = threading.Lock()
        active = 0
        peak = 0

        def observed(**kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.03)
                return successful_context(**kwargs)
            finally:
                with lock:
                    active -= 1

        provider = ScriptedProvider("context", observed)
        analyzer = ContextAnalyzer(
            ProviderRegistry({"context": provider}),
            ["context"],
            max_chunk_chars=100,
            max_workers=3,
        )
        segments = [
            Segment(str(index), "X" * 45, speaker="A", scene="S")
            for index in range(6)
        ]

        pack = analyzer.analyze(segments)

        self.assertGreater(peak, 1)
        self.assertEqual(
            pack.character_guides["A"]["evidence_line_ids"],
            [str(index) for index in range(6)],
        )
        self.assertEqual(
            [part["part"] for part in analyzer.last_execution["parts"]],
            list(range(1, provider.call_count + 1)),
        )

    def test_chunk_limit_counts_serialized_metadata(self) -> None:
        provider = ScriptedProvider("context", successful_context)
        analyzer = ContextAnalyzer(
            ProviderRegistry({"context": provider}),
            ["context"],
            max_chunk_chars=300,
        )

        analyzer.analyze(
            [
                Segment(
                    str(index),
                    "SOURCE",
                    speaker="A",
                    scene="S",
                    metadata={"structural_evidence": "X" * 220},
                )
                for index in range(2)
            ]
        )

        self.assertEqual(provider.call_count, 2)

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

    def test_malformed_context_state_rebuilds_instead_of_stopping(self) -> None:
        provider = ScriptedProvider("context", successful_context)
        analyzer = ContextAnalyzer(ProviderRegistry({"context": provider}), ["context"])
        segments = [Segment("1", "SOURCE", speaker="A", scene="S")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analyze_with_cache(
                analyzer=analyzer,
                segments=segments,
                output_path=root / "context.json",
                state_path=root / "state.json",
            )
            (root / "state.json").write_text("{malformed", encoding="utf-8")

            _, cached = analyze_with_cache(
                analyzer=analyzer,
                segments=segments,
                output_path=root / "context.json",
                state_path=root / "state.json",
            )

        self.assertFalse(cached)
        self.assertEqual(provider.call_count, 2)

    def test_completed_legacy_context_is_reused_without_state_rewrite(self) -> None:
        provider = ScriptedProvider("context", successful_context)
        analyzer = ContextAnalyzer(ProviderRegistry({"context": provider}), ["context"])
        segments = [Segment("1", "SOURCE", speaker="A", scene="S")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analyze_with_cache(
                analyzer=analyzer,
                segments=segments,
                output_path=root / "context.json",
                state_path=root / "state.json",
            )
            state_path = root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state.pop("schema")
            state["fingerprint"] = "legacy-fingerprint"
            state["attestation"].pop("context_preflight")
            state["attestation"].pop("semantic_segment_count")
            state["attestation"].pop("global_style_rules_required")
            legacy_text = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
            state_path.write_text(legacy_text, encoding="utf-8")

            _, cached = analyze_with_cache(
                analyzer=analyzer,
                segments=segments,
                output_path=root / "context.json",
                state_path=state_path,
            )

            self.assertEqual(state_path.read_text(encoding="utf-8"), legacy_text)

        self.assertTrue(cached)
        self.assertEqual(provider.call_count, 1)

    def test_current_context_state_cannot_downgrade_itself_to_legacy(self) -> None:
        provider = ScriptedProvider("context", successful_context)
        analyzer = ContextAnalyzer(ProviderRegistry({"context": provider}), ["context"])
        segments = [Segment("1", "SOURCE", speaker="A", scene="S")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analyze_with_cache(
                analyzer=analyzer,
                segments=segments,
                output_path=root / "context.json",
                state_path=root / "state.json",
            )
            state_path = root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["schema"], "parr0t.context-state.v2")
            state["fingerprint"] = "tampered-fingerprint"
            state["attestation"].pop("context_preflight")
            state_path.write_text(json.dumps(state), encoding="utf-8")

            _, cached = analyze_with_cache(
                analyzer=analyzer,
                segments=segments,
                output_path=root / "context.json",
                state_path=state_path,
            )

        self.assertFalse(cached)
        self.assertEqual(provider.call_count, 2)

    def test_parallelism_change_reuses_semantically_identical_context_cache(self) -> None:
        provider = ScriptedProvider("context", successful_context)
        registry = ProviderRegistry({"context": provider})
        segments = [Segment("1", "SOURCE", speaker="A", scene="S")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analyze_with_cache(
                analyzer=ContextAnalyzer(registry, ["context"], max_workers=1),
                segments=segments,
                output_path=root / "context.json",
                state_path=root / "state.json",
            )
            _, cached = analyze_with_cache(
                analyzer=ContextAnalyzer(registry, ["context"], max_workers=6),
                segments=segments,
                output_path=root / "context.json",
                state_path=root / "state.json",
            )

        self.assertTrue(cached)
        self.assertEqual(provider.call_count, 1)

    def test_cached_context_adds_inventory_binding_without_model_reanalysis(self) -> None:
        provider = ScriptedProvider("context", successful_context)
        analyzer = ContextAnalyzer(ProviderRegistry({"context": provider}), ["context"])
        segments = [Segment("1", "SOURCE", speaker="A", scene="S")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analyze_with_cache(
                analyzer=analyzer,
                segments=segments,
                output_path=root / "context.json",
                state_path=root / "state.json",
            )
            _, cached = analyze_with_cache(
                analyzer=analyzer,
                segments=segments,
                output_path=root / "context.json",
                state_path=root / "state.json",
                source_inventory_fingerprint="verified-inventory",
            )
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))

        self.assertTrue(cached)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(
            state["source_inventory_fingerprint"], "verified-inventory"
        )

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
