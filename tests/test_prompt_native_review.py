from __future__ import annotations

import io
import json
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from game_l10n.cli import build_parser
from game_l10n.errors import ConfigurationError
from game_l10n.formats import read_segments, read_translation_results
from game_l10n.models import AttemptRecord, ContextPack
from game_l10n.native_pass import native_control_step, seal_native_wave_relay
from game_l10n.native_review import _bound_result, _pending_segment, process_native_review_followup
from game_l10n.native_runtime import record_native_launch
from game_l10n.orchestrator import OrchestratorConfig
from game_l10n.review_policy import judge_calls
from game_l10n.workspace import initialize_workspace, load_config
from tests.omp_isolation_fixture import bind_omp_isolation
from tests.test_native_pass import review_segment
from tests import write_semantic_result_rows


class PromptNativeReviewTests(unittest.TestCase):
    fallback_model = "fixture/exact-native-judge:high"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        sdk = root / "sdk"
        sdk.mkdir()
        self.root = root / "workspace"
        with patch("game_l10n.workspace.inventory_sdk_branches", return_value={
            "source": "fixture", "fingerprint": "fixture", "candidates": [],
        }):
            initialize_workspace(self.root, sdk, provider_preset="codex-app-deepseek",
                                 git_choice="disabled")
        bind_omp_isolation(self, self.root)
        self.before = load_config(self.root)

    def cli(self, command, *arguments, expected_exit=0):
        args = build_parser().parse_args([command, "--workspace", str(self.root), *arguments])
        output = io.StringIO()
        with patch("game_l10n.cli.require_sdk_activation"), redirect_stdout(output):
            self.assertEqual(args.func(args), expected_exit)
        return json.loads(output.getvalue())

    def bind(self):
        self.cli("native-runtime", "--host", "omp",
                 "--model", "native-luna=fixture/exact-triager",
                 "--model", "native-sol=fixture/exact-primary-judge")

    def request_fallback(self):
        return self.cli("review-policy", "--fallback-model", self.fallback_model,
                        "--max-calls", "4", "--user-requested")

    def source(self, name, segments):
        path = self.root / name
        path.write_text("".join(json.dumps(asdict(segment)) + "\n" for segment in segments),
                        encoding="utf-8")
        return name

    def run_review(self, source, directory, tier, response):
        prepared = self.cli("prepare-native-pass", "--input", source, "--output", directory,
                            "--pass-kind", "review", "--review-tier", tier,
                            "--max-records", "16", "--min-records", "1", "--max-workers", "1")
        manifest_path = self.root / directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        provider = manifest["route_binding"]["provider"]
        calls = Counter()
        launches = []
        for _ in range(8):
            step = native_control_step(self.root, manifest_path, self.root / (directory + "-results"),
                                       require_runtime=True, require_relay_receipt=True)
            if step["action"] == "aggregate_completed_pass":
                return manifest, calls, launches, step, prepared
            self.assertEqual(step["action"], "dispatch_isolated_wave_relay")
            relay_path = self.root / step["relay"]["dispatch"]
            relay = json.loads(relay_path.read_text(encoding="utf-8"))
            contract = relay["launch_contract"]
            self.assertEqual(contract["route"], provider)
            self.assertEqual(contract["spawn_model_parameter"], "tasks[].model")
            record_native_launch(self.root, relay_path, event="capability-available")
            for binding in relay["task_bindings"]:
                task = self.root / binding["task"]
                ids = [json.loads(line)["target_id"] for line in task.read_text(encoding="utf-8").splitlines()]
                record_native_launch(self.root, relay_path, event="worker-started", task=binding["task"],
                                     worker_id="fixture-worker", model=contract["model"],
                                     observed_model=contract["model"])
                rows = []
                for target in ids:
                    calls[target] += 1
                    status, text = response(target, calls[target])
                    rows.append({"target_id": target, "status": status, "text": text,
                                 "provider": provider, "issues": []})
                destination = self.root / binding["result"]
                write_semantic_result_rows(
                    destination, rows, manifest=manifest
                )
                launches.append((contract["route"], contract["model"], ids))
            seal_native_wave_relay(self.root, relay_path, relay_ended=True)
        self.fail("native review did not reach its existing bounded aggregation barrier")

    def collect(self, source, directory, output, tier):
        return self.cli("collect-native-review", "--input", source,
                        "--manifest", directory + "/manifest.json", "--output", output, "--tier", tier)

    def test_initial_exact_fallback_uses_native_manifest_and_locks_successes(self):
        self.request_fallback()
        self.bind()
        policy = self.cli("review-policy")
        self.assertEqual(policy["status"], "configured")
        config = load_config(self.root)
        self.assertEqual(config["provider_preset"], "codex-app-deepseek")
        self.assertEqual(config["execution_mode"], self.before["execution_mode"])
        self.assertEqual(config["providers"], self.before["providers"])
        self.assertEqual(config["native_workflow"], self.before["native_workflow"])
        self.assertEqual(config["routing"]["reviewers"], ["native-sol", "native-review-fallback"])
        self.assertEqual(config["routing"]["review_attempt_limits"]["native-review-fallback"], 4)
        self.assertEqual(config["model_policy"]["native_roles"]["review-fallback"], self.fallback_model)
        for role in ("translators", "context_analyzers", "review_triagers", "rewriters"):
            self.assertEqual(config["routing"][role], self.before["routing"][role])

        ids = ["accepted"] + [f"pending-{index}" for index in range(8)]
        source = self.source("candidates.jsonl", [
            review_segment(target, candidate="\ub300\uc0c1") for target in ids
        ])
        triage, calls, _, _, _ = self.run_review(
            source, "triage", "triage", lambda *_: ("success", "REWRITE fixture"))
        self.assertEqual(triage["route_binding"]["provider"], "native-luna")
        self.assertEqual(calls, Counter({target: 1 for target in ids}))
        summary = self.collect(source, "triage", "triage-joined", "triage")
        self.assertEqual((summary["accepted"], summary["strong_pending"]), (0, 9))
        strong_input = "triage-joined/strong-input.jsonl"
        with self.assertRaisesRegex(ConfigurationError, "configured order"):
            self.cli("prepare-native-pass", "--input", strong_input, "--output", "skip-primary",
                     "--pass-kind", "review", "--review-tier", "strong",
                     "--native-provider", "native-review-fallback")
        self.assertFalse((self.root / "skip-primary").exists())

        primary, calls, _, done, _ = self.run_review(
            strong_input, "primary", "strong",
            lambda target, _: ("success", "OK") if target == "accepted" else ("refused", ""))
        self.assertEqual(primary["route_binding"]["provider"], "native-sol")
        self.assertEqual(calls["accepted"], 1)
        self.assertEqual([calls[target] for target in ids[1:]], [2] * 8)
        self.assertEqual(done["review_split"]["terminal_reason"], "review_call_limit")
        summary = self.collect(strong_input, "primary", "primary-joined", "strong")
        self.assertEqual((summary["accepted"], summary["fallback_pending"]), (1, 8))
        accepted_path = self.root / "primary-joined/accepted.json"
        primary_accepted = accepted_path.read_bytes()
        fallback_input = "primary-joined/fallback-input.jsonl"
        pending = read_segments(self.root / fallback_input)
        self.assertEqual([segment.id for segment in pending], ids[1:])
        self.assertEqual([judge_calls(_bound_result(segment).attempts, "native-sol")
                          for segment in pending], [2] * 8)

        with patch("game_l10n.cli.read_segments", side_effect=AssertionError("semantic input opened")), \
             patch("game_l10n.cli._require_provider_credential", side_effect=AssertionError("credential requested")), \
             patch("game_l10n.cli._codex_app_fallback_orchestrator", side_effect=AssertionError("external factory constructed")):
            redirect = self.cli("process-native-review-followup", "--operation", "judge",
                                "--input", fallback_input, "--output", "fallback", expected_exit=1)
        self.assertEqual(redirect["action"], "prepare_native_pass")
        self.assertEqual(redirect["provider"], "native-review-fallback")
        self.assertIn("--review-tier", redirect["command_argv"])
        self.assertIn("native-review-fallback", redirect["command_argv"])

        def fallback_response(target, count):
            index = int(target.rsplit("-", 1)[1])
            required = 1 if index < 4 else 2 if index < 6 else 4
            return ("success", "OK") if count >= required else ("refused", "")

        fallback, calls, launches, _, _ = self.run_review(
            fallback_input, "fallback", "strong", fallback_response)
        self.assertEqual(fallback["route_binding"]["provider"], "native-review-fallback")
        self.assertTrue(launches)
        self.assertTrue(all(route == "native-review-fallback" and model == self.fallback_model
                            for route, model, _ in launches))
        self.assertNotIn("accepted", calls)
        self.assertEqual([calls[target] for target in ids[1:]], [1, 1, 1, 1, 2, 2, 4, 4])
        summary = self.collect(fallback_input, "fallback", "fallback-joined", "strong")
        self.assertEqual((summary["accepted"], summary["fallback_pending"]), (8, 0))
        self.assertEqual(accepted_path.read_bytes(), primary_accepted)
        fallback_accepted = read_translation_results(self.root / "fallback-joined/accepted.json")
        self.assertEqual([row.segment_id for row in fallback_accepted], ids[1:])
        for result in fallback_accepted:
            self.assertEqual(result.reviewer, "native-review-fallback")
            self.assertEqual(judge_calls(result.attempts, "native-sol"), 2)
            self.assertEqual(judge_calls(result.attempts, "native-review-fallback"), calls[result.segment_id])
            self.assertEqual(result.attempts[-1].model, self.fallback_model)
            self.assertNotIn("deepseek", [attempt.provider for attempt in result.attempts])
        self.assertEqual(read_segments(self.root / "fallback-joined/fallback-input.jsonl"), [])

    def test_pending_initial_fallback_repairs_host_without_opening_input(self):
        self.request_fallback()
        with patch("game_l10n.cli.read_segments", side_effect=AssertionError("semantic input opened")), \
             patch("game_l10n.cli._require_provider_credential", side_effect=AssertionError("credential requested")), \
             patch("game_l10n.cli._codex_app_fallback_orchestrator", side_effect=AssertionError("external factory constructed")):
            result = self.cli("process-native-review-followup", "--operation", "judge",
                              "--input", "unopened.jsonl", "--output", "pending", expected_exit=1)
        self.assertEqual(result["status"], "native_binding_required")
        self.assertEqual(result["action"], "configure_native_runtime")
        self.assertIn("native-review-fallback", result["native_binding_pending"])
        config = load_config(self.root)
        self.assertEqual(config["model_policy"]["native_roles"]["review-fallback"], self.fallback_model)
        self.assertEqual(config["routing"]["review_attempt_limits"]["native-review-fallback"], 4)
        self.assertEqual(config["providers"], self.before["providers"])

    def test_runtime_role_arguments_require_host_and_preserve_other_slots(self):
        for arguments in (("--role-model", "strong-review=fixture/exact-strong"),
                          ("--user-requested-models",)):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, "require --host"):
                self.cli("native-runtime", *arguments)
        self.cli("native-runtime", "--host", "omp", "--role-model", "strong-review=fixture/exact-strong",
                 "--user-requested-models")
        config = load_config(self.root)
        self.assertEqual(config["routing"]["reviewers"], ["native-strong-review", "deepseek"])
        for role in ("translators", "context_analyzers", "review_triagers", "rewriters"):
            self.assertEqual(config["routing"][role], self.before["routing"][role])
        self.assertEqual(config["providers"], self.before["providers"])
        self.assertEqual(config["model_policy"]["native_roles"]["strong-review"], "fixture/exact-strong")
        source = self.source("overridden.jsonl", [review_segment("fixture", candidate="\ub300\uc0c1")])
        manifest, calls, launches, _, _ = self.run_review(
            source, "overridden", "strong", lambda *_: ("success", "OK"))
        self.assertEqual(manifest["route_binding"]["provider"], "native-strong-review")
        self.assertEqual(calls, Counter({"fixture": 1}))
        self.assertEqual(launches[0][:2], ("native-strong-review", "fixture/exact-strong"))
        self.collect(source, "overridden", "overridden-joined", "strong")
        accepted = read_translation_results(self.root / "overridden-joined/accepted.json")
        self.assertEqual(accepted[0].reviewer, "native-strong-review")

    def test_external_followup_requires_current_overridden_primary_failure(self):
        base = review_segment("fixture", candidate="\ub300\uc0c1")
        result = _bound_result(base)
        result.attempts.append(AttemptRecord("review", "native-strong-review", "error",
                                             candidate_generation=1))
        source = self.source("primary-failed.jsonl", [_pending_segment(base, result)])
        orchestrator = SimpleNamespace(
            registry=SimpleNamespace(get=lambda name: SimpleNamespace(
                name=name, fingerprint=lambda: {"name": name})),
            context_pack=ContextPack(),
            config=OrchestratorConfig(["deepseek"], ["deepseek"], require_review=False,
                                      review_attempt_limits={"deepseek": 1}),
        )
        summary = process_native_review_followup(
            self.root, self.root / source, self.root / "followup", orchestrator,
            operation="judge", deadline_epoch=0,
            primary_review_provider="native-strong-review", primary_review_limit=None,
        )
        self.assertEqual((summary["executed"], summary["unresolved"]), (0, 1))
        with self.assertRaisesRegex(ValueError, "PRIMARY_BARRIER_REQUIRED"):
            process_native_review_followup(
                self.root, self.root / source, self.root / "wrong-primary", orchestrator,
                operation="judge", deadline_epoch=0,
            )
        result.attempts[-1].status = "rewrite"
        source = self.source("not-failed.jsonl", [_pending_segment(base, result)])
        with self.assertRaisesRegex(ValueError, "FALLBACK_NOT_FAILED"):
            process_native_review_followup(
                self.root, self.root / source, self.root / "not-failed", orchestrator,
                operation="judge", deadline_epoch=0,
                primary_review_provider="native-strong-review", primary_review_limit=None,
            )
        result.attempts[-1].status = "error"
        result.attempts[-1].candidate_generation = 0
        source = self.source("stale-primary.jsonl", [_pending_segment(base, result)])
        with self.assertRaisesRegex(ValueError, "PRIMARY_BARRIER_REQUIRED"):
            process_native_review_followup(
                self.root, self.root / source, self.root / "stale", orchestrator,
                operation="judge", deadline_epoch=0,
                primary_review_provider="native-strong-review", primary_review_limit=None,
            )

    def test_library_judge_cannot_use_stale_external_limit_for_native_fallback(self):
        for chain in ([], ["native-sol", "native-review-fallback"]):
            with self.subTest(chain=chain):
                orchestrator = SimpleNamespace(config=OrchestratorConfig(
                    ["deepseek"], chain, require_review=False,
                    review_attempt_limits={"deepseek": 4, "native-review-fallback": 4},
                ))
                with patch("game_l10n.native_review.read_segments", side_effect=AssertionError("semantic input opened")):
                    with self.assertRaisesRegex(ValueError, "NATIVE_FALLBACK_REQUIRED"):
                        process_native_review_followup(
                            self.root, self.root / "unopened.jsonl", self.root / "uncreated",
                            orchestrator, operation="judge",
                        )
                self.assertFalse((self.root / "uncreated").exists())

    def test_runtime_rejects_duplicate_or_malformed_role_arguments(self):
        for arguments in (("--role-model", "strong-review"),
                          ("--role-model", "strong-review=fixture/one", "--role-model", "strong-review=fixture/two")):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, "once per role"):
                self.cli("native-runtime", "--host", "omp", *arguments, "--user-requested-models")


if __name__ == "__main__":
    unittest.main()
