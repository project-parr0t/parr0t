from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from game_l10n.cli import build_parser
from game_l10n.errors import ConfigurationError
from game_l10n.formats import read_segments, read_translation_results
from game_l10n.models import Segment
from game_l10n.native_pass import merge_native_pass, native_control_step, seal_native_wave_relay
from game_l10n.native_runtime import configure_native_runtime, record_native_launch
from tests.test_native_pass import review_segment
from tests.omp_isolation_fixture import bind_omp_isolation


class CustomNativeRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        bind_omp_isolation(self, self.root)
        (self.root / "context").mkdir()
        (self.root / "context/glossary.json").write_text("[]", encoding="utf-8")
        self.config = {
            "provider_preset": "custom", "execution_mode": "custom_provider", "providers": [],
            "project": {"source_language": "en", "target_language": "ko"},
            "routing": {
                "translators": ["native-luna", "native-sol", "native-qwen"],
                "translator_retries": {"native-luna": 5, "native-sol": 3},
                "review_triagers": ["native-luna"],
                "reviewers": ["native-sol", "native-qwen"],
                "rewriters": ["native-qwen", "native-sol"],
                "max_workers": 1,
            },
        }
        configure_native_runtime(self.root, "omp", {"native-qwen": "fixture/qwen-exact"})

    def cli(self, command, *arguments, expected_exit=0):
        args = build_parser().parse_args([command, "--workspace", str(self.root), *arguments])
        output = io.StringIO()
        with patch("game_l10n.cli.load_config", return_value=self.config), \
             patch("game_l10n.cli.require_sdk_activation"), redirect_stdout(output):
            self.assertEqual(args.func(args), expected_exit)
        return json.loads(output.getvalue())

    @staticmethod
    def write_rows(path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def source(self, name, segments):
        path = self.root / name
        self.write_rows(path, [asdict(segment) for segment in segments])
        return name

    def run_pass(self, source, directory, rows, *, kind="translation", provider=None,
                 attempt=1, tier=None):
        arguments = ["--input", source, "--output", directory, "--pass-kind", kind,
                     "--route-attempt", str(attempt)]
        if provider:
            arguments += ["--native-provider", provider]
        if tier:
            arguments += ["--review-tier", tier]
        self.cli("prepare-native-pass", *arguments)
        manifest_path = self.root / directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        bound_provider = manifest["route_binding"]["provider"]
        result_dir = self.root / (directory + "-results")
        step = native_control_step(self.root, manifest_path, result_dir,
                                   require_runtime=True, require_relay_receipt=True)
        self.assertEqual(step["action"], "dispatch_isolated_wave_relay")
        relay_path = self.root / step["relay"]["dispatch"]
        relay = json.loads(relay_path.read_text(encoding="utf-8"))
        self.assertEqual(relay["route_binding"], manifest["route_binding"])
        self.assertEqual(relay["launch_contract"]["route"], bound_provider)
        self.assertEqual(relay["launch_contract"]["spawn_model_parameter"], "tasks[].model")
        if bound_provider == "native-qwen":
            self.assertEqual(relay["launch_contract"]["model"], "fixture/qwen-exact")
        record_native_launch(self.root, relay_path, event="capability-available")
        self.assertEqual(len(relay["task_bindings"]), 1)
        binding = relay["task_bindings"][0]
        record_native_launch(self.root, relay_path, event="worker-started", task=binding["task"],
                             worker_id="fixture-worker", model=relay["launch_contract"]["model"],
                             observed_model=relay["launch_contract"]["model"])
        self.write_rows(self.root / binding["result"], [
            {"target_id": target, "status": status, "text": text,
             "provider": bound_provider, "issues": []} for target, status, text in rows
        ])
        seal_native_wave_relay(self.root, relay_path, relay_ended=True)
        finished = native_control_step(self.root, manifest_path, result_dir,
                                       require_runtime=True, require_relay_receipt=True)
        self.assertEqual(finished["action"], "aggregate_completed_pass")
        merged = self.root / directory / "merged.jsonl"
        self.assertTrue(merge_native_pass(self.root, read_segments(self.root / source), merged,
                                         manifest_path=manifest_path)["complete"])
        return merged.relative_to(self.root).as_posix(), manifest

    def test_cli_custom_chain_uses_six_luna_four_sol_then_exact_qwen_and_locks_successes(self):
        source = self.source("source.jsonl", [Segment(key, "SOURCE") for key in "abcd"])
        first, _ = self.run_pass(source, "first", [("a", "success", "TARGET_A"),
                            ("b", "refused", ""), ("c", "error", ""), ("d", "refused", "")])
        state = self.cli("start-native-translation-retries", "--input", source,
                         "--results", first, "--output", "retry")
        expected = [("native-luna", n) for n in range(2, 7)] + [
            ("native-sol", n) for n in range(1, 5)] + [("native-qwen", 1)]
        for provider, attempt in expected:
            self.assertEqual((state["next_provider"], state["next_attempt"]), (provider, attempt))
            pending = read_segments(self.root / state["pending_input"])
            self.assertNotIn("a", [segment.id for segment in pending])
            if provider != "native-luna":
                self.assertNotIn("b", [segment.id for segment in pending])
            if provider == "native-qwen":
                self.assertEqual([segment.id for segment in pending], ["d"])
            success = {("native-luna", 6): "b", ("native-sol", 4): "c", ("native-qwen", 1): "d"}.get((provider, attempt))
            results, manifest = self.run_pass(state["pending_input"], state["native_pass_output"],
                [(segment.id, "success" if segment.id == success else "refused",
                  "TARGET_" + segment.id.upper() if segment.id == success else "") for segment in pending],
                provider=provider, attempt=attempt)
            self.assertEqual(manifest["route_binding"], {"provider": provider, "attempt": attempt})
            state = self.cli("advance-native-translation-retries", "--state", state["state"],
                             "--results", results)
        self.assertEqual((state["next_action"], state["candidate_count"], state["pending_count"]),
                         ("complete", 4, 0))
        prepared = self.cli("prepare-native-followup", "--input", source,
                            "--primary-results", state["pre_fallback_results"], "--output", "followup",
                            "--retry-state", state["state"], "--exchange-prefix", "fixture")
        self.assertEqual(prepared["fallback_bundles"], [])
        joined = self.cli("collect-native-followup", "--input", source, "--followup", "followup",
                          "--review-output", "review.jsonl", "--unresolved-output", "unresolved.jsonl")
        self.assertEqual(joined["candidate_count"], 4)
        self.assertEqual([s.metadata["_parr0t_candidate"]["provider"]
                          for s in read_segments(self.root / "review.jsonl")],
                         ["native-luna", "native-luna", "native-sol", "native-qwen"])

    def test_custom_review_failure_uses_next_native_judge_then_configured_rewriter(self):
        source = self.source("review.jsonl", [review_segment("fixture")])
        self.run_pass(source, "strong", [("fixture", "refused", "")], kind="review", tier="strong")
        self.cli("collect-native-review", "--input", source, "--manifest", "strong/manifest.json",
                 "--output", "strong-joined", "--tier", "strong")
        fallback = "strong-joined/fallback-input.jsonl"
        self.run_pass(fallback, "fallback", [("fixture", "success", "REWRITE voice")],
                      kind="review", tier="strong")
        self.cli("collect-native-review", "--input", fallback, "--manifest", "fallback/manifest.json",
                 "--output", "fallback-joined", "--tier", "strong")
        rewrite = "fallback-joined/rewrite-input.jsonl"
        self.run_pass(rewrite, "rewrite", [("fixture", "success", "REVISED")], kind="rewrite")
        self.cli("collect-native-rewrite", "--input", rewrite, "--manifest", "rewrite/manifest.json",
                 "--output", "rewrite-joined")
        confirmation = "rewrite-joined/confirmation-input.jsonl"
        self.run_pass(confirmation, "confirm", [("fixture", "success", "OK")], kind="review", tier="strong")
        self.cli("collect-native-review", "--input", confirmation, "--manifest", "confirm/manifest.json",
                 "--output", "confirmed", "--tier", "strong")
        accepted = read_translation_results(self.root / "confirmed/accepted.json")[0]
        self.assertEqual(accepted.reviewer, "native-sol")
        self.assertEqual(accepted.text, "REVISED")
        self.assertEqual(accepted.candidate_generation, 2)
        self.assertEqual([(a.stage, a.provider) for a in accepted.attempts], [
            ("review", "native-sol"), ("review", "native-qwen"),
            ("review-rewrite", "native-qwen"), ("review", "native-sol")])

    def test_custom_review_ok_uses_bound_qwen_identity_and_does_not_reopen_accepted_rows(self):
        self.config["routing"]["reviewers"] = ["native-qwen"]
        source = self.source("review.jsonl", [review_segment("fixture")])
        self.run_pass(source, "review", [("fixture", "success", "OK")], kind="review", tier="strong")
        summary = self.cli("collect-native-review", "--input", source, "--manifest", "review/manifest.json",
                           "--output", "collected", "--tier", "strong")
        self.assertEqual((summary["accepted"], summary["fallback_pending"]), (1, 0))
        accepted = read_translation_results(self.root / "collected/accepted.json")[0]
        self.assertEqual(accepted.reviewer, "native-qwen")
        self.assertEqual(accepted.attempts[-1].provider, "native-qwen")
        self.assertEqual(read_segments(self.root / "collected/fallback-input.jsonl"), [])

    def test_native_route_selection_does_not_skip_earlier_external_judge(self):
        self.config["routing"]["reviewers"] = ["external-judge", "native-qwen"]
        source = self.source("review.jsonl", [review_segment("fixture")])
        with self.assertRaisesRegex(ValueError, "E_NATIVE_ROUTE_INVALID"):
            self.cli("prepare-native-pass", "--input", source, "--output", "tasks",
                     "--pass-kind", "review", "--review-tier", "strong", "--native-provider", "native-qwen")
        self.assertFalse((self.root / "tasks").exists())

    def test_custom_route_requires_its_own_exact_model_mapping(self):
        self.config["routing"]["translators"] = ["native-unbound"]
        source = self.source("source.jsonl", [Segment("fixture", "SOURCE")])
        with self.assertRaisesRegex(ConfigurationError, "E_NATIVE_RUNTIME_INVALID"):
            self.cli("prepare-native-pass", "--input", source, "--output", "tasks")
        self.assertFalse((self.root / "tasks").exists())

    def test_custom_all_native_exhaustion_collects_without_an_external_exchange(self):
        self.config["routing"]["translators"] = ["native-qwen"]
        source = self.source("source.jsonl", [Segment("fixture", "SOURCE")])
        results, _ = self.run_pass(source, "first", [("fixture", "refused", "")])
        state = self.cli("start-native-translation-retries", "--input", source,
                         "--results", results, "--output", "retry")
        self.assertEqual(state["next_action"], "external_fallback")
        prepared = self.cli("prepare-native-followup", "--input", source,
                            "--primary-results", state["pre_fallback_results"], "--output", "followup",
                            "--retry-state", state["state"], "--exchange-prefix", "fixture")
        self.assertEqual(prepared["fallback_bundles"], [])
        self.assertEqual(prepared["fallback_count"], 1)
        joined = self.cli("collect-native-followup", "--input", source, "--followup", "followup",
                          "--review-output", "review.jsonl", "--unresolved-output", "unresolved.jsonl",
                          expected_exit=1)
        self.assertEqual((joined["candidate_count"], joined["unresolved_count"]), (0, 1))
        self.assertFalse(joined["translation_barrier_complete"])
        self.assertEqual([s.id for s in read_segments(self.root / "unresolved.jsonl")], ["fixture"])

    def test_status_recognizes_custom_native_topology_without_reading_source(self):
        import hashlib
        from game_l10n.cli import cmd_status
        from game_l10n.control_plane import establish_control_plane_entry
        from game_l10n.workspace import initialize_workspace, load_config, record_sdk_selection
        from tests.test_workspace import activate_workspace_adapter, write_verified_inventory

        sdk, game, work = (self.root / name for name in ("sdk", "game", "work"))
        sdk.mkdir()
        game.mkdir()
        initialize_workspace(work, sdk, game_roots=[game], git_choice="disabled", provider_preset="custom")
        establish_control_plane_entry(work)
        record_sdk_selection(work, "private-new", target_engine="fixture-engine", target_version="1.0")
        activate_workspace_adapter(work)
        _, inventory = write_verified_inventory(work)
        config = load_config(work)
        config["routing"].update(self.config["routing"])
        config["providers"] = []
        (work / "game-l10n.json").write_text(json.dumps(config), encoding="utf-8")
        body = b"{}\n"
        (work / "context/context-pack.json").write_bytes(body)
        (work / ".game-l10n/context-state.json").write_text(json.dumps({
            "status": "success", "source_inventory_fingerprint": inventory["primary_fingerprint"],
            "output_sha256": hashlib.sha256(body).hexdigest(), "output_size": len(body),
            "execution": {"status": "success", "total_parts": 1, "completed_parts": 1,
                          "parts": [{"status": "success"}]},
        }), encoding="utf-8")
        args = build_parser().parse_args(["status", "--workspace", str(work)])
        output = io.StringIO()
        with patch("game_l10n.cli.read_segments", side_effect=AssertionError("status must use attested metadata")), \
             redirect_stdout(output):
            self.assertEqual(cmd_status(args), 0)
        status = json.loads(output.getvalue())
        self.assertEqual(status["next_actions"], ["run_native_translation_review_pipeline"])
        self.assertEqual(status["execution_mode"], "custom_provider")
        self.assertEqual(status["native_routes"]["translators"][-1],
                         {"provider": "native-qwen", "max_retries": 0})
        self.assertEqual(status["native_routes"]["step_command"], "game-l10n native-pass-step")


    def test_qwen_cannot_launch_on_the_sol_model(self):
        source = self.source("source.jsonl", [Segment("fixture", "SOURCE")])
        self.cli("prepare-native-pass", "--input", source, "--output", "tasks",
                 "--native-provider", "native-qwen")
        manifest = self.root / "tasks/manifest.json"
        results = self.root / "results"
        step = native_control_step(self.root, manifest, results,
                                   require_runtime=True, require_relay_receipt=True)
        relay_path = self.root / step["relay"]["dispatch"]
        relay = json.loads(relay_path.read_text(encoding="utf-8"))
        record_native_launch(self.root, relay_path, event="capability-available")
        record_native_launch(self.root, relay_path, event="worker-started",
                             task=relay["task_bindings"][0]["task"], worker_id="wrong-model",
                             model="openai-codex/gpt-6.1-sol")
        seal_native_wave_relay(self.root, relay_path, relay_ended=True)
        blocked = native_control_step(self.root, manifest, results,
                                      require_runtime=True, require_relay_receipt=True)
        self.assertEqual(blocked["action"], "repair_native_runtime")
        self.assertIn("E_NATIVE_WORKER_MODEL_MISMATCH", blocked["issue_codes"])
        self.assertEqual(blocked["route_binding"]["provider"], "native-qwen")
        self.assertEqual(list(results.glob("*.jsonl")), [])

    def test_locked_preset_does_not_adopt_a_custom_native_route(self):
        self.config["provider_preset"] = "codex-app-deepseek"
        self.config["execution_mode"] = "codex_app_native"
        self.config["routing"]["translators"] = ["native-luna", "native-sol", "deepseek"]
        source = self.source("source.jsonl", [Segment("fixture", "SOURCE")])
        with self.assertRaisesRegex(ConfigurationError, "outside the configured route plan"):
            self.cli("prepare-native-pass", "--input", source, "--output", "tasks",
                     "--native-provider", "native-qwen")
        self.assertFalse((self.root / "tasks").exists())


if __name__ == "__main__":
    unittest.main()
