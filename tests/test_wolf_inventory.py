from __future__ import annotations

import argparse
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

from game_l10n.coverage import completion_attestation, save_inventory_attestation, verify_inventory_pair
from game_l10n.errors import ToolExecutionError
from game_l10n.formats import write_results
from game_l10n.language_policy import language_binding
from game_l10n.models import Segment, TranslationResult
from game_l10n.wolf import cli
from game_l10n.wolf.batches import import_reviewed_results, prepare_batches
from game_l10n.wolf.inventory import require_wolf_patch_gate, write_inventory_pair


class WolfInventoryTests(unittest.TestCase):
    def _source_tree(self, root: Path) -> Path:
        source = root / "extracted-fixture"
        text = source / "text" / "synthetic-fixture"
        text.mkdir(parents=True)
        (source / "names.json").write_text(
            json.dumps(
                {
                    "names": [
                        {"source": "\u3042", "text": "\u3042", "safety": "safe"}
                    ]
                }
            ),
            encoding="utf-8",
        )
        (source / "fixture-structured.json").write_text(
            json.dumps(
                {
                    "scenes": [
                        {
                            "lines": [
                                {
                                    "source": "\u3044",
                                    "text": "\u3044",
                                    "speaker": "fixture-speaker",
                                }
                            ]
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (text / "__strings.json").write_text(
            json.dumps(
                {
                    "files": [
                        {
                            "file": "fixture-script.txt",
                            "lines": [
                                {
                                    "source": "@mes fixture-speaker",
                                    "text": "@mes fixture-speaker",
                                },
                                {"source": "\u3046", "text": "\u3046"},
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return source

    def _incomplete_workspace(self, root: Path) -> tuple[Path, dict]:
        source = self._source_tree(root)
        batches = root / "batches"
        prepare_batches(source, batches, translated_copy=root / "translated-json")
        primary = root / "inputs" / "wolf-primary.json"
        independent = root / "inputs" / "wolf-independent.json"
        write_inventory_pair(source, batches, primary, independent)
        inventory = save_inventory_attestation(root, primary, independent)
        config = {
            "sdk_root": str(root.parent / "synthetic-sdk"),
            "project": {"source_language": "ja", "target_language": "ko"},
            "failure_queue": {"allow_incomplete_artifact": True},
        }
        (root / "game-l10n.json").write_text(json.dumps(config), encoding="utf-8")
        segments = [
            Segment(row["id"], row["source"], metadata={"blocking_on_failure": True})
            for row in json.loads(primary.read_text(encoding="utf-8"))["segments"]
        ]
        rows = [
            TranslationResult(
                segment_id=segment.id, source=segment.source,
                text=segment.source, status="source_preserved",
            )
            for segment in segments
        ]
        reviewed = root / "reviewed.json"
        write_results(reviewed, rows)
        completion = completion_attestation(
            segments, rows, inventory_segments=segments, inventory=inventory,
            runtime={}, review_required=True, target_language="ko",
            language_binding=language_binding(root, config), allow_incomplete_artifact=True,
        )
        completion["result_artifact"] = {
            "path": str(reviewed.resolve()),
            "size": reviewed.stat().st_size,
            "sha256": hashlib.sha256(reviewed.read_bytes()).hexdigest(),
        }
        (root / ".game-l10n" / "completion-attestation.json").write_text(
            json.dumps(completion), encoding="utf-8"
        )
        (root / ".game-l10n" / "failure-queue.json").write_text(
            json.dumps({
                "schema": "parr0t.failure-queue.v1",
                "inventory_fingerprint": inventory["primary_fingerprint"],
                "blocking_count": len(segments),
                "entries": [
                    {"target_id": segment.id, "blocking": True} for segment in segments
                ],
            }),
            encoding="utf-8",
        )
        (root / "built-data").mkdir()
        (root / "Game.wolf").write_bytes(b"synthetic-key-fixture")
        return reviewed, completion

    def _consume(self, root: Path, step: str, *, reviewed: str = "reviewed.json") -> dict:
        args = argparse.Namespace(
            workspace=str(root), segments="inputs/wolf-primary.json", reviewed=reviewed,
            batches="batches", translated_json="translated-json", allow_incomplete=False,
            packer="synthetic-packer", built_data="built-data", game_archive="Game.wolf",
            output="replacement-archives",
        )
        config = json.loads((root / "game-l10n.json").read_text(encoding="utf-8"))
        output = io.StringIO()
        # Provider configuration is unrelated to these engine consumer checks.
        with patch.object(cli, "load_config", return_value=config), redirect_stdout(output):
            status = getattr(cli, f"cmd_{step}")(args)
        self.assertEqual(status, 0)
        return json.loads(output.getvalue())

    def test_independent_inventory_matches_batches_and_imports_reviewed_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._source_tree(root)
            batches = root / "batches"
            prepare_batches(source, batches)
            primary = root / "inputs" / "wolf-primary.json"
            independent = root / "inputs" / "wolf-independent.json"
            write_inventory_pair(source, batches, primary, independent)

            state = verify_inventory_pair(primary, independent)
            self.assertTrue(state["static_complete"])
            self.assertEqual(state["primary_segments"], 3)
            save_inventory_attestation(root, primary, independent)

            inventory = json.loads(primary.read_text(encoding="utf-8"))
            reviewed = root / "reviewed.json"
            reviewed.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "segment_id": row["id"],
                                "source": row["source"],
                                "text": f"TARGET_{index}",
                                "status": "reviewed" if index == 1 else "accepted_unreviewed",
                            }
                            for index, row in enumerate(inventory["segments"][:2], 1)
                        ]
                    }
                ),
                encoding="utf-8",
            )

            summary = import_reviewed_results(reviewed, batches)
            imported_targets = [
                item["target"]
                for path in sorted(batches.glob("batch_*.json"))
                for item in json.loads(path.read_text(encoding="utf-8"))["items"]
            ]

        self.assertEqual(summary["imported"], 3)
        self.assertEqual(summary["accepted_unreviewed"], 1)
        self.assertEqual(summary["source_preserved_missing"], 1)
        self.assertEqual(
            imported_targets,
            ["TARGET_1", "TARGET_2", inventory["segments"][2]["source"]],
        )

    def test_import_source_preserves_structurally_invalid_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batches = root / "batches"
            batches.mkdir()
            (batches / "batch_0001.json").write_text(
                json.dumps(
                    {
                        "format": "wolf-rpg-agent-batch-v2",
                        "items": [
                            {
                                "id": "fixture-control",
                                "source": "SOURCE\\c[1]",
                                "target": "",
                                "refs": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            results = root / "delivery.json"
            results.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "segment_id": "fixture-control",
                                "source": "SOURCE\\c[1]",
                                "text": "TARGET",
                                "status": "accepted_unreviewed",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            summary = import_reviewed_results(results, batches)
            item = json.loads((batches / "batch_0001.json").read_text())["items"][0]

            self.assertEqual(item["target"], "SOURCE\\c[1]")
            self.assertEqual(summary["source_preserved_invalid"], 1)

    def test_blocking_failure_queue_holds_wolf_patch_steps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._source_tree(root)
            batches = root / "batches"
            prepare_batches(source, batches)
            primary = root / "inputs" / "wolf-primary.json"
            independent = root / "inputs" / "wolf-independent.json"
            write_inventory_pair(source, batches, primary, independent)
            state = save_inventory_attestation(root, primary, independent)
            queue = root / ".game-l10n" / "failure-queue.json"
            queue.write_text(
                json.dumps(
                    {
                        "schema": "parr0t.failure-queue.v1",
                        "inventory_fingerprint": state["primary_fingerprint"],
                        "blocking_count": 1,
                        "entries": [
                            {"target_id": "synthetic-blocking", "blocking": True}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ToolExecutionError, "incomplete artifact authorization"):
                require_wolf_patch_gate(root, primary)

    def test_authorized_incomplete_artifact_traverses_import_apply_and_pack_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, completion = self._incomplete_workspace(root)
            queue_path = root / ".game-l10n" / "failure-queue.json"
            completion_path = root / ".game-l10n" / "completion-attestation.json"
            queue_before = queue_path.read_bytes()
            completion_before = completion_path.read_bytes()
            source_before = {
                path.relative_to(root / "extracted-fixture"): path.read_bytes()
                for path in (root / "extracted-fixture").rglob("*.json")
            }

            imported = self._consume(root, "import_reviewed")
            self.assertEqual(imported["source_preserved"], 3)
            self.assertEqual(imported["source_preserved_invalid"], 0)
            applied = self._consume(root, "apply_batches")
            self.assertEqual(applied["applied_occurrences"], 3)
            self.assertEqual(applied["errors"], [])
            # Only the external-tool boundary is replaced; no packer executes.
            with (
                patch.object(cli, "_tool_command", return_value=["synthetic-packer"]),
                patch.object(cli, "pack_pro_archives", return_value=[]) as packer,
            ):
                self._consume(root, "pack_pro")
                packer.assert_called_once_with(
                    ["synthetic-packer"], root / "built-data", root / "Game.wolf",
                    root / "replacement-archives",
                )

            self.assertFalse(completion["target_completion"])
            self.assertFalse(completion["static_completion"])
            self.assertEqual(completion["semantic_review"]["blocking_unresolved_count"], 3)
            self.assertEqual(queue_path.read_bytes(), queue_before)
            self.assertEqual(completion_path.read_bytes(), completion_before)
            for relative, content in source_before.items():
                self.assertEqual((root / "extracted-fixture" / relative).read_bytes(), content)

    def test_consumers_deny_unapproved_stale_or_misaligned_incomplete_artifacts(self) -> None:
        mutations = (
            "consent", "authorization", "artifact", "inventory", "alignment",
            "integrity", "language",
        )
        for step in ("import_reviewed", "apply_batches", "pack_pro"):
            for mutation in mutations:
                with self.subTest(step=step, mutation=mutation), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    reviewed, completion = self._incomplete_workspace(root)
                    if mutation == "consent":
                        config_path = root / "game-l10n.json"
                        config = json.loads(config_path.read_text(encoding="utf-8"))
                        config["failure_queue"]["allow_incomplete_artifact"] = False
                        config_path.write_text(json.dumps(config), encoding="utf-8")
                    elif mutation == "authorization":
                        completion["incomplete_artifact_authorized"] = False
                    elif mutation == "artifact":
                        reviewed.write_bytes(reviewed.read_bytes() + b" ")
                    elif mutation == "inventory":
                        completion["source_inventory"]["fingerprint"] = "stale-inventory"
                    elif mutation == "alignment":
                        completion["result_alignment"]["source_mismatch_count"] = 1
                    elif mutation == "integrity":
                        completion["issue_codes"].append("SOURCE_PRESERVED_AFTER_INTEGRITY_REJECTION")
                    else:
                        completion["target_language"]["binding"]["source_language"] = "en"
                    (root / ".game-l10n" / "completion-attestation.json").write_text(
                        json.dumps(completion), encoding="utf-8"
                    )
                    batch_before = {
                        path: path.read_bytes() for path in (root / "batches").glob("batch_*.json")
                    }
                    with (
                        patch.object(cli, "pack_pro_archives") as packer,
                        patch.object(cli, "_tool_command") as tool_command,
                        self.assertRaises(ToolExecutionError),
                    ):
                        self._consume(root, step)
                    packer.assert_not_called()
                    tool_command.assert_not_called()
                    self.assertFalse((root / "batches" / "apply_report.json").exists())
                    for path, content in batch_before.items():
                        self.assertEqual(path.read_bytes(), content)

    def test_import_binds_actual_reviewed_path_even_when_alternate_has_identical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reviewed, _ = self._incomplete_workspace(root)
            alternate = root / "alternate.json"
            alternate.write_bytes(reviewed.read_bytes())
            with self.assertRaisesRegex(ToolExecutionError, "not the result artifact bound"):
                self._consume(root, "import_reviewed", reviewed=alternate.name)
            for path in (root / "batches").glob("batch_*.json"):
                self.assertTrue(all(
                    not item["target"]
                    for item in json.loads(path.read_text(encoding="utf-8"))["items"]
                ))

    def test_queue_schema_and_inventory_still_gate_authorized_incomplete_artifact(self) -> None:
        for mutation in ("schema", "inventory_fingerprint"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                reviewed, _ = self._incomplete_workspace(root)
                queue_path = root / ".game-l10n" / "failure-queue.json"
                queue = json.loads(queue_path.read_text(encoding="utf-8"))
                queue[mutation] = "stale-or-unsupported"
                queue_path.write_text(json.dumps(queue), encoding="utf-8")
                with self.assertRaises(ValueError):
                    require_wolf_patch_gate(
                        root, root / "inputs" / "wolf-primary.json", result_path=reviewed
                    )

    def test_queue_zero_and_missing_queue_need_no_incomplete_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._incomplete_workspace(root)
            (root / ".game-l10n" / "completion-attestation.json").unlink()
            (root / "game-l10n.json").unlink()
            queue_path = root / ".game-l10n" / "failure-queue.json"
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["blocking_count"] = 0
            queue["entries"] = []
            queue_path.write_text(json.dumps(queue), encoding="utf-8")
            primary = root / "inputs" / "wolf-primary.json"
            self.assertTrue(require_wolf_patch_gate(root, primary)["static_complete"])
            queue_path.unlink()
            self.assertTrue(require_wolf_patch_gate(root, primary)["static_complete"])


if __name__ == "__main__":
    unittest.main()
