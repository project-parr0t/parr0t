from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from game_l10n.formats import read_segments
from game_l10n.models import Segment
from game_l10n.native_pass import native_control_step, prepare_native_tasks, seal_native_wave_relay
from game_l10n.store import stable_hash


class NativePreparationResumeTests(unittest.TestCase):
    def _prepare(self, root: Path) -> tuple[Path, list[Segment], Path, Path]:
        source, tasks, results = root / "source.jsonl", root / "tasks", root / "results"
        source.write_text("\n".join(json.dumps(asdict(Segment(k, "SOURCE"))) for k in ("a", "b")) + "\n",
                          encoding="utf-8")
        segments = read_segments(source)
        prepare_native_tasks(root, source, segments, tasks, max_records=1, max_workers=1)
        return source, segments, tasks, results

    def _fingerprint(self, manifest: dict) -> None:
        keys = ("worker_contract", "detail_contract", "execution_contract", "result_contract",
                "route_binding", "language_binding", "execution_namespace")
        manifest["contract_fingerprint"] = stable_hash({
            key: manifest[key] for key in keys
            if key in manifest and (key != "route_binding" or manifest[key] is not None)
        })

    def test_reprepare_preserves_accepted_legacy_contract_and_imported_wave(self) -> None:
        for variant in ("no-result-contract", "old-detail", "both-without-namespace"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, segments, tasks, results = self._prepare(root)
                path = tasks / "manifest.json"
                manifest = json.loads(path.read_text(encoding="utf-8"))
                if variant != "old-detail":
                    manifest.pop("result_contract")
                if variant != "no-result-contract":
                    manifest["detail_contract"]["review_control"]["allowed_actions"].remove("repair_native_runtime")
                if variant == "both-without-namespace":
                    manifest.pop("execution_namespace")
                self._fingerprint(manifest)
                path.write_text(json.dumps(manifest), encoding="utf-8")
                first = native_control_step(root, path, results,
                                            usage_event_id="fixture-usage", input_tokens=234)
                first_result = results / manifest["tasks"][0]["file"]
                first_result.write_text(json.dumps({"target_id": "a", "status": "success", "text": "TARGET"}) + "\n",
                                        encoding="utf-8")
                seal_native_wave_relay(root, root / first["relay"]["dispatch"], relay_ended=True)
                partial = native_control_step(root, path, results, require_relay_receipt=True)
                self.assertEqual(partial["counts"]["imported_tasks"], 1)
                before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                reused = prepare_native_tasks(root, source, segments, tasks, max_records=1, max_workers=1)
                self.assertTrue(reused["cached"])
                self.assertEqual(reused["contract_fingerprint"], manifest["contract_fingerprint"])
                self.assertEqual({p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}, before)
                resumed = native_control_step(root, path, results, require_relay_receipt=True)
                self.assertEqual(resumed["action"], "wait_for_active_wave")
                self.assertEqual(resumed["counts"]["imported_tasks"], 1)
                self.assertEqual(resumed["counts"]["pending_tasks"], 1)
                self.assertEqual(resumed["usage"]["input_tokens"], 234)
                self.assertEqual(first_result.read_bytes(), before[first_result.relative_to(root)])

    def test_present_noncanonical_contract_is_rejected_without_rewriting_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, segments, tasks, _ = self._prepare(root)
            path = tasks / "manifest.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["result_contract"]["format"] = "csv"
            self._fingerprint(manifest)
            path.write_text(json.dumps(manifest), encoding="utf-8")
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            with self.assertRaisesRegex(ValueError, "E_DETAIL_CONTRACT_CONFLICT"):
                prepare_native_tasks(root, source, segments, tasks, max_records=1, max_workers=1)
            self.assertEqual({p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}, before)


if __name__ == "__main__":
    unittest.main()
