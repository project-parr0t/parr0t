"""Synthetic host only: these tests never create model/API provider calls."""
from __future__ import annotations

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from game_l10n.control_plane import establish_control_plane_entry
from game_l10n.coverage import save_inventory_attestation
from game_l10n.data_plane_task import prepare_data_plane_task
from game_l10n.models import Segment
from game_l10n.native_broker import _drive, _exclusive, probe_native_host, run_native_broker
from game_l10n.native_host import (
    HOST_PROTOCOL, NativeHostClient, configure_native_host, native_host_profile, verified_host_probe,
)
from game_l10n.native_pass import native_control_step, prepare_native_tasks
from game_l10n.native_pass import seal_native_wave_relay
from game_l10n.native_runtime import (
    bind_data_plane_launch, configure_native_runtime, native_launch_summary, native_runtime,
    record_native_launch,
)
from game_l10n.store import stable_hash


class NativeBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        establish_control_plane_entry(self.root)
        self.calls = []
        self.executions = {}
        self.worker_limit = 2
        self.host_id = "fixture-host"
        self.behavior = "complete"
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, value):
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fixture.calls.append(("GET", self.path))
                if self.path == "/capabilities":
                    self.send({"protocol": HOST_PROTOCOL, "host": fixture.host_id,
                        "worker_runtime": "native_agent_api", "independent_workers": True,
                        "broker_only_delivery": True, "fresh_history": True, "explicit_models": True,
                        "atomic_wave_start": True, "idempotent_requests": True, "shared_workspace": True,
                        "max_workers": fixture.worker_limit, "models": ["fixture/luna", "fixture/sol"]})
                elif self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/must-not-follow")
                    self.end_headers()
                else:
                    key = self.path.rsplit("/", 1)[-1]
                    self.send(fixture.executions.get(key, {"request_id": key, "status": "not_found", "retry_safe": True}))

            def do_POST(self):
                fixture.calls.append(("POST", self.path))
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                key = payload["request_id"]
                if key not in fixture.executions:
                    workers = []
                    for index, task in enumerate(payload["tasks"]):
                        row = {"task_key": task["task_key"], "worker_id": "worker-" + key + f"-{index}",
                               "model": payload["model"], "observed_model": payload["model"], "status": "completed"}
                        if task["kind"] == "probe":
                            row["probe_digest"] = stable_hash(task["canary"])
                        elif fixture.behavior == "wrong-model":
                            row["observed_model"] = "fixture/wrong"
                        elif fixture.behavior == "failed-peer" and index == 0:
                            row["status"] = "failed"
                        elif task.get("manifest"):
                            source = fixture.root / task["task"]
                            result = fixture.root / task["result"]
                            result.parent.mkdir(parents=True, exist_ok=True)
                            result.write_text("".join(json.dumps({"target_id": json.loads(line)["target_id"],
                                "status": "success", "text": "TARGET", "provider": "native-luna", "issues": []}) + "\n"
                                for line in source.read_text(encoding="utf-8").splitlines()), encoding="utf-8")
                        workers.append(row)
                    fixture.executions[key] = {"request_id": key, "status": "completed", "workers": workers,
                        "usage": {"input_tokens": 20, "provider_cost_usd": 0.001},
                        "raw_response": "PRIVATE HOST PROSE MUST NOT ESCAPE"}
                self.send(fixture.executions[key])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        endpoint = f"http://127.0.0.1:{self.server.server_port}"
        configure_native_host(self.root, "fixture-host", endpoint)
        configure_native_runtime(self.root, "fixture-host", {"native-luna": "fixture/luna", "native-sol": "fixture/sol"},
                                 transport="native-host-api")

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def probe(self, route="native-luna"):
        return probe_native_host(self.root, route, live=True, poll_seconds=0)

    def prepare(self, **kwargs):
        self.probe()
        segments = [Segment("a", "SOURCE A"), Segment("b", "SOURCE B")]
        source = self.root / "source.jsonl"
        source.write_text("".join(json.dumps(asdict(row)) + "\n" for row in segments), encoding="utf-8")
        prepare_native_tasks(self.root, source, segments, self.root / "tasks", max_records=1, max_workers=8,
                             route_binding={"provider": "native-luna", "attempt": 1}, **kwargs)
        return self.step()

    def step(self):
        return native_control_step(self.root, self.root / "tasks/manifest.json", self.root / "results",
                                   require_relay_receipt=True, require_runtime=True)

    def posts(self):
        return sum(method == "POST" for method, _ in self.calls)

    def test_nonlive_probe_does_not_start_workers_and_route_requires_canary(self):
        result = probe_native_host(self.root, "native-luna")
        self.assertFalse(result["ready"])
        self.assertEqual(self.posts(), 0)
        with self.assertRaisesRegex(ValueError, "E_NATIVE_HOST_PROBE_REQUIRED"):
            verified_host_probe(self.root, native_runtime(self.root), "native-luna")
        self.assertTrue(self.probe()["execution_verified"])
        count = self.posts()
        self.probe()
        self.assertEqual(self.posts(), count)
        with self.assertRaisesRegex(ValueError, "E_NATIVE_HOST_PROBE_REQUIRED"):
            verified_host_probe(self.root, native_runtime(self.root), "native-sol")

    def test_bound_wave_executes_once_seals_and_records_usage(self):
        step = self.prepare()
        self.assertEqual(step["relay"]["executor"], "native-host-api")
        manifest = json.loads((self.root / "tasks/manifest.json").read_text())
        self.assertEqual(manifest["max_workers"], 2)
        relay = self.root / step["relay"]["dispatch"]
        result = run_native_broker(self.root, relay, poll_seconds=0)
        self.assertFalse(result["issue_codes"])
        self.assertEqual(native_launch_summary(self.root, relay)["observed_model_workers"], 2)
        count = self.posts()
        again = run_native_broker(self.root, relay, poll_seconds=0)
        self.assertEqual(self.posts(), count)
        self.assertEqual(result, again)
        complete = self.step()
        self.assertEqual(complete["action"], "aggregate_completed_pass")
        self.assertEqual(complete["usage"]["input_tokens"], 20)
        self.assertEqual(complete["usage"]["events"], 1)
        metadata = "".join(path.read_text() for path in (self.root / ".game-l10n/native/broker").rglob("*.json"))
        self.assertNotIn("PRIVATE HOST PROSE", metadata + json.dumps(result))

    def test_lost_response_reconciles_without_duplicate_start(self):
        step = self.prepare()
        relay = self.root / step["relay"]["dispatch"]
        original = NativeHostClient.request

        def lose_once(client, method, path, payload=None):
            value = original(client, method, path, payload)
            if method == "POST":
                raise ValueError("E_NATIVE_HOST_TRANSPORT_ERROR")
            return value

        with patch.object(NativeHostClient, "request", lose_once):
            with self.assertRaisesRegex(ValueError, "E_NATIVE_HOST_TRANSPORT_ERROR"):
                run_native_broker(self.root, relay, poll_seconds=0)
        count = self.posts()
        run_native_broker(self.root, relay, poll_seconds=0)
        self.assertEqual(self.posts(), count)

    def test_failed_worker_does_not_discard_peer_success(self):
        step = self.prepare()
        self.behavior = "failed-peer"
        run_native_broker(self.root, self.root / step["relay"]["dispatch"], poll_seconds=0)
        failed = json.loads((self.root / "results/task-0001.jsonl").read_text())
        peer = json.loads((self.root / "results/task-0002.jsonl").read_text())
        self.assertEqual(failed["status"], "error")
        self.assertEqual(peer["status"], "success")

    def test_wrong_model_never_seals_success(self):
        step = self.prepare()
        self.behavior = "wrong-model"
        with self.assertRaisesRegex(ValueError, "E_NATIVE_HOST_MODEL_OR_REPLY_INVALID"):
            run_native_broker(self.root, self.root / step["relay"]["dispatch"], poll_seconds=0)
        self.assertEqual(list((self.root / "results").glob("*.jsonl")), [])

    def test_probe_source_task_uses_same_broker_and_seals_missing_output_as_error(self):
        self.probe("native-sol")
        game = self.root / "game"
        game.mkdir()
        config = {"game_roots": [str(game)], "sdk_root": str(self.root / "sdk"),
                  "sdk_selection": {"inventory_fingerprint": "fixture-inventory"}}
        prepared = prepare_data_plane_task(self.root, self.root / ".game-l10n/data-plane/source_probe",
                                          worker_role="source_probe", config=config)
        relay = self.root / prepared["relay_path"]
        bind_data_plane_launch(self.root, relay)
        result = run_native_broker(self.root, relay, poll_seconds=0)
        self.assertEqual(result["status"], "error")
        self.assertEqual(native_launch_summary(self.root, relay)["started_workers"], 1)

    def test_explicit_transport_and_exact_models_are_required(self):
        with self.assertRaisesRegex(ValueError, "E_NATIVE_RUNTIME_INVALID"):
            configure_native_runtime(self.root, "unknown-host", {}, transport="native-host-api")
        configure_native_runtime(self.root, "codex", {})
        with self.assertRaisesRegex(ValueError, "E_NATIVE_BROKER_NOT_SELECTED"):
            run_native_broker(self.root, self.root / "missing.json")
        self.assertEqual(self.posts(), 0)

    def test_credential_is_environment_only_and_redirects_are_not_followed(self):
        profile = native_host_profile(self.root, "fixture-host")
        configure_native_host(self.root, "credential-host", profile["endpoint"], "FIXTURE_HOST_TOKEN")
        credential = native_host_profile(self.root, "credential-host")
        with patch.dict("os.environ", {"FIXTURE_HOST_TOKEN": "SECRET-MARKER"}):
            with self.assertRaisesRegex(ValueError, "E_NATIVE_HOST_TRANSPORT_ERROR"):
                NativeHostClient(credential).request("GET", "/redirect")
        self.assertNotIn(("GET", "/must-not-follow"), self.calls)
        self.assertNotIn("SECRET-MARKER", json.dumps(credential))
        with self.assertRaisesRegex(ValueError, "E_NATIVE_HOST_CREDENTIAL_TRANSPORT_INVALID"):
            configure_native_host(self.root, "bad-host", "http://example.invalid", "FIXTURE_HOST_TOKEN")

    def test_profile_is_immutable_and_tampered_route_probe_cannot_be_reused(self):
        profile = native_host_profile(self.root, "fixture-host")
        with self.assertRaisesRegex(ValueError, "IMMUTABLE"):
            configure_native_host(self.root, "fixture-host", profile["endpoint"] + "/changed")
        self.probe()
        configure_native_runtime(self.root, "fixture-host", {"native-luna": "fixture/changed"})
        with self.assertRaisesRegex(ValueError, "E_NATIVE_HOST_PROBE_REQUIRED"):
            verified_host_probe(self.root, native_runtime(self.root), "native-luna")

    def test_wave_rejects_path_escape_before_any_start(self):
        step = self.prepare()
        relay = self.root / step["relay"]["dispatch"]
        value = json.loads(relay.read_text())
        value["task_bindings"][0]["result"] = "../escaped.jsonl"
        unsigned = {key: item for key, item in value.items() if key != "dispatch_fingerprint"}
        value["dispatch_fingerprint"] = stable_hash(unsigned)
        relay.write_text(json.dumps(value))
        before = self.posts()
        with self.assertRaisesRegex(ValueError, "E_NATIVE_BROKER_BINDING_CHANGED"):
            run_native_broker(self.root, relay, poll_seconds=0)
        self.assertEqual(self.posts(), before)

    def test_running_poll_can_enrich_model_evidence_without_restarting_worker(self):
        step = self.prepare()
        relay = self.root / step["relay"]["dispatch"]
        original = NativeHostClient.wave

        def running(client, request_id, payload, *, reconcile):
            complete = original(client, request_id, payload, reconcile=reconcile)
            value = json.loads(json.dumps(complete))
            value["status"] = "running"
            for row in value["workers"]:
                row.update(status="running", observed_model=None)
            return value

        with patch.object(NativeHostClient, "wave", running):
            run_native_broker(self.root, relay, poll_seconds=0)
        self.assertEqual(native_launch_summary(self.root, relay)["observed_model_workers"], 2)

    def test_concurrent_broker_is_rejected_without_stale_lock_files(self):
        lock = self.root / "fixture.lock"
        with _exclusive(lock):
            with self.assertRaisesRegex(ValueError, "E_NATIVE_BROKER_ALREADY_RUNNING"):
                with _exclusive(lock):
                    self.fail("concurrent broker entered")
        with _exclusive(lock):
            pass

    def test_missing_request_requires_explicit_retry_safe(self):
        client = NativeHostClient(native_host_profile(self.root, "fixture-host"))
        with patch.object(client, "request", return_value={"request_id": "request-1", "status": "not_found"}) as call:
            with self.assertRaisesRegex(ValueError, "E_NATIVE_HOST_LAUNCH_UNCERTAIN"):
                client.wave("request-1", {}, reconcile=True)
        self.assertEqual(call.call_count, 1)

    def test_context_slots_end_before_finalizer_and_resume_does_not_relaunch(self):
        self.probe("native-sol")
        game = self.root / "game"
        game.mkdir()
        inventory = {"schema": "parr0t.source-inventory.v1", "method": "fixture",
            "implementation_fingerprint": "fixture-implementation",
            "coverage": {"declared_complete": True, "exclusions": []},
            "segments": [{"id": f"fixture-{i}", "source": "SOURCE " * 2000} for i in range(3)]}
        primary, independent = self.root / "primary.json", self.root / "independent.json"
        primary.write_text(json.dumps(inventory))
        independent.write_text(json.dumps({**inventory, "method": "fixture-independent",
                                          "implementation_fingerprint": "fixture-independent"}))
        save_inventory_attestation(self.root, primary, independent)
        config = {"game_roots": [str(game)], "sdk_root": str(self.root / "sdk"),
            "sdk_selection": {"inventory_fingerprint": "fixture-sdk"},
            "files": {"context_pack": "context/context-pack.json"}, "project": {"context_mode": "auto"},
            "routing": {"context_analyzers": ["native-sol"], "max_workers": 8}}
        prepared = prepare_data_plane_task(self.root, self.root / ".game-l10n/data-plane/context_analyzer",
            worker_role="context_analyzer", config=config, sdk_activation={"status": "active"})
        relay = self.root / prepared["relay_path"]
        bind_data_plane_launch(self.root, relay)
        payload = json.loads(relay.read_text())
        self.assertEqual(payload["max_workers"], 2)
        self.assertEqual(payload["execution_plan"]["required_worker_slots"], [1, 2])
        from game_l10n.data_plane_task import record_data_plane_relay_event
        events = []

        def record(*args, **kwargs):
            events.append((kwargs["event"], kwargs.get("slot")))
            return record_data_plane_relay_event(*args, **kwargs)

        with patch("game_l10n.data_plane_task.record_data_plane_relay_event", record):
            result = run_native_broker(self.root, relay, poll_seconds=0)
        self.assertEqual(events, [("worker-started", 1), ("worker-started", 2),
            ("worker-ended", 1), ("worker-ended", 2), ("finalizer-started", None), ("finalizer-ended", None)])
        self.assertEqual(result["status"], "error")  # Synthetic workers intentionally produce no semantic context.
        count = self.posts()
        run_native_broker(self.root, relay, poll_seconds=0)
        self.assertEqual(self.posts(), count)

    def test_invalid_terminal_snapshot_is_not_cached_as_complete(self):
        client = NativeHostClient(native_host_profile(self.root, "fixture-host"))
        identity = {"model": "fixture/luna", "test": "incomplete-terminal"}
        reply = {"request_id": "wave-" + stable_hash(identity), "status": "completed", "workers": [
            {"task_key": "probe", "worker_id": "worker-1", "model": "fixture/luna", "status": "running"}]}
        path = self.root / "execution.json"
        with patch.object(client, "wave", return_value=reply):
            with self.assertRaisesRegex(ValueError, "E_NATIVE_HOST_REPLY_INVALID"):
                _drive(client, path, identity, [{"task_key": "probe"}], poll_seconds=0)
        self.assertEqual(json.loads(path.read_text())["status"], "requested")

    def test_ended_unstarted_wave_can_change_transport_on_same_host_and_model(self):
        profile = native_host_profile(self.root, "fixture-host")
        self.host_id = "codex"
        configure_native_host(self.root, "codex", profile["endpoint"])
        models = {"native-luna": "fixture/luna", "native-sol": "fixture/sol"}
        configure_native_runtime(self.root, "codex", models)
        segments = [Segment("a", "SOURCE A")]
        source = self.root / "source.jsonl"
        source.write_text(json.dumps(asdict(segments[0])) + "\n")
        prepare_native_tasks(self.root, source, segments, self.root / "tasks", max_records=1,
                             route_binding={"provider": "native-luna", "attempt": 1})
        initial = self.step()
        relay = self.root / initial["relay"]["dispatch"]
        task_bytes = (self.root / "tasks/task-0001.jsonl").read_bytes()
        record_native_launch(self.root, relay, event="capability-unavailable")
        seal_native_wave_relay(self.root, relay, relay_ended=True)
        configure_native_runtime(self.root, "codex", {}, transport="native-host-api")
        self.probe()
        resumed = native_control_step(self.root, self.root / "tasks/manifest.json", self.root / "results",
            require_relay_receipt=True, require_runtime=True, resume_relay=True)
        self.assertEqual(resumed["relay"]["executor"], "native-host-api")
        self.assertEqual(resumed["route_binding"]["attempt"], 1)
        self.assertEqual((self.root / "tasks/task-0001.jsonl").read_bytes(), task_bytes)
        self.assertFalse(run_native_broker(self.root, relay, poll_seconds=0)["issue_codes"])

    def test_http_parser_errors_do_not_echo_host_prose(self):
        import http.client
        client = NativeHostClient(native_host_profile(self.root, "fixture-host"))
        with patch("urllib.request.OpenerDirector.open", side_effect=http.client.BadStatusLine("PRIVATE HOST PROSE")):
            with self.assertRaisesRegex(ValueError, "^E_NATIVE_HOST_TRANSPORT_ERROR$"):
                client.request("GET", "/capabilities")
