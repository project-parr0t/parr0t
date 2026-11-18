from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import sys
import unittest

from game_l10n.coverage import completion_attestation
from game_l10n.errors import ToolExecutionError
from game_l10n.formats import write_results
from game_l10n.language_policy import language_binding
from game_l10n.models import FailureCode, Segment, TranslationResult
from game_l10n.tooling import _require_pack_input_binding, require_incomplete_artifact, run_cached_tool


class IncompleteArtifactTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[dict, Path, Path, dict]:
        config = {
            "project": {"source_language": "en", "target_language": "ko"},
            "failure_queue": {"allow_incomplete_artifact": True},
        }
        (root / "game-l10n.json").write_text(json.dumps(config), encoding="utf-8")
        segment = Segment("one", "SYNTHETIC SOURCE", metadata={"blocking_on_failure": True})
        row = TranslationResult(segment_id=segment.id, source=segment.source, text=segment.source, status="source_preserved")
        inventory = {
            "status": "static_verified", "static_complete": True,
            "primary_segments": 1, "primary_fingerprint": "synthetic-inventory",
        }
        result_path = root / "results.json"
        write_results(result_path, [row])
        completion = completion_attestation(
            [segment], [row], inventory_segments=[segment], inventory=inventory,
            runtime={}, review_required=True, target_language="ko",
            language_binding=language_binding(root, config), allow_incomplete_artifact=True,
        )
        self._bind(completion, result_path)
        path = root / ".game-l10n/completion-attestation.json"
        path.parent.mkdir()
        path.write_text(json.dumps(completion), encoding="utf-8")
        return inventory, result_path, path, completion

    @staticmethod
    def _bind(completion: dict, path: Path) -> None:
        completion["result_artifact"] = {
            "path": str(path.resolve()), "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    def test_exact_current_incomplete_artifact_works_with_or_without_result_argument(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory, result, _, completion = self._fixture(root)
            receipt = require_incomplete_artifact(root, inventory)
            self.assertEqual(receipt["status"], "explicit_incomplete")
            self.assertEqual(receipt["result_artifact"], completion["result_artifact"])
            self.assertEqual(require_incomplete_artifact(root, inventory, result_path=result), receipt)
            other = root / "same-bytes.json"
            other.write_bytes(result.read_bytes())
            with self.assertRaises(ToolExecutionError):
                require_incomplete_artifact(root, inventory, result_path=other)
            result.write_text("changed", encoding="utf-8")
            with self.assertRaises(ToolExecutionError):
                require_incomplete_artifact(root, inventory)

    def test_both_consent_and_bound_safe_completion_are_mandatory(self) -> None:
        for mutation in ("consent", "authorization", "inventory", "alignment", "scope", "failed", "integrity", "language"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                inventory, _, path, completion = self._fixture(root)
                if mutation == "consent":
                    config_path = root / "game-l10n.json"
                    config = json.loads(config_path.read_text(encoding="utf-8"))
                    config["failure_queue"]["allow_incomplete_artifact"] = False
                    config_path.write_text(json.dumps(config), encoding="utf-8")
                elif mutation == "authorization":
                    completion["incomplete_artifact_authorized"] = False
                elif mutation == "inventory":
                    inventory["primary_fingerprint"] = "other"
                elif mutation == "alignment":
                    completion["result_alignment"]["source_mismatch_count"] = 1
                elif mutation == "scope":
                    completion["target_scope"]["full_inventory"] = False
                elif mutation == "failed":
                    completion["semantic_review"]["failed_count"] = 1
                elif mutation == "integrity":
                    completion["issue_codes"].append("SOURCE_PRESERVED_AFTER_INTEGRITY_REJECTION")
                else:
                    completion["target_language"]["binding"]["source_language"] = "ja"
                path.write_text(json.dumps(completion), encoding="utf-8")
                with self.assertRaises(ToolExecutionError):
                    require_incomplete_artifact(root, inventory)

    def test_cached_pack_rechecks_current_incomplete_artifact_consent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, result, _, completion = self._fixture(root)
            tool = root / "pack.py"
            tool.write_text(
                "import shutil, sys\nshutil.copyfile(sys.argv[1], sys.argv[2])\n",
                encoding="utf-8",
            )
            output = root / "packed.bin"
            arguments = dict(
                stage="pack", command=[sys.executable, str(tool), "{input}", "{output}"],
                input_path=result, output_path=output, work_dir=root,
            )
            self.assertEqual(run_cached_tool(**arguments)["status"], "completed")
            self.assertEqual(run_cached_tool(**arguments)["status"], "skipped")
            config_path = root / "game-l10n.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["failure_queue"]["allow_incomplete_artifact"] = False
            config_path.write_text(json.dumps(config), encoding="utf-8")
            self.assertTrue(completion["incomplete_artifact_authorized"])
            with self.assertRaisesRegex(ToolExecutionError, "current explicit incomplete-artifact permission"):
                run_cached_tool(**arguments)
            self.assertEqual(output.read_bytes(), result.read_bytes())

    def test_complete_artifact_does_not_need_incomplete_permission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory, result, path, _ = self._fixture(root)
            config_path = root / "game-l10n.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            segment = Segment("one", "SYNTHETIC SOURCE")
            row = TranslationResult(
                segment_id=segment.id, source=segment.source, text=segment.source,
                status="source_preserved",
            )
            completion = completion_attestation(
                [segment], [row], inventory_segments=[segment], inventory=inventory,
                runtime={}, review_required=True, target_language="ko",
                language_binding=language_binding(root, config), allow_incomplete_artifact=True,
            )
            self.assertTrue(completion["static_completion"])
            self.assertTrue(completion["incomplete_artifact_authorized"])
            self._bind(completion, result)
            path.write_text(json.dumps(completion), encoding="utf-8")
            config["failure_queue"]["allow_incomplete_artifact"] = False
            config_path.write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(_require_pack_input_binding(root, result), completion["result_artifact"]["sha256"])

    def test_artifact_authorization_does_not_reinterpret_lossless_or_delivery_formats(self) -> None:
        for suffix in (".json", ".jsonl", ".csv"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                inventory, _, path, completion = self._fixture(root)
                result = root / ("exact-result" + suffix)
                row = TranslationResult(
                    segment_id="one", source="SYNTHETIC SOURCE", text="SYNTHETIC SOURCE",
                    status="source_preserved",
                )
                write_results(result, [row])
                self._bind(completion, result)
                path.write_text(json.dumps(completion), encoding="utf-8")
                self.assertEqual(
                    require_incomplete_artifact(root, inventory, result_path=result)["result_artifact"],
                    completion["result_artifact"],
                )

    def test_actual_core_integrity_rejection_never_authorizes_an_incomplete_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory, result, path, _ = self._fixture(root)
            config = json.loads((root / "game-l10n.json").read_text(encoding="utf-8"))
            segment = Segment("one", "SYNTHETIC SOURCE", metadata={"blocking_on_failure": True})
            unsafe = TranslationResult(
                segment_id=segment.id, source=segment.source, text=segment.source,
                status="source_preserved", resolution_code=FailureCode.INTEGRITY_INVALID.value,
            )
            write_results(result, [unsafe])
            completion = completion_attestation(
                [segment], [unsafe], inventory_segments=[segment], inventory=inventory,
                runtime={}, review_required=True, target_language="ko",
                language_binding=language_binding(root, config), allow_incomplete_artifact=True,
            )
            self.assertFalse(completion["incomplete_artifact_authorized"])
            self._bind(completion, result)
            path.write_text(json.dumps(completion), encoding="utf-8")
            with self.assertRaises(ToolExecutionError):
                require_incomplete_artifact(root, inventory)


if __name__ == "__main__":
    unittest.main()
