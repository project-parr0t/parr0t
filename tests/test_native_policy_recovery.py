from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from game_l10n.control_plane import establish_control_plane_entry
from game_l10n.models import Segment
from game_l10n.native_pass import native_control_step, prepare_native_tasks, seal_native_wave_relay
from game_l10n.native_runtime import configure_native_runtime, record_native_launch
from game_l10n.omp_host_policy import CONFIG_ENV
from game_l10n.store import stable_hash
from tests.omp_isolation_fixture import bind_omp_isolation


class NativePolicyRecoveryTests(unittest.TestCase):
    route = "native-luna"
    model = "fixture/exact-model:high"
    original_policy = "1" * 64
    repaired_policy = "2" * 64

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, {CONFIG_ENV: ""}))
        bind_omp_isolation(self, self.root)
        establish_control_plane_entry(self.root)
        self.configure(self.original_policy)
        segments = [Segment("a", "SOURCE A"), Segment("b", "SOURCE B")]
        source = self.root / "source.jsonl"
        source.write_text("".join(json.dumps(asdict(row)) + "\n" for row in segments), encoding="utf-8")
        tasks = self.root / "tasks"
        self.manifest = tasks / "manifest.json"
        self.results = self.root / "results"
        prepare_native_tasks(
            self.root, source, segments, tasks, max_records=1, max_workers=2,
            route_binding={"provider": self.route, "attempt": 1},
        )
        initial = self.step()
        self.assertEqual(initial["action"], "dispatch_isolated_wave_relay")
        self.relay = self.root / initial["relay"]["dispatch"]
        self.payload = json.loads(self.relay.read_bytes())
        self.ledger = self.relay.with_name(self.relay.stem + "-launch.json")

    def configure(self, policy: str) -> dict:
        return configure_native_runtime(
            self.root, "omp", {self.route: self.model}, host_policy_fingerprint=policy,
        )

    def step(self, *, resume_relay: bool = False) -> dict:
        return native_control_step(
            self.root, self.manifest, self.results, require_runtime=True,
            require_relay_receipt=True, resume_relay=resume_relay,
        )

    def seal_denial(self, *, capability: bool = False) -> Path:
        if capability:
            record_native_launch(self.root, self.relay, event="capability-unavailable")
        else:
            record_native_launch(self.root, self.relay, event="model-unavailable", failure_code="host_dispatch_denied")
        sealed = seal_native_wave_relay(self.root, self.relay, relay_ended=True)
        return self.root / sealed["receipt"]

    def assert_archived(self, path: Path, content: bytes) -> None:
        archive = path.with_name(f"{path.name}.legacy-{hashlib.sha256(content).hexdigest()}")
        self.assertEqual(archive.read_bytes(), content)

    def test_sealed_policy_denial_requires_changed_binding(self) -> None:
        receipt = self.seal_denial()
        paths = (self.relay, self.ledger, receipt)
        originals = [path.read_bytes() for path in paths]
        unchanged = self.step(resume_relay=True)
        self.assertEqual(unchanged["action"], "repair_native_runtime")
        self.assertEqual(unchanged["issue_codes"], ["E_NATIVE_WORKER_MODEL_UNAVAILABLE"])
        self.assertEqual([path.read_bytes() for path in paths], originals)

        runtime = self.configure(self.repaired_policy)
        resumed = self.step(resume_relay=True)
        self.assertEqual(resumed["action"], "dispatch_isolated_wave_relay")
        self.assertEqual(resumed["route_binding"], self.payload["route_binding"])
        replacement = json.loads(self.relay.read_bytes())["launch_contract"]
        self.assertEqual(replacement["model"], self.model)
        self.assertEqual(replacement["route"], self.route)
        self.assertEqual(replacement["host_policy_fingerprint"], runtime["host_policy_fingerprint"])
        self.assertFalse(receipt.exists())
        self.assertFalse(self.ledger.exists())
        for path, original in zip(paths, originals):
            self.assert_archived(path, original)
        launch = record_native_launch(self.root, self.relay, event="capability-available")
        self.assertEqual(launch["unstarted_tasks"], self.payload["launch_contract"]["tasks"])

    def test_changed_policy_does_not_replay_started_workers(self) -> None:
        record_native_launch(self.root, self.relay, event="capability-available")
        record_native_launch(
            self.root, self.relay, event="worker-started", model=self.model,
            task=self.payload["launch_contract"]["tasks"][0], worker_id="fixture-worker-1",
        )
        receipt = self.seal_denial()
        paths = (self.relay, self.ledger, receipt)
        originals = [path.read_bytes() for path in paths]
        self.configure(self.repaired_policy)
        with self.assertRaisesRegex(ValueError, "cannot rebind a started worker"):
            self.step(resume_relay=True)
        self.assertEqual([path.read_bytes() for path in paths], originals)
        self.assertFalse(list(self.relay.parent.glob("*.legacy-*")))

    def test_changed_policy_does_not_replay_unrecorded_host_admission(self) -> None:
        digest = hashlib.sha256(self.relay.read_bytes()).hexdigest()
        task = self.payload["launch_contract"]["tasks"][0]
        key = hashlib.sha256(f"{digest}\0{task}".encode()).hexdigest()
        admission = self.root / ".game-l10n/native/omp-launch-admissions" / f"{key}.json"
        admission.parent.mkdir(parents=True, exist_ok=True)
        admission.write_text(json.dumps({
            "schema": "parr0t.omp-native-admission.v1",
            "relay_sha256": digest,
            "task": task,
            "state": "ended",
            "worker_id": "fixture-worker-1",
        }), encoding="utf-8")
        receipt = self.seal_denial()
        paths = (self.relay, self.ledger, receipt, admission)
        originals = [path.read_bytes() for path in paths]
        self.configure(self.repaired_policy)
        with self.assertRaisesRegex(ValueError, "cannot rebind a started worker"):
            self.step(resume_relay=True)
        self.assertEqual([path.read_bytes() for path in paths], originals)
        self.assertFalse(list(self.relay.parent.glob("*.legacy-*")))

    def test_changed_policy_validates_receipt_before_mutating_artifacts(self) -> None:
        receipt = self.seal_denial()
        modified = json.loads(receipt.read_bytes())
        modified.pop("receipt_fingerprint")
        modified["wave"] += 1
        receipt.write_text(json.dumps({**modified, "receipt_fingerprint": stable_hash(modified)}), encoding="utf-8")
        paths = (self.relay, self.ledger, receipt)
        originals = [path.read_bytes() for path in paths]
        self.configure(self.repaired_policy)
        with self.assertRaisesRegex(ValueError, "E_NATIVE_RELAY_RECEIPT_INVALID"):
            self.step(resume_relay=True)
        self.assertEqual([path.read_bytes() for path in paths], originals)
        self.assertFalse(list(self.relay.parent.glob("*.legacy-*")))

    def test_explicit_resume_reissues_sealed_nested_dispatch_failure(self) -> None:
        receipt = self.seal_denial(capability=True)
        original = receipt.read_bytes()
        blocked = self.step()
        self.assertEqual(blocked["issue_codes"], ["E_NATIVE_DISPATCH_UNAVAILABLE"])
        resumed = self.step(resume_relay=True)
        self.assertEqual(resumed["action"], "dispatch_isolated_wave_relay")
        self.assertEqual(resumed["route_binding"], self.payload["route_binding"])
        self.assert_archived(receipt, original)
        record_native_launch(self.root, self.relay, event="capability-available")
        self.assertEqual(self.step()["action"], "wait_for_active_wave")
