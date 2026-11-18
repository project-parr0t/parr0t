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
                review_attempt_limits={fallback.name: 3} if configured else {},
                review_max_rounds=10, grace_retries=8, retry_refusals_once=True,
                rewriter_chain=[editor.name], use_style_memory=False,
                defer_fallbacks_until_primary_complete=deferred, preserve_source_on_failure=True,
            ),
            project=ProjectContext("TEST", "en", "ko"), work_dir=root,
        )
        return orchestrator, providers

    def test_sol_cap_and_explicit_fallback_total_in_single_and_deferred_paths(self):
        for deferred in (False, True):
            with self.subTest(deferred=deferred), tempfile.TemporaryDirectory() as directory:
                orchestrator, providers = self.orchestrator(Path(directory), deferred=deferred)
                segment = Segment("fixture", "Source")
                result = orchestrator.translate_many([segment])[0] if deferred else orchestrator.translate_one(segment)
                self.assertEqual((result.status, result.reviewer), ("reviewed", "fallback"))
                self.assertEqual(providers["openai-sol"].call_count, 2)
                self.assertEqual(providers["fallback"].call_count, 3)
                self.assertEqual(providers["translator"].call_count, 1)

    def test_missing_policy_stays_pending_and_resume_does_not_repeat_primary(self):
        for deferred in (False, True):
            with self.subTest(deferred=deferred), tempfile.TemporaryDirectory() as directory:
                orchestrator, providers = self.orchestrator(Path(directory), deferred=deferred, configured=False)
                segment = Segment("fixture", "Source")
                run = lambda: orchestrator.translate_many([segment])[0] if deferred else orchestrator.translate_one(segment)
                pending = run()
                self.assertEqual(pending.status, "failed")
                self.assertEqual(pending.text, "\ub300\uc0c1")
                self.assertEqual(pending.metadata["review_policy_required"], "fallback")
                self.assertNotIn("All review routes were exhausted", pending.errors)
                self.assertEqual(providers["fallback"].call_count, 0)
                orchestrator.config.review_attempt_limits["fallback"] = 3
                result = run()
                self.assertEqual(result.status, "reviewed")
                self.assertEqual(providers["openai-sol"].call_count, 2)
                self.assertEqual(providers["translator"].call_count, 1)
                self.assertEqual(providers["fallback"].call_count, 3)

    def test_confirmation_shares_the_sol_call_budget(self):
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
            self.assertEqual(review_policy_status(updated)["missing_fallback_providers"], [])
            self.assertEqual(result["review_limits"], settings)
            self.assertEqual({key: updated["routing"][key] for key in settings}, settings)

    def test_native_failed_only_chain_stops_after_two_and_preserves_count(self):
        from game_l10n.native_pass import native_control_step, prepare_native_tasks
        from game_l10n.native_review import _bound_result, collect_native_review
        from game_l10n.formats import read_segments
        from tests.test_native_pass import review_segment
        from tests import write_semantic_result_rows
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [review_segment(name) for name in ("a", "b")]
            source = root / "source.jsonl"
            source.write_text("".join(json.dumps(asdict(segment)) + "\n" for segment in segments), encoding="utf-8")
            segments = read_segments(source)
            prepare_native_tasks(root, source, segments, root / "tasks", pass_kind="review", max_records=2, min_records=1)
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
                def run(**kwargs):
                    with lock:
                        active[role] += 1
                        peak[role] = max(peak[role], active[role])
                    barrier.wait(timeout=5)
                    with lock:
                        active[role] -= 1
                    return {"verdict": verdict, "issues": ["meaning"] if verdict == "rewrite" else []}
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
            results = orchestrator.translate_many([Segment(f"fixture-{index}", f"Source {index}") for index in range(6)])
            self.assertTrue(all(result.status == "reviewed" for result in results))
            self.assertEqual(peak, {"weak": 3, "strong": 2})

    def test_compact_native_history_does_not_reset_a_fallback_budget(self):
        from game_l10n.models import AttemptRecord, TranslationResult
        with tempfile.TemporaryDirectory() as directory:
            orchestrator, providers = self.orchestrator(Path(directory))
            result = TranslationResult("fixture", "Source", "\ub300\uc0c1", attempts=[
                AttemptRecord("review", "fallback", "error", ordinal=2, candidate_generation=1),
            ])
            verdict = orchestrator._judge_with_refusal_retry(result, providers["fallback"], Segment("fixture", "Source"), "\ub300\uc0c1", [])
            self.assertIsNone(verdict)
            self.assertEqual(providers["fallback"].call_count, 1)
            self.assertEqual(judge_calls(result.attempts, "fallback"), 3)


if __name__ == "__main__":
    unittest.main()
