from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.unity.payload import compile_runtime_payload


class UnityPayloadTests(unittest.TestCase):
    def test_compiles_reviewed_targets_by_engine_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = root / "fixture-segments.json"
            reviewed = root / "fixture-reviewed.json"
            output = root / "fixture-runtime.json"
            segments.write_text(
                json.dumps(
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
                    ]
                ),
                encoding="utf-8",
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

            summary = compile_runtime_payload(segments, reviewed, output, locale="ko-KR")
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

    def test_refuses_unreviewed_results_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = root / "fixture-segments.json"
            reviewed = root / "fixture-reviewed.json"
            segments.write_text(
                json.dumps(
                    [
                        {
                            "id": "fixture-1",
                            "source": "SOURCE_TEXT",
                            "metadata": {"unity_kind": "literal"},
                        }
                    ]
                ),
                encoding="utf-8",
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
                compile_runtime_payload(segments, reviewed, root / "fixture-runtime.json", locale="ko")

    def test_refuses_to_overwrite_source_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = root / "fixture-segments.json"
            reviewed = root / "fixture-reviewed.json"
            segments.write_text("[]", encoding="utf-8")
            reviewed.write_text('{"results": []}', encoding="utf-8")
            with self.assertRaises(ValueError):
                compile_runtime_payload(segments, reviewed, segments, locale="ko")


if __name__ == "__main__":
    unittest.main()
