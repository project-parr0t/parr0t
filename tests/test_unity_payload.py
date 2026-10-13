from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.coverage import save_inventory_attestation
from game_l10n.unity.payload import compile_runtime_payload


def write_verified_inventory(root: Path, rows: list[dict]) -> Path:
    primary = root / "fixture-segments.json"
    independent = root / "fixture-segments-independent.json"
    base = {
        "schema": "parr0t.source-inventory.v1",
        "coverage": {"declared_complete": True, "exclusions": []},
        "segments": rows,
    }
    primary.write_text(
        json.dumps(
            {
                **base,
                "method": "unity-serialized-resource-walk",
                "implementation_fingerprint": "fixture-primary-v1",
            }
        ),
        encoding="utf-8",
    )
    independent.write_text(
        json.dumps(
            {
                **base,
                "method": "unity-type-index-audit",
                "implementation_fingerprint": "fixture-independent-v1",
            }
        ),
        encoding="utf-8",
    )
    save_inventory_attestation(root, primary, independent)
    return primary


class UnityPayloadTests(unittest.TestCase):
    def test_compiles_reviewed_targets_by_engine_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reviewed = root / "fixture-reviewed.json"
            output = root / "fixture-runtime.json"
            segments = write_verified_inventory(
                root,
                [
                    {
                        "id": "fixture-table-1",
                        "source": "SOURCE_MENU",
                        "metadata": {
                            "unity_kind": "string_table",
                            "collection": "fixture_collection",
                            "entry_id": "1001",
                            "is_smart": True,
                            "ui_path": "FixtureRoot/FixtureLabel",
                        },
                    },
                    {
                        "id": "fixture-asset-1",
                        "source": "SOURCE_SCENE",
                        "metadata": {"unity_kind": "text_asset", "asset_key": "fixture_asset"},
                    },
                    {
                        "id": "fixture-literal-1",
                        "source": "SOURCE_LABEL",
                        "metadata": {"unity_kind": "literal"},
                    },
                ],
            )
            reviewed.write_text(
                json.dumps(
                    {
                        "results": [
                            {"id": "fixture-table-1", "source": "SOURCE_MENU", "text": "TARGET_MENU", "status": "reviewed"},
                            {"id": "fixture-asset-1", "source": "SOURCE_SCENE", "text": "TARGET_SCENE", "status": "reviewed"},
                            {"id": "fixture-literal-1", "source": "SOURCE_LABEL", "text": "SOURCE_LABEL", "status": "source_preserved"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            summary = compile_runtime_payload(
                segments, reviewed, output, work_dir=root, locale="ko-KR"
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(summary["segments"], 3)
        self.assertEqual(summary["source_preserved"], 1)
        self.assertEqual(
            payload["string_tables"]["fixture_collection"]["1001"],
            {"text": "TARGET_MENU", "is_smart": True},
        )
        self.assertEqual(payload["text_assets"], {"fixture_asset": "TARGET_SCENE"})
        self.assertEqual(payload["literal_strings"], {"SOURCE_LABEL": "SOURCE_LABEL"})
        self.assertEqual(payload["ui_paths"], ["FixtureRoot/FixtureLabel"])
        self.assertTrue(payload["inventory_fingerprint"])

    def test_refuses_unreviewed_results_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reviewed = root / "fixture-reviewed.json"
            segments = write_verified_inventory(
                root,
                [
                    {
                        "id": "fixture-1",
                        "source": "SOURCE_TEXT",
                        "metadata": {"unity_kind": "literal"},
                    }
                ],
            )
            reviewed.write_text(
                json.dumps(
                    {
                        "results": [
                            {"id": "fixture-1", "source": "SOURCE_TEXT", "text": "TARGET_TEXT", "status": "translated"}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                compile_runtime_payload(
                    segments,
                    reviewed,
                    root / "fixture-runtime.json",
                    work_dir=root,
                    locale="ko",
                )

    def test_refuses_to_overwrite_source_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = write_verified_inventory(
                root,
                [{"id": "fixture-1", "source": "SOURCE_TEXT", "metadata": {"unity_kind": "literal"}}],
            )
            reviewed = root / "fixture-reviewed.json"
            reviewed.write_text('{"results": []}', encoding="utf-8")
            with self.assertRaises(ValueError):
                compile_runtime_payload(
                    segments, reviewed, segments, work_dir=root, locale="ko"
                )

    def test_blocking_failure_queue_holds_runtime_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = write_verified_inventory(
                root,
                [{"id": "fixture-1", "source": "SOURCE_TEXT", "metadata": {"unity_kind": "literal"}}],
            )
            reviewed = root / "fixture-reviewed.json"
            reviewed.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "id": "fixture-1",
                                "source": "SOURCE_TEXT",
                                "text": "SOURCE_TEXT",
                                "status": "source_preserved",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            attestation = json.loads(
                (root / ".game-l10n" / "inventory-attestation.json").read_text(
                    encoding="utf-8"
                )
            )
            (root / ".game-l10n" / "failure-queue.json").write_text(
                json.dumps(
                    {
                        "schema": "parr0t.failure-queue.v1",
                        "inventory_fingerprint": attestation["primary_fingerprint"],
                        "blocking_count": 1,
                        "entries": [{"target_id": "fixture-1", "blocking": True}],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "blocking"):
                compile_runtime_payload(
                    segments,
                    reviewed,
                    root / "fixture-runtime.json",
                    work_dir=root,
                    locale="ko",
                )


if __name__ == "__main__":
    unittest.main()
