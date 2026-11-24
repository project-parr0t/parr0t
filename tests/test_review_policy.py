from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from game_l10n.models import ProjectContext, Segment
from game_l10n.orchestrator import LocalizationOrchestrator, OrchestratorConfig
from game_l10n.providers import ProviderRegistry, ScriptedProvider
from game_l10n.review_policy import configure_review_policy, judge_calls, review_policy_status
from game_l10n.workspace import initialize_workspace, load_config


class ReviewPolicyTests(unittest.TestCase):
    def orchestrator(self, root, *, deferred=False, configured=True, rewrite=False):
        translator = ScriptedProvider("translator", lambda **_: {"status": "success", "text": "\ub300\uc0c1", "reason": ""})
        sol = ScriptedProvider("openai-sol", lambda **_: {"verdict": "rewrite" if rewrite and sol.call_count == 1 else "error", "issues": ["fluency"] if rewrite else []})
        fallback = ScriptedProvider("fallback", lambda **_: {"verdict": "ok" if fallback.call_count == 3 else "error", "issues": []})
        editor = ScriptedProvider("editor", lambda **_: {"status": "success", "text": "\uac1c\uc120", "reason": ""})
        providers = {item.name: item for item in (translator, sol, fallback, editor)}
        orchestrator = LocalizationOrchestrator(
            registry=ProviderRegistry(providers),
            config=OrchestratorConfig(
                [translator.name], [sol.name, fallback.name],
                review_attempt_limits={sol.name: 2, **({fallback.name: 3} if configured else {})},
                review_max_rounds=10, grace_retries=8, retry_refusals_once=True,
                rewriter_chain=[editor.name], use_style_memory=False,
                defer_fallbacks_until_primary_complete=deferred, preserve_source_on_failure=True,
            ),
            project=ProjectContext("TEST", "en", "ko"), work_dir=root,
        )
        return orchestrator, providers

    def test_explicit_primary_and_fallback_total_in_single_and_deferred_paths(self):
        for deferred in (False, True):
            with self.subTest(deferred=deferred), tempfile.TemporaryDirectory() as directory:
                orchestrator, providers = self.orchestrator(Path(directory), deferred=deferred)
                segment = Segment("fixture", "Source")
                result = orchestrator.translate_many([segment])[0] if deferred else orchestrator.translate_one(segment)
                self.assertEqual((result.status, result.reviewer), ("reviewed", "fallback"))
                self.assertEqual(providers["openai-sol"].call_count, 2)
                self.assertEqual(providers["fallback"].call_count, 3)
                self.assertEqual(providers["translator"].call_count, 1)

    def test_default_fallback_policy_runs_without_human_count_and_success_is_reused(self):
        for deferred in (False, True):
            with self.subTest(deferred=deferred), tempfile.TemporaryDirectory() as directory:
                orchestrator, providers = self.orchestrator(Path(directory), deferred=deferred, configured=False)
                segment = Segment("fixture", "Source")
                run = lambda: orchestrator.translate_many([segment])[0] if deferred else orchestrator.translate_one(segment)
                result = run()
                self.assertEqual(run().status, "reviewed")
                self.assertEqual(result.status, "reviewed")
                self.assertEqual(providers["openai-sol"].call_count, 2)
                self.assertEqual(providers["translator"].call_count, 1)
                self.assertEqual(providers["fallback"].call_count, 3)

    def test_confirmation_shares_the_explicit_route_call_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator, providers = self.orchestrator(Path(directory), rewrite=True)
            result = orchestrator.translate_one(Segment("fixture", "Source"))
            self.assertEqual((result.status, result.text), ("reviewed", "\uac1c\uc120"))
            self.assertEqual(providers["openai-sol"].call_count, 2)
            self.assertEqual(providers["editor"].call_count, 1)

    def test_user_model_binding_is_exact_and_other_configuration_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            sdk.mkdir()
            workspace = root / "workspace"
            with patch("game_l10n.workspace.inventory_sdk_branches", return_value={"source": "fixture", "fingerprint": "fixture", "candidates": []}):
                initialize_workspace(workspace, sdk, provider_preset="custom", git_choice="disabled")
            path = workspace / "game-l10n.json"
            config = json.loads(path.read_text(encoding="utf-8"))
            config["providers"].append({"name": "fallback", "type": "compatible_chat", "model": "EXACT_FIXTURE_MODEL", "api_key_env": "FIXTURE_KEY"})
            config["routing"]["reviewers"].append("fallback")
            config["project"]["style_guide"] = "FIXTURE_STYLE"
            path.write_text(json.dumps(config), encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "MODEL_BINDING_INVALID"):
                configure_review_policy(workspace, model="OTHER_MODEL", max_calls=4, user_requested=True)
            self.assertEqual(path.read_bytes(), before)
            settings = {"triage_max_calls": 1, "strong_max_calls": 2, "review_max_workers": 4,
                        "triage_max_workers": 3, "strong_max_workers": 2}
            result = configure_review_policy(workspace, model="EXACT_FIXTURE_MODEL", max_calls=4,
                                             settings=settings, user_requested=True)
            self.assertEqual((result["provider"], result["status"]), ("fallback", "configured"))
            updated = load_config(workspace)
            self.assertEqual(updated["routing"]["review_attempt_limits"]["fallback"], 4)
            self.assertEqual(updated["project"]["style_guide"], "FIXTURE_STYLE")
            self.assertEqual(updated["providers"][-1]["model"], "EXACT_FIXTURE_MODEL")
            self.assertNotIn("fallback", review_policy_status(updated)["defaulted_fallback_providers"])
            self.assertEqual(result["review_limits"], settings)
            self.assertEqual({key: updated["routing"][key] for key in settings}, settings)

    def test_native_failed_only_chain_preserves_explicit_two_call_policy(self):
        from game_l10n.native_pass import native_control_step, prepare_native_tasks
        from game_l10n.native_review import _bound_result, collect_native_review
        from game_l10n.formats import read_segments
        from tests.test_native_pass import review_segment
        from tests import write_semantic_result_rows
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "game-l10n.json").write_text(json.dumps({
                "routing": {"reviewers": ["native-sol"], "review_attempt_limits": {"native-sol": 2}},
            }), encoding="utf-8")
            segments = [review_segment(name) for name in ("a", "b")]
            source = root / "source.jsonl"
            source.write_text("".join(json.dumps(asdict(segment)) + "\n" for segment in segments), encoding="utf-8")
            segments = read_segments(source)
            prepare_native_tasks(root, source, segments, root / "tasks", pass_kind="review", max_records=2, min_records=1,
                                 route_binding={"provider": "native-sol", "model": "fixture-review-exact", "attempt": 1})
            manifest_path = root / "tasks" / "manifest.json"
            results = root / "results"
            for _ in range(2):
                step = native_control_step(root, manifest_path, results)
                relay = json.loads((root / step["relay"]["dispatch"]).read_text(encoding="utf-8"))
                for binding in relay["task_bindings"]:
                    ids = [json.loads(line)["target_id"] for line in (root / binding["task"]).read_text(encoding="utf-8").splitlines()]
                    destination = root / binding["result"]
                    write_semantic_result_rows(
                        destination,
                        [
                            {"target_id": target, "status": "refused", "text": ""}
                            for target in ids
                        ],
                    )
            done = native_control_step(root, manifest_path, results)
            self.assertEqual(done["action"], "aggregate_completed_pass")
            self.assertEqual(done["review_split"]["semantic_passes"], 2)
            self.assertEqual(done["review_split"]["terminal_reason"], "review_call_limit")
            summary = collect_native_review(root, source, manifest_path, root / "collected", tier="strong")
            self.assertEqual(summary["fallback_pending"], 2)
            for segment in read_segments(root / "collected" / "fallback-input.jsonl"):
                self.assertEqual(judge_calls(_bound_result(segment).attempts, "native-sol"), 2)


    def test_tier_caps_bound_weak_and_non_sol_strong_refusals(self):
        with tempfile.TemporaryDirectory() as directory:
            translator = ScriptedProvider("translator", lambda **_: {"status": "success", "text": "\ub300\uc0c1", "reason": ""})
            weak = ScriptedProvider("weak", lambda **_: {"verdict": "refused", "issues": []})
            strong = ScriptedProvider("strong", lambda **_: {"verdict": "refused", "issues": []})
            orchestrator = LocalizationOrchestrator(
                registry=ProviderRegistry({item.name: item for item in (translator, weak, strong)}),
                config=OrchestratorConfig(["translator"], ["strong"], review_triager_chain=["weak"],
                    triage_max_calls=2, strong_max_calls=3, grace_retries=8, use_style_memory=False),
                project=ProjectContext("TEST", "en", "ko"), work_dir=Path(directory),
            )
            result = orchestrator.translate_one(Segment("fixture", "Source"))
            self.assertEqual(result.status, "failed")
            self.assertEqual((weak.call_count, strong.call_count), (2, 3))

    def test_global_and_tier_worker_limits_bound_actual_parallel_review(self):
        import threading
        with tempfile.TemporaryDirectory() as directory:
            active = {"weak": 0, "strong": 0}
            peak = {"weak": 0, "strong": 0}
            lock = threading.Lock()
            def handler(role, expected, verdict):
                barrier = threading.Barrier(expected)
                first = True
                def run(**kwargs):
                    nonlocal first
                    verdict_result = {"verdict": verdict, "issues": ["meaning"] if verdict == "rewrite" else []}
                    with lock:
                        if first:
                            # An exact binding's initial request must finish before its peers enter.
                            first = False
                            return verdict_result
                        active[role] += 1
                        peak[role] = max(peak[role], active[role])
                    barrier.wait(timeout=5)
                    with lock:
                        active[role] -= 1
                    return verdict_result
                return run
            translator = ScriptedProvider("translator", lambda **_: {"status": "success", "text": "\ub300\uc0c1", "reason": ""})
            weak = ScriptedProvider("weak", handler("weak", 3, "rewrite"))
            strong = ScriptedProvider("strong", handler("strong", 2, "ok"))
            orchestrator = LocalizationOrchestrator(
                registry=ProviderRegistry({item.name: item for item in (translator, weak, strong)}),
                config=OrchestratorConfig(["translator"], ["strong"], review_triager_chain=["weak"],
                    max_workers=8, review_max_workers=4, triage_max_workers=3, strong_max_workers=2,
                    defer_fallbacks_until_primary_complete=True, use_style_memory=False),
                project=ProjectContext("TEST", "en", "ko"), work_dir=Path(directory),
            )
            self.assertEqual(orchestrator.translate_many([Segment("initial", "Initial source")])[0].status, "reviewed")
            results = orchestrator.translate_many([Segment(f"fixture-{index}", f"Source {index}") for index in range(6)])
            self.assertTrue(all(result.status == "reviewed" for result in results))
            self.assertEqual(peak, {"weak": 3, "strong": 2})

    def test_compact_native_history_does_not_reset_a_fallback_budget(self):
        from game_l10n.models import AttemptRecord, TranslationResult
        with tempfile.TemporaryDirectory() as directory:
            orchestrator, providers = self.orchestrator(Path(directory))
            from game_l10n.store import stable_hash
            provider = providers["fallback"]
            fingerprint = {**provider.fingerprint(), "model": "fixture/exact-fallback"}
            result = TranslationResult("fixture", "Source")
            orchestrator._set_candidate(result, "\ub300\uc0c1")
            result.attempts.append(AttemptRecord(
                "review", "fallback", "error", ordinal=2,
                model=fingerprint["model"], candidate_generation=result.candidate_generation,
                provider_fingerprint=stable_hash(fingerprint), operational=False, retryable=True,
            ))
            with patch.object(provider, "fingerprint", return_value=fingerprint):
                verdict = orchestrator._judge_with_refusal_retry(
                    result, provider, Segment("fixture", "Source"), result.text, [],
                )
            self.assertIsNone(verdict)
            self.assertEqual(provider.call_count, 1)
            self.assertEqual([attempt.ordinal for attempt in result.attempts], [2, 3])
            self.assertTrue(all(attempt.model == fingerprint["model"] for attempt in result.attempts))
            self.assertTrue(all(attempt.provider_fingerprint == stable_hash(fingerprint) for attempt in result.attempts))
            self.assertEqual(judge_calls(result.attempts, "fallback", model=fingerprint["model"]), 3)

    def test_model_identity_never_creates_a_cap_or_combines_distinct_routes(self):
        from game_l10n.models import AttemptRecord
        from game_l10n.review_policy import review_call_limit, validate_review_settings
        self.assertEqual(review_call_limit("custom-sol", ["custom-sol"], {"custom-sol": 9},
                                          model="fixture/gpt-6.1-sol:high"), 9)
        self.assertIsNone(review_call_limit("native-sol", ["native-sol"], {}))
        self.assertEqual(review_call_limit("fallback", ["primary", "fallback"], {}), 3)
        self.assertEqual(review_call_limit("fallback", ["primary", "fallback"], {"fallback": 7}, tier_limit=5), 5)
        attempts = [AttemptRecord("review", "native-sol", "error", model="one/gpt-6.1-sol:high", ordinal=2),
                    AttemptRecord("review", "other-sol", "error", model="two/gpt-6.1-sol:high", ordinal=8)]
        self.assertEqual(judge_calls(attempts, "native-sol", model="one/gpt-6.1-sol:high"), 2)
        validate_review_settings({"review_max_workers": 128})

    def test_model_cutover_does_not_charge_a_known_different_model(self):
        from game_l10n.models import AttemptRecord
        attempts = [
            AttemptRecord("review", "native-review", "error", model="fixture/old:high", ordinal=7),
            AttemptRecord("review", "native-review", "error", model="fixture/new:high", ordinal=1),
            AttemptRecord("review-confirm", "other-role", "error", model="fixture/new:high", ordinal=2),
        ]
        self.assertEqual(judge_calls(attempts, "native-review", model="fixture/new:high"), 2)
        self.assertEqual(judge_calls(attempts, "native-review", model="fixture/old:high"), 7)
        attempts.append(AttemptRecord("review", "native-review", "error", ordinal=3))
        self.assertEqual(judge_calls(attempts, "native-review", model="fixture/new:high"), 3)
        self.assertEqual(judge_calls(attempts, "native-review"), 7)

    def test_confirmed_operational_failures_do_not_spend_semantic_review_calls(self):
        from game_l10n.models import AttemptRecord
        model = "fixture/exact-review"
        attempts = [
            AttemptRecord("review", "denied-adapter", "error", model=model, ordinal=99, operational=True),
            AttemptRecord("review-triage", "denied-adapter", "error", model=model, ordinal=99, operational=True),
        ]
        self.assertEqual(judge_calls(attempts, "healthy-adapter", model=model), 0)
        self.assertEqual(judge_calls(attempts, "healthy-adapter", model=model, stage_prefix="review-triage"), 0)
        attempts.append(AttemptRecord("review", "healthy-adapter", "error", model=model, ordinal=1, operational=False))
        self.assertEqual(judge_calls(attempts, "healthy-adapter", model=model), 1)
        attempts.append(AttemptRecord("review-confirm", "legacy-adapter", "error", model=model, ordinal=2))
        self.assertEqual(judge_calls(attempts, "healthy-adapter", model=model), 2)

    def test_review_physical_ordinals_subtract_only_distinct_known_operational_events(self):
        from game_l10n.models import AttemptRecord
        model = "fixture/exact-review"
        attempts = [
            AttemptRecord("review", "route", "error", model=model, ordinal=1, operational=True),
            AttemptRecord("review", "route", "error", model=model, ordinal=1, operational=True),
            AttemptRecord("review", "route", "approved", model=model, ordinal=2, operational=False),
        ]
        self.assertEqual(judge_calls(attempts, "route", model=model), 1)
        attempts.append(AttemptRecord("review-confirm", "route", "error", model=model, ordinal=3))
        self.assertEqual(judge_calls(attempts, "route", model=model), 2)
        attempts.extend([
            AttemptRecord("review", "route", "error", model=model, ordinal=4, operational=True),
            AttemptRecord("review-confirm", "route", "approved", model=model, ordinal=7, operational=False),
        ])
        self.assertEqual(judge_calls(attempts, "route", model=model), 5)


if __name__ == "__main__":
    unittest.main()
