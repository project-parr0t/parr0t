from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.control_plane import establish_control_plane_entry
from game_l10n.coverage import save_inventory_attestation
from game_l10n.data_plane_task import (
    claim_context_chunk,
    commit_context_fragment,
    prepare_data_plane_task,
)
from game_l10n.errors import ConfigurationError
from game_l10n.store import stable_hash


class ContextClaimBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.work, sdk, game = (root / name for name in ("work", "sdk", "game"))
        for path in (self.work, sdk, game):
            path.mkdir()
        establish_control_plane_entry(self.work)
        inputs = self.work / "inputs"
        inputs.mkdir()
        primary, independent = inputs / "primary.json", inputs / "independent.json"
        inventory = {
            "schema": "parr0t.source-inventory.v1",
            "method": "synthetic-primary",
            "implementation_fingerprint": "synthetic-primary-v1",
            "coverage": {"declared_complete": True, "exclusions": []},
            "segments": [
                {"id": f"synthetic-{index}", "source": f"SYNTHETIC LABEL {index} " + "." * 2200}
                for index in range(2)
            ],
        }
        primary.write_text(json.dumps(inventory), encoding="utf-8")
        independent.write_text(json.dumps({
            **inventory, "method": "synthetic-independent",
            "implementation_fingerprint": "synthetic-independent-v1",
        }), encoding="utf-8")
        save_inventory_attestation(self.work, primary, independent)
        config = {
            "game_roots": [str(game)], "sdk_root": str(sdk),
            "sdk_selection": {"inventory_fingerprint": "synthetic-sdk"},
            "files": {"context_pack": "context/context-pack.json"},
            "project": {"context_mode": "functional"},
            "routing": {"context_analyzers": ["native-sol"], "max_workers": 2,
                        "context_chunk_chars": 2048},
        }
        self.output = self.work / ".game-l10n/data-plane/context_analyzer"
        prepare_data_plane_task(
            self.work, self.output, worker_role="context_analyzer",
            config=config, sdk_activation={"status": "active"},
        )
        self.task_path = self.output / "task.json"

    def claim(self, slot: int = 1) -> tuple[Path, dict]:
        result = claim_context_chunk(self.work, self.task_path, slot=slot)
        path = self.work / result["claim_path"]
        return path, json.loads(path.read_text(encoding="utf-8"))

    def rewrite_claim(self, path: Path, claim: dict) -> None:
        unsigned = {key: claim[key] for key in (
            "schema", "task_id", "manifest_id", "binding_id", "binding",
            "source_index", "slot", "provider", "rows",
        )}
        if "localization_inputs" in claim:
            unsigned["localization_inputs"] = claim["localization_inputs"]
        claim["claim_id"] = stable_hash(unsigned)
        path.write_text(json.dumps(claim), encoding="utf-8")

    def result(self, claim: dict, *, success: bool = True) -> Path:
        path = self.output / f"result-{claim['slot']}.json"
        part = {
            "status": "success", "reason": "",
            "global_style_rules": ["Keep synthetic labels consistent."],
            "terminology_rules": [], "reference_cues": [],
            "functional_summary": "Synthetic fixture screen.",
            "screens": [{"screen": "synthetic", "purpose": "Synthetic fixture.",
                         "actions": [], "states": [], "constraints": [],
                         "evidence_line_ids": [row["id"] for row in claim["rows"]]}],
            "features": [],
        }
        path.write_text(json.dumps({
            "schema": "parr0t.context-chunk-result.v1", "claim_id": claim["claim_id"],
            "status": "success" if success else "error",
            "context_part": part if success else {},
            "issue_code": "" if success else "worker_error",
        }), encoding="utf-8")
        return path

    def test_self_hashed_task_route_change_is_rejected(self) -> None:
        task = json.loads(self.task_path.read_text(encoding="utf-8"))
        task["context_route"] = "native-unbound-synthetic"
        task.pop("task_id")
        task["task_id"] = stable_hash(task)
        self.task_path.write_text(json.dumps(task), encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "E_DATA_PLANE_RELAY_INVALID"):
            claim_context_chunk(self.work, self.task_path, slot=1)

    def test_unbound_task_copy_cannot_claim_work(self) -> None:
        copied = self.output / "unbound-task.json"
        copied.write_bytes(self.task_path.read_bytes())
        with self.assertRaisesRegex(ConfigurationError, "E_CONTEXT_TASK_INVALID"):
            claim_context_chunk(self.work, copied, slot=1)

    def test_forged_provider_cannot_poison_fragment_provenance(self) -> None:
        path, claim = self.claim()
        claim["provider"] = "native-unbound-synthetic"
        self.rewrite_claim(path, claim)
        with self.assertRaisesRegex(ConfigurationError, "E_CONTEXT_FRAGMENT_INVALID"):
            commit_context_fragment(self.work, self.task_path, path, self.result(claim))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "claimed")
        self.assertFalse((self.work / ".game-l10n/context-progress/fragments").exists())

    def test_forged_failure_cannot_consume_different_manifest_rows(self) -> None:
        path, original = self.claim()
        for field in ("manifest_id", "source_index", "rows"):
            with self.subTest(field=field):
                claim = json.loads(json.dumps(original))
                if field == "manifest_id":
                    claim[field] = "0" * 64
                elif field == "source_index":
                    claim[field] += 1
                else:
                    claim[field][0]["source"] = "SYNTHETIC UNBOUND SOURCE"
                self.rewrite_claim(path, claim)
                with self.assertRaisesRegex(ConfigurationError, "E_CONTEXT_FRAGMENT_INVALID"):
                    commit_context_fragment(
                        self.work, self.task_path, path, self.result(claim, success=False)
                    )
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "claimed")

    def test_unbound_claim_copy_is_not_rewritten_as_failed(self) -> None:
        path, claim = self.claim()
        copied = self.work / "unbound-claim.json"
        copied.write_bytes(path.read_bytes())
        with self.assertRaisesRegex(ConfigurationError, "E_CONTEXT_FRAGMENT_INVALID"):
            commit_context_fragment(
                self.work, self.task_path, copied, self.result(claim, success=False)
            )
        self.assertEqual(json.loads(copied.read_text(encoding="utf-8"))["status"], "claimed")

    def test_stale_active_pointer_never_returns_unbound_claim(self) -> None:
        path, claim = self.claim()
        copied = self.work / "unbound-active-claim.json"
        copied.write_bytes(path.read_bytes())
        state = path.parent.parent / "slots/slot-1.json"
        owner = json.loads(state.read_text(encoding="utf-8"))
        owner["active_claim_path"] = copied.relative_to(self.work).as_posix()
        state.write_text(json.dumps(owner), encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "E_CONTEXT_FRAGMENT_INVALID"):
            claim_context_chunk(self.work, self.task_path, slot=claim["slot"])

    def test_valid_claims_remain_exclusive_and_preserve_committed_progress(self) -> None:
        first_path, first = self.claim(slot=1)
        second_path, second = self.claim(slot=2)
        self.assertNotEqual(first["binding_id"], second["binding_id"])
        commit_context_fragment(self.work, self.task_path, first_path, self.result(first))
        pending = claim_context_chunk(self.work, self.task_path, slot=1)
        self.assertEqual(pending["status"], "exhausted")
        self.assertEqual((pending["success"], pending["pending"]), (1, 1))
        commit_context_fragment(self.work, self.task_path, second_path, self.result(second))
        completed = claim_context_chunk(self.work, self.task_path, slot=2)
        self.assertEqual((completed["success"], completed["pending"]), (2, 0))


if __name__ == "__main__":
    unittest.main()
