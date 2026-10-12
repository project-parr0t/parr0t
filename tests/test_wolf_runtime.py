from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.wolf.runtime import (
    WolfIntegrityError,
    protect_runtime_identifiers,
    validate_decompiled_pair,
    validate_text_script_commands,
)

SYNTHETIC_RUNTIME_KEY = "\u3042"
SYNTHETIC_VISIBLE_TEXT = "\u3044"
SYNTHETIC_DB_FIELD = "\u3046"
SYNTHETIC_DB_ROW = "\u3048"
SYNTHETIC_DB_COLUMN = "\u304a"
SYNTHETIC_EVENT_NAME = "\u304b"


class WolfRuntimeTests(unittest.TestCase):
    def test_runtime_comparison_keys_and_resource_paths_are_restored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "CommonEvent.dat.wscript"
            script.write_text(
                f'commonEvent 1 {{\n  SetString S[1] = "{SYNTHETIC_RUNTIME_KEY}"\n'
                f'  if S[1] == "{SYNTHETIC_RUNTIME_KEY}"\n}}\n',
                encoding="utf-8",
            )
            translated = root / "mutable-fixture-copy"
            translated.mkdir()
            event_path = translated / "CommonEvent.dat.json"
            event_path.write_text(
                json.dumps(
                    {
                        "scenes": [
                            {
                                "event": 1,
                                "lines": [
                                    {"source": SYNTHETIC_RUNTIME_KEY, "text": "TARGET_RUNTIME_KEY"},
                                    {"source": SYNTHETIC_VISIBLE_TEXT, "text": "TARGET_VISIBLE_TEXT"},
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            resource_path = translated / "fixture-resources.json"
            resource_path.write_text(
                json.dumps(
                    {
                        "entry": {
                            "source": "fixture/assets/synthetic-image.png",
                            "text": "TARGET_RESOURCE_PATH",
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            report = protect_runtime_identifiers(translated, {"CommonEvent.dat": script})
            self.assertEqual(report["restored_count"], 2)
            event = json.loads(event_path.read_text(encoding="utf-8"))
            self.assertEqual(event["scenes"][0]["lines"][0]["text"], SYNTHETIC_RUNTIME_KEY)
            self.assertEqual(event["scenes"][0]["lines"][1]["text"], "TARGET_VISIBLE_TEXT")
            resource = json.loads(resource_path.read_text(encoding="utf-8"))
            self.assertEqual(resource["entry"]["text"], "fixture/assets/synthetic-image.png")

    def test_wscript_audit_allows_visible_database_value_but_not_lookup_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.wscript"
            localized = root / "localized.wscript"
            original.write_text(
                f'commonEvent 1 {{\n  value = Database("{SYNTHETIC_VISIBLE_TEXT}", '
                f'"{SYNTHETIC_DB_FIELD}", "{SYNTHETIC_DB_ROW}", "{SYNTHETIC_DB_COLUMN}")\n'
                f'  CommonEventByName("{SYNTHETIC_EVENT_NAME}")\n}}\n',
                encoding="utf-8",
            )
            localized.write_text(
                f'commonEvent 1 {{\n  value = Database("TARGET_VISIBLE_TEXT", '
                f'"{SYNTHETIC_DB_FIELD}", "{SYNTHETIC_DB_ROW}", "{SYNTHETIC_DB_COLUMN}")\n'
                f'  CommonEventByName("{SYNTHETIC_EVENT_NAME}")\n}}\n',
                encoding="utf-8",
            )
            self.assertEqual(validate_decompiled_pair("CommonEvent.dat", original, localized), 4)
            localized.write_text(
                f'commonEvent 1 {{\n  value = Database("TARGET_VISIBLE_TEXT", '
                f'"TARGET_DB_FIELD", "{SYNTHETIC_DB_ROW}", "{SYNTHETIC_DB_COLUMN}")\n'
                f'  CommonEventByName("TARGET_EVENT_NAME")\n}}\n',
                encoding="utf-8",
            )
            with self.assertRaises(WolfIntegrityError) as caught:
                validate_decompiled_pair("CommonEvent.dat", original, localized)
            self.assertIn("database lookup identifier changed", str(caught.exception))
            self.assertIn("called event name changed", str(caught.exception))

    def test_text_script_audit_only_allows_visible_command_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original"
            localized = root / "localized"
            original.mkdir()
            localized.mkdir()
            (original / "fixture-scene.txt").write_text(
                f"@putselect {SYNTHETIC_VISIBLE_TEXT}\n@voice FIXTURE_AUDIO_ID 0\n", encoding="utf-8"
            )
            (localized / "fixture-scene.txt").write_text(
                "@putselect TARGET_VISIBLE_TEXT\n@voice FIXTURE_AUDIO_ID 0\n", encoding="utf-8"
            )
            self.assertEqual(validate_text_script_commands(original, localized), (2, 1))
            (localized / "fixture-scene.txt").write_text(
                "@putselect TARGET_VISIBLE_TEXT\n@voice CHANGED_RUNTIME_ID 0\n", encoding="utf-8"
            )
            with self.assertRaises(WolfIntegrityError):
                validate_text_script_commands(original, localized)


if __name__ == "__main__":
    unittest.main()
