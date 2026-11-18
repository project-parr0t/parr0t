from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from game_l10n.coverage import completion_attestation
from game_l10n.errors import ProviderError, ProviderFailure
from game_l10n.models import (
    AttemptRecord,
    ContextPack,
    GlossaryEntry,
    ProjectContext,
    Segment,
    TranslationResult,
)
from game_l10n.orchestrator import LocalizationOrchestrator, OrchestratorConfig
from game_l10n.providers import ProviderRegistry, ScriptedProvider
from game_l10n.store import stable_hash


class OrchestratorTests(unittest.TestCase):
    def test_repaired_transport_does_not_hide_an_unknown_prefix_before_operational_counter(self):
        class Repairable(ScriptedProvider):
            revision = 0
            def fingerprint(self):
                return {**super().fingerprint(), "revision": self.revision, "model": "fixture/exact"}
        def unavailable(**_):
            raise ProviderFailure("authentication_error", retryable=False)
        with tempfile.TemporaryDirectory() as directory:
            provider = Repairable("route", unavailable)
            orchestrator = LocalizationOrchestrator(
                registry=ProviderRegistry({"route": provider}),
                config=OrchestratorConfig(["route"], [], require_review=False, translator_retries={"route": 2}),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            )
            segment = Segment("one", "SOURCE")
            prior = orchestrator.translate_one(segment)
            prior.attempts[0].ordinal = 99
            provider.revision = 1
            status = orchestrator.continuation_status(segment, prior)
            self.assertFalse(status["eligible"])
            self.assertEqual(status["issue_code"], "historical_failure_evidence_unknown")
            self.assertEqual(provider.call_count, 1)

    def test_compact_translation_ordinal_spends_the_captured_semantic_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider("route", lambda **_: {"status": "refused", "text": ""})
            registry = ProviderRegistry({"route": provider})
            segment = Segment("one", "SOURCE")
            first = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(["route"], [], require_review=False, translator_retries={"route": 1}),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            )
            prior = first.translate_one(segment)
            prior.attempts = [prior.attempts[-1]]
            self.assertEqual(prior.attempts[0].ordinal, 2)
            resumed = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(["route"], [], require_review=False, translator_retries={"route": 2}),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            )
            result = resumed.continue_many([segment], [prior])[0]
            self.assertEqual(provider.call_count, 3)
            self.assertEqual([item.ordinal for item in result.attempts if item.provider == "route"], [2, 3])
            self.assertFalse(resumed.continuation_status(segment, result)["eligible"])

    def test_semantic_counter_keeps_compact_gaps_without_charging_known_operational_events(self):
        operational = AttemptRecord("review", "route", "error", ordinal=1, operational=True)
        semantic = AttemptRecord("review", "route", "error", ordinal=2, operational=False)
        used = LocalizationOrchestrator._semantic_attempts_used
        self.assertEqual(used([semantic]), 2)
        self.assertEqual(used([operational, semantic]), 1)
        operational.ordinal = 99
        self.assertEqual(used([operational]), 0)

    def test_request_capacity_failure_exposes_actionable_repair_not_budget_exhaustion(self) -> None:
        def oversized(**_):
            raise ProviderFailure("input_capacity_exceeded", retryable=False)
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider("configured", oversized)
            orchestrator = LocalizationOrchestrator(
                registry=ProviderRegistry({provider.name: provider}),
                config=OrchestratorConfig([provider.name], [], require_review=False),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            )
            result = orchestrator.translate_one(Segment("line", "SOURCE"))
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(result.attempts[-1].issue_code, "input_capacity_exceeded")
            self.assertFalse(result.metadata["capacity_repair"]["user_budget_exhausted"])
            self.assertEqual(result.metadata["capacity_repair"]["action"],
                             "compact_focused_context_or_configure_larger_input_capacity")

    def test_empty_parallel_inventory_does_not_create_an_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider("configured", lambda **_: self.fail("No targets"))
            orchestrator = LocalizationOrchestrator(
                registry=ProviderRegistry({provider.name: provider}),
                config=OrchestratorConfig([provider.name], [], require_review=False,
                                          max_workers=128, use_style_memory=False),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            )
            self.assertEqual(orchestrator.translate_many([]), [])

    @staticmethod
    def _optional_empty_segment() -> Segment:
        source = "OPTIONAL_ARTICLE"
        metadata = {
            "ui_slot_role": "template_fragment",
            "ui_composed_with_ids": ["fixture-label"],
            "ui_render_pattern": "{prefix}{value}",
            "ui_fixed_target_prefix": "\ub300\uc0c1 ",
            "ui_fixed_target_suffix": "",
        }
        source_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        composition = {
            "target_id": "optional-empty",
            "source_sha256": source_digest,
            **metadata,
        }
        metadata["ui_empty_target"] = {
            "schema": "parr0t.ui-empty-target/v1",
            "reason_code": "optional_grammatical_slot",
            "source_sha256": source_digest,
            "composition_sha256": stable_hash(composition),
            "evidence_sha256": hashlib.sha256(b"SYNTHETIC_BINDING").hexdigest(),
        }
        return Segment("optional-empty", source, metadata=metadata)

    def test_single_and_deferred_paths_accept_bound_empty_candidate(self) -> None:
        for deferred in (False, True):
            with self.subTest(deferred=deferred), tempfile.TemporaryDirectory() as directory:
                translator = ScriptedProvider(
                    "fixture-translator",
                    lambda **_: {"status": "success", "text": "", "reason": ""},
                )
                orchestrator = LocalizationOrchestrator(
                    registry=ProviderRegistry({translator.name: translator}),
                    config=OrchestratorConfig(
                        [translator.name],
                        [],
                        require_review=False,
                        defer_fallbacks_until_primary_complete=deferred,
                    ),
                    project=ProjectContext("TEST", "en", "ko"),
                    work_dir=Path(directory),
                )
                segment = self._optional_empty_segment()
                result = (
                    orchestrator.translate_many([segment])[0]
                    if deferred
                    else orchestrator.translate_one(segment)
                )
                self.assertEqual(result.status, "translated")
                self.assertEqual(result.text, "")

    def test_wrong_language_candidate_reaches_next_route_in_all_paths(self) -> None:
        for deferred in (False, True):
            with self.subTest(deferred=deferred), tempfile.TemporaryDirectory() as directory:
                primary = ScriptedProvider(
                    "primary",
                    lambda **_: {
                        "status": "success",
                        "text": "Save the current game now",
                        "reason": "",
                    },
                )
                fallback = ScriptedProvider(
                    "fallback",
                    lambda **_: {
                        "status": "success",
                        "text": "\uac8c\uc784 \uc800\uc7a5",
                        "reason": "",
                    },
                )
                orchestrator = LocalizationOrchestrator(
                    registry=ProviderRegistry(
                        {primary.name: primary, fallback.name: fallback}
                    ),
                    config=OrchestratorConfig(
                        [primary.name, fallback.name],
                        [],
                        translator_retries={primary.name: 0, fallback.name: 0},
                        require_review=False,
                        defer_fallbacks_until_primary_complete=deferred,
                    ),
                    project=ProjectContext("TEST", "en", "ko"),
                    work_dir=Path(directory),
                )
                segment = Segment("language-route", "Save the game")
                result = (
                    orchestrator.translate_many([segment])[0]
                    if deferred
                    else orchestrator.translate_one(segment)
                )
                self.assertEqual(result.status, "translated")
                self.assertEqual(result.translator, fallback.name)
                self.assertEqual(result.text, "\uac8c\uc784 \uc800\uc7a5")

    def test_deferred_success_is_staged_before_later_target_interrupts(self) -> None:
        calls: list[str] = []

        def interrupted(**kwargs):
            source = json.loads(kwargs["user"])["translation_target"]["source"]
            calls.append(source)
            if source == "SOURCE_2":
                raise KeyboardInterrupt
            return {"status": "success", "text": "TARGET_1", "reason": ""}

        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            first = ScriptedProvider("fixture-translator", interrupted)
            config = OrchestratorConfig(
                [first.name],
                [],
                require_review=False,
                defer_fallbacks_until_primary_complete=True,
                max_workers=1,
            )
            segments = [Segment("one", "SOURCE_1"), Segment("two", "SOURCE_2")]
            with self.assertRaises(KeyboardInterrupt):
                LocalizationOrchestrator(
                    registry=ProviderRegistry({first.name: first}),
                    config=config,
                    project=ProjectContext("TEST", "src", "dst"),
                    work_dir=work,
                ).translate_many(segments)

            resumed_calls: list[str] = []

            def resumed(**kwargs):
                source = json.loads(kwargs["user"])["translation_target"]["source"]
                resumed_calls.append(source)
                return {"status": "success", "text": "TARGET_2", "reason": ""}

            second = ScriptedProvider("fixture-translator", resumed)
            results = LocalizationOrchestrator(
                registry=ProviderRegistry({second.name: second}),
                config=config,
                project=ProjectContext("TEST", "src", "dst"),
                work_dir=work,
            ).translate_many(segments)

        self.assertEqual(calls, ["SOURCE_1", "SOURCE_2"])
        self.assertEqual(resumed_calls, ["SOURCE_2"])
        self.assertEqual([item.status for item in results], ["translated", "translated"])

    def test_unsatisfiable_ui_constraint_skips_every_provider(self) -> None:
        for deferred in (False, True):
            with self.subTest(deferred=deferred), tempfile.TemporaryDirectory() as directory:
                translator = ScriptedProvider(
                    "fixture-translator",
                    lambda **_: {
                        "status": "success",
                        "text": "TARGET",
                        "reason": "",
                    },
                )
                orchestrator = LocalizationOrchestrator(
                    registry=ProviderRegistry({translator.name: translator}),
                    config=OrchestratorConfig(
                        [translator.name],
                        [],
                        require_review=False,
                        defer_fallbacks_until_primary_complete=deferred,
                    ),
                    project=ProjectContext("TEST", "src", "dst"),
                    work_dir=Path(directory),
                )
                segment = Segment(
                    "impossible", "{player}", metadata={"ui_max_chars": 4}
                )
                result = (
                    orchestrator.translate_many([segment])[0]
                    if deferred
                    else orchestrator.translate_one(segment)
                )
                self.assertEqual(result.status, "failed")
                self.assertEqual(translator.call_count, 0)
                self.assertNotIn("All translation routes were exhausted", result.errors)
                self.assertIn(
                    "constraint_unsatisfiable",
                    {attempt.issue_code for attempt in result.attempts},
                )

    def test_style_memory_prioritizes_context_compatible_dialogue(self) -> None:
        provider = ScriptedProvider(
            "translator",
            lambda **_: {"status": "success", "text": "TARGET", "reason": ""},
        )
        contexts = {
            "compatible": {
                "consistency_group_id": "speaker:addressee",
                "addressee_ids": ["addressee"],
                "relationship_state": "trusted",
            },
            "unrelated": {
                "consistency_group_id": "speaker:other",
                "addressee_ids": ["other"],
                "relationship_state": "hostile",
            },
            "current": {
                "consistency_group_id": "speaker:addressee",
                "addressee_ids": ["addressee"],
                "relationship_state": "trusted",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = LocalizationOrchestrator(
                registry=ProviderRegistry({provider.name: provider}),
                config=OrchestratorConfig(
                    [provider.name],
                    [],
                    require_review=False,
                    use_style_memory=True,
                    style_anchor_limit=1,
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(dialogue_contexts=contexts),
                work_dir=Path(directory),
            )
            orchestrator._remember(
                Segment("compatible", "SOURCE_1", speaker="speaker"), "TARGET_1"
            )
            orchestrator._remember(
                Segment("unrelated", "SOURCE_2", speaker="speaker"), "TARGET_2"
            )

            anchors = orchestrator._anchors(
                Segment("current", "SOURCE_3", speaker="speaker")
            )

        self.assertEqual([anchor["target_id"] for anchor in anchors], ["compatible"])

    def test_unreviewed_results_do_not_become_approved_style_anchors(self) -> None:
        observed_anchors: list[list[dict[str, str]]] = []

        def translate(**kwargs):
            observed_anchors.append(
                json.loads(kwargs["user"])["approved_same_speaker_style_anchors"]
            )
            return {"status": "success", "text": "TARGET", "reason": ""}

        translator = ScriptedProvider("fixture-translator", translate)
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = LocalizationOrchestrator(
                registry=ProviderRegistry({translator.name: translator}),
                config=OrchestratorConfig(
                    [translator.name], [], require_review=False, use_style_memory=True
                ),
                project=ProjectContext("TEST", "src", "dst"),
                work_dir=Path(directory),
            )
            orchestrator.translate_one(Segment("one", "SOURCE_1", speaker="A"))
            orchestrator.translate_one(Segment("two", "SOURCE_2", speaker="A"))

        self.assertEqual(observed_anchors, [[], []])

    def test_deferred_fallback_preserves_manifest_order_after_reverse_completion(self) -> None:
        class ReverseCompletionOrchestrator(LocalizationOrchestrator):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.stage_indexes: list[tuple[str, list[int]]] = []

            def _run_deferred_stage(self, indexes, operation, *, stage):
                self.stage_indexes.append((stage, list(indexes)))
                completed = [(index, operation(index)) for index in indexes]
                yield from reversed(completed)

        primary = ScriptedProvider(
            "primary",
            lambda **_: {"status": "refused", "text": "", "reason": ""},
        )
        fallback = ScriptedProvider(
            "fallback",
            lambda **_: {"status": "success", "text": "TARGET", "reason": ""},
        )
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = ReverseCompletionOrchestrator(
                registry=ProviderRegistry(
                    {primary.name: primary, fallback.name: fallback}
                ),
                config=OrchestratorConfig(
                    [primary.name, fallback.name],
                    [],
                    translator_retries={primary.name: 0, fallback.name: 0},
                    require_review=False,
                    defer_fallbacks_until_primary_complete=True,
                ),
                project=ProjectContext("TEST", "src", "dst"),
                work_dir=Path(directory),
            )
            results = orchestrator.translate_many(
                [Segment("one", "SOURCE_1"), Segment("two", "SOURCE_2")]
            )

        self.assertEqual(
            orchestrator.stage_indexes,
            [("translation", [0, 1]), ("translation_fallback", [0, 1])],
        )
        self.assertEqual([result.status for result in results], ["translated", "translated"])

    def test_provider_wait_rechecks_a_cooldown_extended_by_another_worker(self) -> None:
        provider = ScriptedProvider(
            "fixture",
            lambda **_: {"status": "success", "text": "TARGET", "reason": ""},
        )
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = LocalizationOrchestrator(
                registry=ProviderRegistry({provider.name: provider}),
                config=OrchestratorConfig([provider.name], [], require_review=False),
                project=ProjectContext("TEST", "src", "dst"),
                work_dir=Path(directory),
            )
            clock = [0.0]
            sleeps: list[float] = []
            orchestrator._provider_cooldowns[provider.name] = 1.0

            def fake_sleep(seconds: float) -> None:
                sleeps.append(seconds)
                clock[0] += seconds
                if len(sleeps) == 1:
                    orchestrator._provider_cooldowns[provider.name] = 3.0

            with patch(
                "game_l10n.orchestrator.time.monotonic", side_effect=lambda: clock[0]
            ), patch("game_l10n.orchestrator.time.sleep", side_effect=fake_sleep):
                orchestrator._wait_for_provider(provider.name)

        self.assertEqual(sleeps, [1.0, 2.0])

    def test_failed_or_deterministically_invalid_cache_is_reprocessed(self) -> None:
        cases = [
            ("failed", "", "dst", "TARGET"),
            ("translated", "Still English", "ko", "\ubc88\uc5ed"),
        ]
        for cached_status, cached_text, target_language, fresh_text in cases:
            for deferred in (False, True):
                with self.subTest(
                    status=cached_status, deferred=deferred
                ), tempfile.TemporaryDirectory() as directory:
                    translator = ScriptedProvider(
                        "fixture",
                        lambda text=fresh_text, **_: {
                            "status": "success",
                            "text": text,
                            "reason": "",
                        },
                    )
                    orchestrator = LocalizationOrchestrator(
                        registry=ProviderRegistry({translator.name: translator}),
                        config=OrchestratorConfig(
                            [translator.name],
                            [],
                            require_review=False,
                            defer_fallbacks_until_primary_complete=deferred,
                        ),
                        project=ProjectContext("TEST", "en", target_language),
                        work_dir=Path(directory),
                    )
                    segment = Segment("cached", "SOURCE")
                    cache_key = orchestrator._cache_key(segment, [])
                    orchestrator.store.put(
                        cache_key,
                        TranslationResult(
                            segment_id=segment.id,
                            source=segment.source,
                            text=cached_text,
                            status=cached_status,
                        ),
                    )

                    result = (
                        orchestrator.translate_many([segment])[0]
                        if deferred
                        else orchestrator.translate_one(segment)
                    )

                self.assertEqual(translator.call_count, 1)
                self.assertEqual(result.status, "translated")
                self.assertEqual(result.text, fresh_text)

    def test_unrelated_context_and_parallelism_changes_reuse_target_cache(self) -> None:
        translator = ScriptedProvider(
            "fixture-translator",
            lambda **_: {"status": "success", "text": "TARGET", "reason": ""},
        )
        segment = Segment("one", "SOURCE", scene="CURRENT")
        first_context = ContextPack(
            story_summary="STORY",
            global_style_rules=("RULE",),
            scene_summaries={
                "CURRENT": {"summary": "CURRENT"},
                "OTHER": {"summary": "OLD"},
            },
        )
        second_context = ContextPack(
            story_summary="STORY",
            global_style_rules=("RULE",),
            scene_summaries={
                "CURRENT": {"summary": "CURRENT"},
                "OTHER": {"summary": "CHANGED"},
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            first = LocalizationOrchestrator(
                registry=ProviderRegistry({translator.name: translator}),
                config=OrchestratorConfig(
                    [translator.name], [], require_review=False, max_workers=1
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=first_context,
                work_dir=work,
            ).translate_one(segment)
            second = LocalizationOrchestrator(
                registry=ProviderRegistry({translator.name: translator}),
                config=OrchestratorConfig(
                    [translator.name], [], require_review=False, max_workers=4
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=second_context,
                work_dir=work,
            ).translate_one(segment)

        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertEqual(translator.call_count, 1)

    def test_relevant_context_change_invalidates_target_cache(self) -> None:
        translator = ScriptedProvider(
            "fixture-translator",
            lambda **_: {"status": "success", "text": "TARGET", "reason": ""},
        )
        segment = Segment("one", "SOURCE", scene="CURRENT")
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            for summary in ("FIRST", "SECOND"):
                LocalizationOrchestrator(
                    registry=ProviderRegistry({translator.name: translator}),
                    config=OrchestratorConfig(
                        [translator.name], [], require_review=False
                    ),
                    project=ProjectContext("TEST", "src", "dst"),
                    context_pack=ContextPack(
                        story_summary="STORY",
                        global_style_rules=("RULE",),
                        scene_summaries={"CURRENT": {"summary": summary}},
                    ),
                    work_dir=work,
                ).translate_one(segment)

        self.assertEqual(translator.call_count, 2)

    def test_deferred_pipeline_reuses_focused_target_cache(self) -> None:
        translator = ScriptedProvider(
            "fixture-translator",
            lambda **_: {"status": "success", "text": "TARGET", "reason": ""},
        )
        segment = Segment("one", "SOURCE", scene="CURRENT")
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            results = []
            for unrelated, workers in (("OLD", 1), ("CHANGED", 4)):
                results.append(
                    LocalizationOrchestrator(
                        registry=ProviderRegistry({translator.name: translator}),
                        config=OrchestratorConfig(
                            [translator.name],
                            [],
                            require_review=False,
                            defer_fallbacks_until_primary_complete=True,
                            max_workers=workers,
                        ),
                        project=ProjectContext("TEST", "src", "dst"),
                        context_pack=ContextPack(
                            story_summary="STORY",
                            global_style_rules=("RULE",),
                            scene_summaries={
                                "CURRENT": {"summary": "CURRENT"},
                                "OTHER": {"summary": unrelated},
                            },
                        ),
                        work_dir=work,
                    ).translate_many([segment])[0]
                )

        self.assertFalse(results[0].from_cache)
        self.assertTrue(results[1].from_cache)
        self.assertEqual(translator.call_count, 1)

    def test_deferred_pipeline_reports_counts_without_semantic_content(self) -> None:
        events: list[dict[str, object]] = []
        translator = ScriptedProvider(
            "fixture-translator",
            lambda **_: {"status": "success", "text": "TARGET", "reason": ""},
        )
        with tempfile.TemporaryDirectory() as directory:
            results = LocalizationOrchestrator(
                registry=ProviderRegistry({translator.name: translator}),
                config=OrchestratorConfig(
                    [translator.name],
                    [],
                    require_review=False,
                    defer_fallbacks_until_primary_complete=True,
                    use_style_memory=False,
                    max_workers=2,
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
                progress_callback=events.append,
            ).translate_many(
                [Segment("one", "SOURCE_1"), Segment("two", "SOURCE_2")]
            )

        self.assertEqual(len(results), 2)
        self.assertTrue(events)
        self.assertEqual(events[-1]["completed_records"], 2)
        self.assertEqual(events[-1]["translation_completed"], 2)
        serialized = json.dumps(events)
        for forbidden in ("SOURCE_1", "TARGET", "segment_id", "target_id"):
            self.assertNotIn(forbidden, serialized)

    def test_route_attempt_caps_retry_refusal_before_advancing(self) -> None:
        calls = {"luna": 0, "sol": 0, "deepseek": 0}

        def refused(route: str):
            def handler(**_):
                calls[route] += 1
                return {"status": "refused", "text": "", "reason": "private"}

            return handler

        deepseek = ScriptedProvider(
            "deepseek",
            lambda **_: (
                calls.__setitem__("deepseek", calls["deepseek"] + 1)
                or {"status": "success", "text": "TARGET", "reason": ""}
            ),
        )
        providers = [
            ScriptedProvider("luna", refused("luna")),
            ScriptedProvider("sol", refused("sol")),
            deepseek,
        ]
        with tempfile.TemporaryDirectory() as directory:
            result = LocalizationOrchestrator(
                registry=ProviderRegistry({item.name: item for item in providers}),
                config=OrchestratorConfig(
                    ["luna", "sol", "deepseek"],
                    [],
                    translator_retries={"luna": 5, "sol": 3},
                    require_review=False,
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
            ).translate_one(Segment("retry-route", "SOURCE"))

        self.assertEqual(result.status, "translated")
        self.assertEqual(calls, {"luna": 6, "sol": 4, "deepseek": 1})
        self.assertEqual(
            [item.reason for item in result.attempts if item.status == "refused"],
            ["explicit_provider_refusal"] * 10,
        )

    def test_low_cost_triage_approval_skips_strong_reviewer(self) -> None:
        translator = ScriptedProvider(
            "fixture-translator",
            lambda **_: {"status": "success", "text": "TARGET", "reason": ""},
        )
        triager = ScriptedProvider(
            "fixture-triager", lambda **_: {"verdict": "ok", "issues": []}
        )
        strong = ScriptedProvider(
            "fixture-strong", lambda **_: {"verdict": "ok", "issues": []}
        )
        registry = ProviderRegistry(
            {item.name: item for item in (translator, triager, strong)}
        )
        segment = Segment("triage-only", "SOURCE")
        with tempfile.TemporaryDirectory() as directory:
            result = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    [translator.name],
                    [strong.name],
                    review_triager_chain=[triager.name],
                    strong_review_sample_percent=0,
                    strong_review_high_risk_categories=[],
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
            ).translate_one(segment)

        self.assertEqual(result.status, "reviewed")
        self.assertEqual(result.metadata["review_tier"], "triage")
        self.assertEqual(triager.call_count, 1)
        self.assertEqual(strong.call_count, 0)
        state = completion_attestation(
            [segment],
            [result],
            inventory_segments=[segment],
            inventory={
                "status": "static_verified",
                "static_complete": True,
                "primary_segments": 1,
                "primary_fingerprint": "fixture",
            },
            runtime={"runtime_complete": False},
            review_required=True,
        )
        self.assertTrue(state["semantic_review"]["complete"])

    def test_triage_flag_routes_candidate_to_strong_reviewer(self) -> None:
        translator = ScriptedProvider(
            "fixture-translator",
            lambda **_: {"status": "success", "text": "TARGET", "reason": ""},
        )
        triager = ScriptedProvider(
            "fixture-triager",
            lambda **_: {"verdict": "rewrite", "issues": ["literalism"]},
        )
        strong = ScriptedProvider(
            "fixture-strong", lambda **_: {"verdict": "ok", "issues": []}
        )
        registry = ProviderRegistry(
            {item.name: item for item in (translator, triager, strong)}
        )
        with tempfile.TemporaryDirectory() as directory:
            result = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    [translator.name],
                    [strong.name],
                    review_triager_chain=[triager.name],
                    strong_review_sample_percent=0,
                    strong_review_high_risk_categories=[],
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
            ).translate_one(Segment("triage-flag", "SOURCE"))

        self.assertEqual(result.status, "reviewed")
        self.assertEqual(result.metadata["review_tier"], "strong")
        self.assertIn("triage_flagged", result.metadata["strong_review_reasons"])
        self.assertEqual(triager.call_count, 1)
        self.assertEqual(strong.call_count, 1)

    def test_deterministic_sample_routes_candidate_to_strong_reviewer(self) -> None:
        translator = ScriptedProvider(
            "fixture-translator",
            lambda **_: {"status": "success", "text": "TARGET", "reason": ""},
        )
        triager = ScriptedProvider(
            "fixture-triager", lambda **_: {"verdict": "ok", "issues": []}
        )
        strong = ScriptedProvider(
            "fixture-strong", lambda **_: {"verdict": "ok", "issues": []}
        )
        registry = ProviderRegistry(
            {item.name: item for item in (translator, triager, strong)}
        )
        with tempfile.TemporaryDirectory() as directory:
            result = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    [translator.name],
                    [strong.name],
                    review_triager_chain=[triager.name],
                    strong_review_sample_percent=100,
                    strong_review_high_risk_categories=[],
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
            ).translate_one(Segment("sampled", "SOURCE"))

        self.assertEqual(result.metadata["review_tier"], "strong")
        self.assertIn("deterministic_sample", result.metadata["strong_review_reasons"])
        self.assertEqual(strong.call_count, 1)

    def test_deferred_batch_triages_all_candidates_without_strong_calls(self) -> None:
        translator = ScriptedProvider(
            "fixture-translator",
            lambda **_: {"status": "success", "text": "TARGET", "reason": ""},
        )
        triager = ScriptedProvider(
            "fixture-triager", lambda **_: {"verdict": "ok", "issues": []}
        )
        strong = ScriptedProvider(
            "fixture-strong", lambda **_: {"verdict": "ok", "issues": []}
        )
        registry = ProviderRegistry(
            {item.name: item for item in (translator, triager, strong)}
        )
        with tempfile.TemporaryDirectory() as directory:
            results = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    [translator.name],
                    [strong.name],
                    review_triager_chain=[triager.name],
                    strong_review_sample_percent=0,
                    strong_review_high_risk_categories=[],
                    defer_fallbacks_until_primary_complete=True,
                    use_style_memory=False,
                    max_workers=2,
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
            ).translate_many(
                [Segment("batch-1", "SOURCE_1"), Segment("batch-2", "SOURCE_2")]
            )

        self.assertEqual([item.status for item in results], ["reviewed", "reviewed"])
        self.assertEqual([item.metadata["review_tier"] for item in results], ["triage", "triage"])
        self.assertEqual(triager.call_count, 2)
        self.assertEqual(strong.call_count, 0)

    def test_successful_review_retry_completes_initial_and_corrected_candidates(self) -> None:
        for confirmation in (False, True):
            with self.subTest(confirmation=confirmation):
                calls = 0

                def judge_handler(**_):
                    nonlocal calls
                    calls += 1
                    if confirmation and calls == 1:
                        return {"verdict": "rewrite", "issues": ["voice"]}
                    if calls == (2 if confirmation else 1):
                        raise ProviderFailure("malformed_response", retryable=True)
                    return {"verdict": "ok", "issues": []}

                translator = ScriptedProvider(
                    "synthetic-translator",
                    lambda **_: {"status": "success", "text": "TARGET_A", "reason": ""},
                )
                judge = ScriptedProvider("synthetic-judge", judge_handler)
                rewriter = ScriptedProvider(
                    "synthetic-rewriter",
                    lambda **_: {"status": "success", "text": "TARGET_B"},
                )
                registry = ProviderRegistry(
                    {provider.name: provider for provider in (translator, judge, rewriter)}
                )
                segment = Segment("synthetic-retry", "SOURCE")
                with tempfile.TemporaryDirectory() as directory:
                    result = LocalizationOrchestrator(
                        registry=registry,
                        config=OrchestratorConfig(
                            [translator.name],
                            [judge.name],
                            grace_retries=1,
                            rewriter_chain=[rewriter.name],
                        ),
                        project=ProjectContext("SYNTHETIC", "src", "dst"),
                        context_pack=ContextPack(story_summary="SYNTHETIC_STORY"),
                        work_dir=Path(directory),
                    ).translate_one(segment)
                state = completion_attestation(
                    [segment],
                    [result],
                    inventory_segments=[segment],
                    inventory={
                        "status": "static_verified",
                        "static_complete": True,
                        "primary_segments": 1,
                        "primary_fingerprint": "synthetic",
                    },
                    runtime={"runtime_complete": False},
                    review_required=True,
                )
                self.assertTrue(state["semantic_review"]["complete"])
                self.assertTrue(state["static_completion"])
                self.assertEqual(result.candidate_generation, 2 if confirmation else 1)
                self.assertEqual(calls, 3 if confirmation else 2)

    def test_compact_review_skips_rewriter_for_approved_candidate(self) -> None:
        translator = ScriptedProvider(
            "fixture-translator",
            lambda **_: {"status": "success", "text": "TARGET", "reason": ""},
        )
        judge = ScriptedProvider(
            "fixture-judge", lambda **_: {"verdict": "ok", "issues": []}
        )
        rewriter = ScriptedProvider(
            "fixture-rewriter",
            lambda **_: {"status": "success", "text": "UNEXPECTED"},
        )
        registry = ProviderRegistry(
            {item.name: item for item in [translator, judge, rewriter]}
        )
        with tempfile.TemporaryDirectory() as directory:
            result = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    [translator.name],
                    [judge.name],
                    rewriter_chain=[rewriter.name],
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
            ).translate_one(Segment("fixture-1", "SOURCE"))

        self.assertEqual(result.status, "reviewed")
        self.assertEqual(result.text, "TARGET")
        self.assertEqual(result.review_outcome, "approved")
        self.assertEqual(judge.call_count, 1)
        self.assertEqual(rewriter.call_count, 0)

    def test_compact_review_rewrites_only_flagged_candidate_then_confirms(self) -> None:
        judge_calls = 0

        def judge_handler(**kwargs):
            nonlocal judge_calls
            judge_calls += 1
            payload = json.loads(kwargs["user"])
            if payload["candidate_translation"] == "TARGET_A":
                return {"verdict": "rewrite", "issues": ["voice"]}
            return {"verdict": "ok", "issues": []}

        def rewrite_handler(**kwargs):
            payload = json.loads(kwargs["user"])
            self.assertEqual(payload["detected_issue_codes"], ["voice"])
            return {"status": "success", "text": "TARGET_B"}

        translator = ScriptedProvider(
            "fixture-translator",
            lambda **_: {"status": "success", "text": "TARGET_A", "reason": ""},
        )
        judge = ScriptedProvider("fixture-judge", judge_handler)
        rewriter = ScriptedProvider("fixture-rewriter", rewrite_handler)
        registry = ProviderRegistry(
            {item.name: item for item in [translator, judge, rewriter]}
        )
        with tempfile.TemporaryDirectory() as directory:
            result = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    [translator.name],
                    [judge.name],
                    rewriter_chain=[rewriter.name],
                    review_max_rounds=2,
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
            ).translate_one(Segment("fixture-1", "SOURCE"))

        self.assertEqual(result.status, "reviewed")
        self.assertEqual(result.text, "TARGET_B")
        self.assertEqual(result.review_outcome, "revised")
        self.assertEqual(result.candidate_generation, 2)
        self.assertEqual(judge_calls, 2)
        self.assertEqual(rewriter.call_count, 1)
        self.assertIn("review-confirm", [item.stage for item in result.attempts])
        self.assertIn("review-rewrite", [item.stage for item in result.attempts])

    def test_strong_approval_cannot_waive_locked_target_language(self) -> None:
        def rewrite_handler(**kwargs):
            payload = json.loads(kwargs["user"])
            self.assertEqual(payload["detected_issue_codes"], ["fluency"])
            return {
                "status": "success",
                "text": "\ud55c\uad6d\uc5b4 \ud604\uc9c0\ud654 \uac80\uc218\ub97c \uc704\ud55c \uc815\uc0c1\uc801\uc778 \ubc88\uc5ed\ubb38\uc785\ub2c8\ub2e4.",
            }

        translator = ScriptedProvider(
            "fixture-translator",
            lambda **_: {
                "status": "success",
                "text": "This candidate is still written entirely in English.",
                "reason": "",
            },
        )
        judge = ScriptedProvider(
            "fixture-judge", lambda **_: {"verdict": "ok", "issues": []}
        )
        rewriter = ScriptedProvider("fixture-rewriter", rewrite_handler)
        registry = ProviderRegistry(
            {item.name: item for item in [translator, judge, rewriter]}
        )

        with tempfile.TemporaryDirectory() as directory:
            result = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    [translator.name],
                    [judge.name],
                    rewriter_chain=[rewriter.name],
                    strong_review_sample_percent=0,
                    strong_review_high_risk_categories=[],
                ),
                project=ProjectContext("TEST", "en", "ko"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
            ).translate_one(
                Segment(
                    "fixture-language-lock",
                    "This is a source sentence for the Korean localization review.",
                )
            )

        self.assertEqual(result.status, "failed")
        self.assertIn("target_script_absent", result.metadata["target_language_risk"])
        self.assertEqual(judge.call_count, 0)
        self.assertEqual(rewriter.call_count, 0)

    def test_rewriter_refusal_does_not_accept_a_candidate_flagged_for_rewrite(self) -> None:
        translator = ScriptedProvider(
            "fixture-translator",
            lambda **_: {"status": "success", "text": "TARGET_A", "reason": ""},
        )
        judge = ScriptedProvider(
            "fixture-judge", lambda **_: {"verdict": "rewrite", "issues": ["meaning"]}
        )
        rewriter = ScriptedProvider(
            "fixture-rewriter", lambda **_: {"status": "refused", "text": ""}
        )
        registry = ProviderRegistry(
            {item.name: item for item in [translator, judge, rewriter]}
        )
        with tempfile.TemporaryDirectory() as directory:
            result = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    [translator.name],
                    [judge.name],
                    rewriter_chain=[rewriter.name],
                    review_refusal_policy="accept_candidate_unreviewed",
                    preserve_source_on_failure=True,
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
            ).translate_one(Segment("fixture-1", "SOURCE"))

        self.assertEqual(result.status, "source_preserved")
        self.assertEqual(result.text, "SOURCE")
        self.assertNotEqual(result.status, "accepted_unreviewed")

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
        self.assertEqual(result.attempts[0].reason, "explicit_provider_refusal")
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
                    review_attempt_limits={"deepseek": 1},
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
        self.assertIn("explicit_provider_refusal", {item.reason for item in results[0].attempts})
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
                    translator_chain=["t-refuse", "t-ok"], reviewer_chain=["r-refuse", "r-ok"],
                    review_attempt_limits={"r-ok": 2},
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

    def test_retryable_failure_gets_grace_attempt_before_fallback(self) -> None:
        calls = 0

        def translator(**_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProviderError("temporary transport failure")
            return {"status": "success", "text": "TARGET", "reason": ""}

        provider = ScriptedProvider("fixture-translator", translator)
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = LocalizationOrchestrator(
                registry=ProviderRegistry({provider.name: provider}),
                config=OrchestratorConfig(
                    [provider.name],
                    [],
                    require_review=False,
                    retry_refusals_once=False,
                    grace_retries=1,
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
            )
            result = orchestrator.translate_one(Segment("fixture-1", "SOURCE"))

        self.assertEqual(result.status, "translated")
        self.assertEqual([item.ordinal for item in result.attempts], [1, 2])
        self.assertEqual(result.attempts[0].reason, "provider_error")
        self.assertEqual(result.attempts[1].stage, "translate-grace-retry")

    def test_review_refusal_policy_keeps_candidate_without_false_approval(self) -> None:
        translator = ScriptedProvider(
            "fixture-translator",
            lambda **_: {"status": "success", "text": "TARGET", "reason": ""},
        )
        reviewer = ScriptedProvider(
            "fixture-reviewer",
            lambda **_: {
                "decision": "refused",
                "text": "",
                "meaning_preserved": False,
                "issues": [],
                "reason": "provider prose must not escape",
            },
        )
        registry = ProviderRegistry({item.name: item for item in [translator, reviewer]})
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    [translator.name],
                    [reviewer.name],
                    retry_refusals_once=False,
                    review_refusal_policy="accept_candidate_unreviewed",
                ),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
            )
            result = orchestrator.translate_one(Segment("fixture-1", "SOURCE"))

        self.assertEqual(result.status, "accepted_unreviewed")
        self.assertEqual(result.review_outcome, "refused")
        self.assertEqual(result.acceptance_authority, "user_policy")
        self.assertEqual(result.resolution_code, "user_accepted_after_review_refusal")
        self.assertNotEqual(result.review_outcome, "approved")

    def test_revised_review_advances_candidate_generation(self) -> None:
        translator = ScriptedProvider(
            "fixture-translator",
            lambda **_: {"status": "success", "text": "TARGET_A", "reason": ""},
        )
        reviewer = ScriptedProvider(
            "fixture-reviewer",
            lambda **_: {
                "decision": "revised",
                "text": "TARGET_B",
                "meaning_preserved": True,
                "issues": [],
                "reason": "",
            },
        )
        registry = ProviderRegistry({item.name: item for item in [translator, reviewer]})
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig([translator.name], [reviewer.name]),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
            )
            result = orchestrator.translate_one(Segment("fixture-1", "SOURCE"))

        self.assertEqual(result.candidate_generation, 2)
        self.assertTrue(result.candidate_digest)
        self.assertEqual(result.review_outcome, "revised")

    def test_full_existing_review_judges_every_reused_candidate(self) -> None:
        translator = ScriptedProvider(
            "fixture-unused-translator",
            lambda **_: {"status": "success", "text": "UNEXPECTED", "reason": ""},
        )
        reviewer = ScriptedProvider(
            "fixture-reviewer", lambda **_: {"verdict": "ok", "issues": []}
        )
        registry = ProviderRegistry({item.name: item for item in [translator, reviewer]})
        segments = [
            Segment("synthetic-1", "SOURCE_ONE"),
            Segment("synthetic-2", "SOURCE_TWO"),
        ]
        candidates = {
            "synthetic-1": {"source": "SOURCE_ONE", "text": "TARGET_ONE"},
            "synthetic-2": {"source": "SOURCE_TWO", "text": "TARGET_TWO"},
        }
        with tempfile.TemporaryDirectory() as directory:
            results = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig([translator.name], [reviewer.name]),
                project=ProjectContext("TEST", "src", "dst"),
                context_pack=ContextPack(story_summary="STORY"),
                work_dir=Path(directory),
            ).review_existing_many(segments, candidates)

        self.assertEqual(reviewer.call_count, 2)
        self.assertEqual(translator.call_count, 0)
        self.assertEqual([item.status for item in results], ["reviewed", "reviewed"])
        self.assertTrue(
            all(item.metadata["candidate_origin"] == "existing" for item in results)
        )


    def test_integrity_retry_carries_validator_feedback_without_refusal_note(self) -> None:
        calls: list[tuple[str, dict]] = []

        def handler(*, system: str, user: str, **_: object) -> dict:
            calls.append((system, json.loads(user)))
            text = "TARGET" if len(calls) == 1 else "TARGET {name}"
            return {"status": "success", "text": text, "reason": ""}

        translator = ScriptedProvider("fixture-translator", handler)
        with tempfile.TemporaryDirectory() as directory:
            result = LocalizationOrchestrator(
                registry=ProviderRegistry({translator.name: translator}),
                config=OrchestratorConfig(
                    [translator.name], [], require_review=False, max_workers=1, grace_retries=1
                ),
                project=ProjectContext("TEST", "src", "dst"),
                work_dir=Path(directory),
            ).translate_one(Segment("fixture-token", "SOURCE {name}"))

        self.assertEqual(result.text, "TARGET {name}")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("previous_attempt_validation_failures", calls[0][1])
        feedback = calls[1][1]["previous_attempt_validation_failures"]
        self.assertEqual(feedback[0]["code"], "protected-token-mismatch")
        self.assertIn("{name}", feedback[0]["detail"])
        self.assertNotIn("retry after a refusal", calls[1][0])
        self.assertIn("failed deterministic validation", calls[1][0])

    def test_reference_cue_and_fallback_origin_escalate_past_triage(self) -> None:
        failing = ScriptedProvider(
            "fixture-primary", lambda **_: {"status": "error", "text": "", "reason": ""}
        )
        fallback = ScriptedProvider(
            "fixture-fallback", lambda **_: {"status": "success", "text": "TARGET", "reason": ""}
        )
        triager = ScriptedProvider("fixture-triager", lambda **_: {"verdict": "ok", "issues": []})
        strong = ScriptedProvider("fixture-strong", lambda **_: {"verdict": "ok", "issues": []})
        registry = ProviderRegistry(
            {item.name: item for item in (failing, fallback, triager, strong)}
        )
        cue = {"kind": "wordplay", "scene": "", "evidence_line_ids": ["cue-target"],
               "recognition_cue": "CUE", "translation_goal": "GOAL", "confidence": "high"}
        for chain, segment_id, reason in (
            ([fallback.name], "cue-target", "context_reference_cue"),
            ([failing.name, fallback.name], "fallback-target", "fallback_translation"),
        ):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                result = LocalizationOrchestrator(
                    registry=registry,
                    config=OrchestratorConfig(
                        chain, [strong.name], review_triager_chain=[triager.name],
                        strong_review_sample_percent=0, strong_review_high_risk_categories=[],
                    ),
                    project=ProjectContext("TEST", "src", "dst"),
                    context_pack=ContextPack(story_summary="STORY", reference_cues=(cue,)),
                    work_dir=Path(directory),
                ).translate_one(Segment(segment_id, "SOURCE"))
                self.assertIn(reason, result.metadata["strong_review_reasons"])

    def test_rewrite_retry_signals_follow_only_their_own_cause(self) -> None:
        rewrites: list[tuple[str, dict]] = []
        replies = iter([
            {"status": "success", "text": "TARGET"},            # drops {name}: integrity retry
            {"status": "refused", "text": ""},                  # refusal retry
            {"status": "success", "text": "TARGET {name}"},
        ])

        def rewriter_handler(*, system: str, user: str, **_: object) -> dict:
            rewrites.append((system, json.loads(user)))
            return next(replies)

        verdicts = iter([{"verdict": "rewrite", "issues": ["voice"]}, {"verdict": "ok", "issues": []}])
        translator = ScriptedProvider(
            "fixture-translator", lambda **_: {"status": "success", "text": "DRAFT {name}", "reason": ""}
        )
        judge = ScriptedProvider("fixture-judge", lambda **_: next(verdicts))
        rewriter = ScriptedProvider("fixture-rewriter", rewriter_handler)
        with tempfile.TemporaryDirectory() as directory:
            result = LocalizationOrchestrator(
                registry=ProviderRegistry({p.name: p for p in (translator, judge, rewriter)}),
                config=OrchestratorConfig(
                    [translator.name], [judge.name], rewriter_chain=[rewriter.name],
                    grace_retries=1, max_workers=1,
                ),
                project=ProjectContext("TEST", "src", "dst"),
                work_dir=Path(directory),
            ).translate_one(Segment("fixture-rewrite", "SOURCE {name}"))

        self.assertEqual(result.text, "TARGET {name}")
        self.assertEqual(len(rewrites), 3)
        self.assertIn("previous_attempt_validation_failures", rewrites[1][1])
        self.assertNotIn("previous_attempt_validation_failures", rewrites[2][1])
        self.assertNotIn("correction target. If permitted", rewrites[1][0])
        self.assertIn("correction target. If permitted", rewrites[2][0])

    def test_continuation_does_not_replay_unchanged_operational_chain(self) -> None:
        def unavailable(**_):
            raise ProviderFailure("provider_error", retryable=True, usage={"input_tokens": 3})

        with tempfile.TemporaryDirectory() as directory:
            providers = [ScriptedProvider(name, unavailable) for name in ("first", "second")]
            orchestrator = LocalizationOrchestrator(
                registry=ProviderRegistry({p.name: p for p in providers}),
                config=OrchestratorConfig(
                    ["first", "second"], [], require_review=False,
                    translator_retries={"first": 4, "second": 4},
                ),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            )
            segment = Segment("one", "SOURCE")
            initial = orchestrator.translate_one(segment)
            snapshot = initial.to_dict()
            with patch.object(orchestrator.audit, "append", side_effect=AssertionError("Pure status wrote audit")):
                status = orchestrator.continuation_status(segment, initial)
            self.assertFalse(status["eligible"])
            self.assertEqual(status["next_action"], "repair")
            self.assertEqual(status["issue_code"], "operational_route_unchanged")
            self.assertEqual(initial.to_dict(), snapshot)
            resumed = orchestrator.continue_many([segment], [initial])[0]
            repeated = orchestrator.continue_many([segment], {segment.id: resumed})[0]
            self.assertEqual([p.call_count for p in providers], [1, 1])
            self.assertEqual(repeated.attempts, initial.attempts)
            self.assertEqual(initial.to_dict(), snapshot)
            self.assertEqual(repeated.status, "failed")

    def test_continuation_uses_new_route_and_locks_successful_peers(self) -> None:
        def first_handler(*, user, **_):
            if json.loads(user)["translation_target"]["id"] == "done":
                return {"status": "success", "text": "TARGET_DONE"}
            raise ProviderFailure("timeout", retryable=True)

        with tempfile.TemporaryDirectory() as directory:
            first = ScriptedProvider("first", first_handler)
            successor = ScriptedProvider(
                "new-approved", lambda **_: {"status": "success", "text": "TARGET_NEW"},
            )
            registry = ProviderRegistry({p.name: p for p in (first, successor)})
            segments = [Segment("done", "SOURCE_DONE"), Segment("pending", "SOURCE_PENDING")]
            initial = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(["first"], [], require_review=False),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            ).translate_many(segments)
            orchestrator = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(["first", "new-approved"], [], require_review=False),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            )
            status = orchestrator.continuation_status(segments[1], initial[1])
            self.assertTrue(status["eligible"])
            self.assertEqual(status["route_binding"]["provider"], "new-approved")
            results = orchestrator.continue_many(segments, initial)
            self.assertIs(results[0], initial[0])
            self.assertEqual(results[0].to_dict(), initial[0].to_dict())
            self.assertEqual(results[1].attempts[0], initial[1].attempts[0])
            self.assertEqual((first.call_count, successor.call_count), (2, 1))
            self.assertEqual(results[1].translator, "new-approved")
            self.assertEqual(results[1].status, "translated")

    def test_continuation_carries_ordinals_when_explicit_semantic_budget_increases(self) -> None:
        replies = iter([
            {"status": "refused", "text": ""},
            {"status": "refused", "text": ""},
            {"status": "success", "text": "TARGET"},
        ])
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider("route", lambda **_: next(replies))
            registry = ProviderRegistry({"route": provider})
            segment = Segment("one", "SOURCE")
            initial_orchestrator = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    ["route"], [], require_review=False, translator_retries={"route": 1},
                ),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            )
            initial = initial_orchestrator.translate_one(segment)
            self.assertFalse(initial_orchestrator.continuation_status(segment, initial)["eligible"])
            orchestrator = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    ["route"], [], require_review=False, translator_retries={"route": 2},
                ),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            )
            status = orchestrator.continuation_status(segment, initial)
            self.assertEqual(status["route_binding"]["ordinal"], 3)
            result = orchestrator.continue_many([segment], [initial])[0]
            self.assertEqual([a.ordinal for a in result.attempts], [1, 2, 3])
            self.assertEqual(provider.call_count, 3)
            self.assertEqual(result.status, "translated")

    def test_continuation_requires_captured_repair_not_elapsed_time(self) -> None:
        class ConfiguredProvider(ScriptedProvider):
            endpoint = "before"

            def fingerprint(self):
                return {**super().fingerprint(), "model": "exact-model", "endpoint": self.endpoint}

        with tempfile.TemporaryDirectory() as directory:
            def handler(**_):
                if provider.endpoint == "before":
                    raise ProviderFailure("authentication_error", retryable=False)
                return {"status": "success", "text": "TARGET"}

            provider = ConfiguredProvider("route", handler)
            orchestrator = LocalizationOrchestrator(
                registry=ProviderRegistry({"route": provider}),
                config=OrchestratorConfig(
                    ["route"], [], require_review=False, translator_retries={"route": 0},
                ),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            )
            segment = Segment("one", "SOURCE")
            initial = orchestrator.translate_one(segment)
            self.assertFalse(orchestrator.continuation_status(segment, initial)["eligible"])
            provider.endpoint = "after"
            self.assertTrue(orchestrator.continuation_status(segment, initial)["eligible"])
            result = orchestrator.continue_many([segment], [initial])[0]
            self.assertEqual([a.ordinal for a in result.attempts], [1, 2])
            self.assertEqual(result.attempts[0].model, "exact-model")
            self.assertNotEqual(
                result.attempts[0].provider_fingerprint, result.attempts[1].provider_fingerprint,
            )
            self.assertEqual(result.status, "translated")

    def test_unknown_legacy_evidence_holds_only_affected_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider("route", lambda **_: {"status": "success", "text": "TARGET"})
            orchestrator = LocalizationOrchestrator(
                registry=ProviderRegistry({"route": provider}),
                config=OrchestratorConfig(["route"], [], require_review=False),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            )
            segments = [Segment("old", "SOURCE_OLD"), Segment("fresh", "SOURCE_FRESH")]
            old = TranslationResult(
                "old", "SOURCE_OLD",
                attempts=[AttemptRecord("translate", "route", "error", issue_code="timeout")],
            )
            result = orchestrator.continue_many(segments, [old])
            self.assertEqual(provider.call_count, 1)
            self.assertEqual([r.status for r in result], ["failed", "translated"])
            self.assertEqual(
                result[0].metadata["continuation"]["issue_code"], "historical_failure_evidence_unknown",
            )
            self.assertIsNone(result[0].attempts[0].operational)

    def test_continuation_resumes_rewrite_then_confirmation_without_retranslation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            translator = ScriptedProvider("translator", lambda **_: {"status": "success", "text": "DRAFT"})
            judge = ScriptedProvider(
                "judge", lambda **_: {"verdict": "rewrite", "issues": ["voice"]},
            )
            def unavailable(**_):
                raise ProviderFailure("provider_error", retryable=True)
            failed_rewriter = ScriptedProvider("old-rewriter", unavailable)
            new_rewriter = ScriptedProvider(
                "new-rewriter", lambda **_: {"status": "success", "text": "REVISED"},
            )
            registry = ProviderRegistry({p.name: p for p in (
                translator, judge, failed_rewriter, new_rewriter,
            )})
            segment = Segment("one", "SOURCE")
            initial = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    ["translator"], ["judge"], rewriter_chain=["old-rewriter"],
                ),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            ).translate_one(segment)
            self.assertEqual(initial.status, "failed")
            self.assertEqual(initial.text, "DRAFT")
            judge.handler = lambda **_: {"verdict": "ok", "issues": []}
            orchestrator = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    ["translator"], ["judge"], rewriter_chain=["old-rewriter", "new-rewriter"],
                ),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            )
            status = orchestrator.continuation_status(segment, initial)
            self.assertEqual(status["next_action"], "review-rewrite")
            self.assertEqual(status["route_binding"]["provider"], "new-rewriter")
            result = orchestrator.continue_many([segment], [initial])[0]
            self.assertEqual(result.status, "reviewed")
            self.assertEqual(result.text, "REVISED")
            self.assertEqual(result.candidate_generation, 2)
            self.assertEqual((translator.call_count, failed_rewriter.call_count), (1, 1))
            self.assertEqual((new_rewriter.call_count, judge.call_count), (1, 2))
            self.assertEqual(
                [a.ordinal for a in result.attempts if a.provider == "judge"], [1, 2],
            )
            self.assertEqual(result.attempts[-1].stage, "review-confirm")

    def test_metadata_only_continuation_status_never_creates_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider("route", lambda **_: self.fail("Metadata evaluation called provider"))
            root = Path(directory) / "read-only-status"
            with patch("game_l10n.orchestrator.require_localization_mode", side_effect=AssertionError("Status read execution mode")):
                orchestrator = LocalizationOrchestrator(
                    registry=ProviderRegistry({"route": provider}),
                    config=OrchestratorConfig(["route"], [], require_review=False),
                    project=ProjectContext("TEST", "src", "dst"), work_dir=root,
                    metadata_only=True,
                )
                prior = TranslationResult("one", "SOURCE")
                snapshot = prior.to_dict()
                status = orchestrator.continuation_status(Segment("one", "SOURCE"), prior)
                self.assertTrue(status["eligible"])
                self.assertEqual(prior.to_dict(), snapshot)
                self.assertIsNone(orchestrator.store)
                self.assertIsNone(orchestrator.audit)
                self.assertFalse(root.exists())
                for execute in (
                    lambda: orchestrator.translate_one(Segment("one", "SOURCE")),
                    lambda: orchestrator.translate_many([]),
                    lambda: orchestrator.review_existing_one(Segment("one", "SOURCE"), "TARGET"),
                    lambda: orchestrator.review_existing_many([], {}),
                    lambda: orchestrator.continue_many([], []),
                ):
                    with self.assertRaisesRegex(ValueError, "E_METADATA_ONLY_EXECUTION_FORBIDDEN"):
                        execute()
            self.assertEqual(provider.call_count, 0)

    def test_continuation_honors_authoritative_execution_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider("route", lambda **_: self.fail("Stopped work called provider"))
            orchestrator = LocalizationOrchestrator(
                registry=ProviderRegistry({"route": provider}),
                config=OrchestratorConfig(["route"], [], require_review=False),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            )
            with patch("game_l10n.orchestrator.require_localization_mode", side_effect=ValueError("USER_STOP")):
                with self.assertRaisesRegex(ValueError, "USER_STOP"):
                    orchestrator.continue_many([Segment("one", "SOURCE")], [])
            self.assertEqual(provider.call_count, 0)

    def test_new_exact_model_can_continue_without_resetting_old_model_budget(self) -> None:
        class SelectedModelProvider(ScriptedProvider):
            model = "original-exact-model"

            def fingerprint(self):
                return {**super().fingerprint(), "model": self.model, "effort": "high"}

        with tempfile.TemporaryDirectory() as directory:
            provider = SelectedModelProvider(
                "route", lambda **_: {"status": "refused", "text": ""},
            )
            orchestrator = LocalizationOrchestrator(
                registry=ProviderRegistry({"route": provider}),
                config=OrchestratorConfig(
                    ["route"], [], require_review=False, translator_retries={"route": 0},
                ),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            )
            segment = Segment("one", "SOURCE")
            initial = orchestrator.translate_one(segment)
            self.assertFalse(orchestrator.continuation_status(segment, initial)["eligible"])
            provider.model = "new-approved-exact-model"
            provider.handler = lambda **_: {"status": "success", "text": "TARGET"}
            status = orchestrator.continuation_status(segment, initial)
            self.assertTrue(status["eligible"])
            self.assertEqual(status["route_binding"]["model"], "new-approved-exact-model")
            result = orchestrator.continue_many([segment], [initial])[0]
            self.assertEqual(
                [(a.model, a.ordinal) for a in result.attempts],
                [("original-exact-model", 1), ("new-approved-exact-model", 1)],
            )

    def test_semantic_budget_stays_consumed_after_transport_config_changes(self) -> None:
        class ConfiguredProvider(ScriptedProvider):
            endpoint = "before"

            def fingerprint(self):
                return {**super().fingerprint(), "model": "exact", "endpoint": self.endpoint}

        with tempfile.TemporaryDirectory() as directory:
            provider = ConfiguredProvider("route", lambda **_: {"status": "refused", "text": ""})
            orchestrator = LocalizationOrchestrator(
                registry=ProviderRegistry({"route": provider}),
                config=OrchestratorConfig(
                    ["route"], [], require_review=False, translator_retries={"route": 0},
                ),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            )
            segment = Segment("one", "SOURCE")
            initial = orchestrator.translate_one(segment)
            provider.endpoint = "after"
            self.assertFalse(orchestrator.continuation_status(segment, initial)["eligible"])
            resumed = orchestrator.continue_many([segment], [initial])[0]
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(resumed.attempts, initial.attempts)

    def test_review_successor_with_same_model_keeps_distinct_provider_budget(self) -> None:
        class ExactModelProvider(ScriptedProvider):
            def fingerprint(self):
                return {**super().fingerprint(), "model": "same-exact-model"}

        with tempfile.TemporaryDirectory() as directory:
            translator = ScriptedProvider("translator", lambda **_: {"status": "success", "text": "DRAFT"})
            first = ExactModelProvider("first-review", lambda **_: {"verdict": "refused", "issues": []})
            second = ExactModelProvider("second-review", lambda **_: {"verdict": "ok", "issues": []})
            result = LocalizationOrchestrator(
                registry=ProviderRegistry({p.name: p for p in (translator, first, second)}),
                config=OrchestratorConfig(
                    ["translator"], ["first-review", "second-review"],
                    review_attempt_limits={"first-review": 1, "second-review": 1},
                ),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            ).translate_one(Segment("one", "SOURCE"))
            self.assertEqual(result.status, "reviewed")
            self.assertEqual((first.call_count, second.call_count), (1, 1))
            self.assertEqual(result.reviewer, "second-review")
            self.assertEqual([a.ordinal for a in result.attempts[1:]], [1, 1])

    def test_attempt_evidence_roundtrips_and_legacy_fields_remain_unknown(self) -> None:
        legacy = LocalizationOrchestrator._result_from_state({
            "segment_id": "one", "source": "SOURCE",
            "attempts": [{"stage": "translate", "provider": "route", "status": "error"}],
        })
        self.assertIsNone(legacy.attempts[0].operational)
        self.assertIsNone(legacy.attempts[0].retryable)
        self.assertEqual(legacy.attempts[0].provider_fingerprint, "")
        current = TranslationResult(
            "one", "SOURCE", attempts=[AttemptRecord(
                "translate", "route", "error", operational=True,
                provider_fingerprint=stable_hash({"model": "exact"}), retryable=False,
            )],
        )
        decoded = LocalizationOrchestrator._result_from_state(current.to_dict())
        self.assertEqual(decoded.to_dict(), current.to_dict())

    def test_continuation_does_not_accept_rejected_legacy_review_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            translator = ScriptedProvider(
                "translator", lambda **_: {"status": "success", "text": "DRAFT {name}"},
            )
            reviewer = ScriptedProvider("reviewer", lambda **_: {
                "decision": "approved", "text": "INVALID", "meaning_preserved": True,
                "issues": [],
            })
            orchestrator = LocalizationOrchestrator(
                registry=ProviderRegistry({p.name: p for p in (translator, reviewer)}),
                config=OrchestratorConfig(["translator"], ["reviewer"]),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            )
            segment = Segment("one", "SOURCE {name}")
            initial = orchestrator.translate_one(segment)
            self.assertEqual(initial.status, "failed")
            status = orchestrator.continuation_status(segment, initial)
            self.assertFalse(status["eligible"])
            self.assertEqual(status["next_action"], "repair")
            result = orchestrator.continue_many([segment], [initial])[0]
            self.assertEqual(result.status, "failed")
            self.assertEqual((translator.call_count, reviewer.call_count), (1, 1))

    def test_new_triager_can_resolve_operationally_unresolved_triage(self) -> None:
        def unavailable(**_):
            raise ProviderFailure("timeout", retryable=True)

        with tempfile.TemporaryDirectory() as directory:
            translator = ScriptedProvider("translator", lambda **_: {"status": "success", "text": "DRAFT"})
            old_triager = ScriptedProvider("old-triager", unavailable)
            strong = ScriptedProvider("strong", unavailable)
            new_triager = ScriptedProvider(
                "new-triager", lambda **_: {"verdict": "ok", "issues": []},
            )
            registry = ProviderRegistry({p.name: p for p in (
                translator, old_triager, strong, new_triager,
            )})
            segment = Segment("one", "SOURCE")
            initial = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    ["translator"], ["strong"], review_triager_chain=["old-triager"],
                    strong_review_sample_percent=0, strong_review_high_risk_categories=[],
                ),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            ).translate_one(segment)
            orchestrator = LocalizationOrchestrator(
                registry=registry,
                config=OrchestratorConfig(
                    ["translator"], ["strong"],
                    review_triager_chain=["old-triager", "new-triager"],
                    strong_review_sample_percent=0, strong_review_high_risk_categories=[],
                ),
                project=ProjectContext("TEST", "src", "dst"), work_dir=Path(directory),
            )
            status = orchestrator.continuation_status(segment, initial)
            self.assertEqual(status["next_action"], "review-triage")
            self.assertEqual(status["route_binding"]["provider"], "new-triager")
            result = orchestrator.continue_many([segment], [initial])[0]
            self.assertEqual(result.status, "reviewed")
            self.assertEqual(result.reviewer, "new-triager")
            self.assertEqual((translator.call_count, old_triager.call_count, strong.call_count), (1, 1, 1))

if __name__ == "__main__":
    unittest.main()
