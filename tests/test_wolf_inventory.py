from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.coverage import save_inventory_attestation, verify_inventory_pair
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

            with self.assertRaisesRegex(ValueError, "blocking"):
                require_wolf_patch_gate(root, primary)


if __name__ == "__main__":
    unittest.main()
