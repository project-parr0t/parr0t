from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.wolf.batches import (
    MANUAL_QUEUE_FORMAT,
    apply_batches,
    export_manual_queue,
    merge_manual_queue,
    prepare_batches,
)

SYNTHETIC_SOURCE_NAME = "\u3042"
SYNTHETIC_SOURCE_CONTROL = "\u3044\\c[1]"
SYNTHETIC_SOURCE_SCENE = "\u3046"
SYNTHETIC_SOURCE_MANUAL = "\u3048"


class WolfBatchTests(unittest.TestCase):
    def test_repeated_source_occurrences_keep_distinct_stable_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "extracted-fixture"
            text_dir = source / "text" / "synthetic-fixture"
            text_dir.mkdir(parents=True)
            (text_dir / "__strings.json").write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "file": "fixture-script.txt",
                                "lines": [
                                    {"source": "@mes fixture-speaker", "text": "@mes fixture-speaker"},
                                    {"source": SYNTHETIC_SOURCE_SCENE, "text": SYNTHETIC_SOURCE_SCENE},
                                    {"source": SYNTHETIC_SOURCE_SCENE, "text": SYNTHETIC_SOURCE_SCENE},
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            batches = root / "batches"

            manifest = prepare_batches(source, batches)
            items = [
                item
                for path in sorted(batches.glob("batch_*.json"))
                for item in json.loads(path.read_text(encoding="utf-8"))["items"]
            ]

        self.assertEqual(manifest["item_count"], 2)
        self.assertEqual([item["source"] for item in items], [SYNTHETIC_SOURCE_SCENE] * 2)
        self.assertEqual(len({item["id"] for item in items}), 2)

    def test_prepare_and_apply_structured_names_and_text_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "extracted-fixture"
            text_dir = source / "text" / "synthetic-fixture"
            text_dir.mkdir(parents=True)
            (source / "names.json").write_text(
                json.dumps(
                    {
                        "names": [
                            {
                                "source": SYNTHETIC_SOURCE_NAME,
                                "text": SYNTHETIC_SOURCE_NAME,
                                "safety": "safe",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (source / "fixture-structured.json").write_text(
                json.dumps(
                    {
                        "scenes": [
                            {
                                "event": 1,
                                "page": 1,
                                "lines": [
                                    {
                                        "source": SYNTHETIC_SOURCE_CONTROL,
                                        "text": SYNTHETIC_SOURCE_CONTROL,
                                    }
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (text_dir / "__strings.json").write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "file": "fixture-script.txt",
                                "lines": [
                                    {"source": "@mes fixture-speaker", "text": "@mes fixture-speaker"},
                                    {"source": SYNTHETIC_SOURCE_SCENE, "text": SYNTHETIC_SOURCE_SCENE},
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            batches = root / "batches"
            translated = root / "mutable-fixture-copy"
            manifest = prepare_batches(source, batches, translated_copy=translated)
            self.assertEqual(manifest["item_count"], 3)
            batch_path = next(batches.glob("batch_*.json"))
            batch = json.loads(batch_path.read_text(encoding="utf-8"))
            targets = {
                SYNTHETIC_SOURCE_NAME: "TARGET_NAME",
                SYNTHETIC_SOURCE_CONTROL: "TARGET_CONTROL\\c[1]",
                SYNTHETIC_SOURCE_SCENE: "TARGET_SCENE",
            }
            for item in batch["items"]:
                item["target"] = targets[item["source"]]
            batch_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")

            report = apply_batches(batches, translated)
            self.assertEqual(report["applied_occurrences"], 3)
            names = json.loads((translated / "names.json").read_text(encoding="utf-8"))
            self.assertEqual(names["names"][0]["text"], "TARGET_NAME")
            event = json.loads((translated / "fixture-structured.json").read_text(encoding="utf-8"))
            self.assertEqual(event["scenes"][0]["lines"][0]["text"], "TARGET_CONTROL\\c[1]")
            text = json.loads(
                (translated / "text" / "synthetic-fixture" / "__strings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(text["files"][0]["lines"][1]["text"], "TARGET_SCENE")

    def test_manual_queue_and_sample_share_the_same_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batches = root / "batches"
            batches.mkdir()
            batch_path = batches / "batch_0001.json"
            batch_path.write_text(
                json.dumps(
                    {
                        "format": "wolf-rpg-agent-batch-v1",
                        "items": [
                            {
                                "id": "synthetic-manual-item",
                                "kind": "dialogue",
                                "speaker": "fixture-speaker",
                                "source": SYNTHETIC_SOURCE_MANUAL,
                                "context": {},
                                "target": "",
                                "manual": True,
                                "refs": [],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            queue_path = root / "fixture-manual-queue.json"
            sample_path = root / "fixture-sample.json"
            export_manual_queue(batches, queue_path, sample_path=sample_path)
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            sample = json.loads(sample_path.read_text(encoding="utf-8"))
            self.assertEqual(queue["format"], MANUAL_QUEUE_FORMAT)
            self.assertEqual(sample["format"], queue["format"])
            self.assertEqual(set(sample["items"][0]), set(queue["items"][0]))
            queue["items"][0]["target"] = "TARGET_MANUAL"
            queue_path.write_text(json.dumps(queue, ensure_ascii=False), encoding="utf-8")
            report = merge_manual_queue(queue_path, batches)
            self.assertEqual(report["merged"], 1)
            merged = json.loads(batch_path.read_text(encoding="utf-8"))["items"][0]
            self.assertEqual(merged["target"], "TARGET_MANUAL")
            self.assertNotIn("manual", merged)

    def test_apply_is_transactional_when_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            translated = root / "mutable-fixture-copy"
            translated.mkdir()
            target = translated / "fixture-structured.json"
            original = {"scenes": [{"lines": [{"source": "A\\c[1]", "text": "A\\c[1]"}]}]}
            target.write_text(json.dumps(original), encoding="utf-8")
            batches = root / "batches"
            batches.mkdir()
            (batches / "batch_0001.json").write_text(
                json.dumps(
                    {
                        "format": "wolf-rpg-agent-batch-v1",
                        "items": [
                            {
                                "id": "bad",
                                "source": "A\\c[1]",
                                "target": "B",
                                "refs": [
                                    {
                                        "json": "fixture-structured.json",
                                        "type": "structured",
                                        "path": ["scenes", 0, "lines", 0],
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                apply_batches(batches, translated)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), original)


if __name__ == "__main__":
    unittest.main()
