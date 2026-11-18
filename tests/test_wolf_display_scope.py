from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.formats import read_segments
from game_l10n.wolf.batches import apply_batches, prepare_batches
from game_l10n.wolf.inventory import write_inventory_pair


def row(source: str, **fields: object) -> dict:
    return {"source": source, "text": source, **fields}


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class WolfDisplayScopeTests(unittest.TestCase):
    def test_consumer_roles_pending_census_context_and_source_integrity(self) -> None:
        text = [chr(0x3042 + index) for index in range(16)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            save(source / "names.json", {"names": [row(text[0], safety="safe")]})
            structured = {
                "scenes": [{"lines": [row(text[1], speaker="dialogue"),
                                       row(text[2], speaker="system/error"),
                                       row("@putselect " + text[3]),
                                       row("#" + text[4]),
                                       row("@unknown_render " + text[5])]}],
                "engine": [row(text[6], speaker="system/error")],
                "module": {"scenes": [{"lines": [row(text[7])]}]},
                "metadata": row(text[8]),
                "debug": row(text[9]),
            }
            save(source / "events.json", structured)
            # An entirely unsupported file must remain byte-for-byte unchanged.
            save(source / "unsupported.json", {"unknown_display": row(text[10])})
            script = {"files": [{"file": "fixture-script", "lines": [
                row("@mes fixture"), row(text[11]), row("not-selected-language"),
                row("@unknown_render " + text[12]), row("#" + text[13]),
                row("@toki-teach " + text[14]), row(text[15]),
            ]}]}
            # A directory name is not proof of non-display.
            script_path = Path("text/debug/__strings.json")
            save(source / script_path, script)
            original = {path.relative_to(source): path.read_bytes() for path in source.rglob("*.json")}
            batches = root / "batches"
            translated = root / "translated"
            manifest = prepare_batches(source, batches, translated_copy=translated)
            primary, independent = write_inventory_pair(
                source, batches, root / "primary.json", root / "independent.json")
            expected = [text[index] for index in (0, 1, 2, 3, 11, 14, 15)]
            for inventory in (primary, independent):
                segments = read_segments(inventory)
                self.assertEqual([segment.source for segment in segments], expected)
                payload = json.loads(inventory.read_text(encoding="utf-8"))
                self.assertFalse(payload["coverage"]["declared_complete"])
                scope = payload["coverage"]["display_scope"]
                self.assertEqual(scope["raw_occurrence_count"], 18)
                self.assertEqual(scope["display_candidate_count"], 7)
                self.assertEqual(scope["control_only_count"], 3)
                self.assertEqual(scope["unclassified_count"], 7)
                self.assertEqual(len(payload["raw_occurrences"]), 18)
                self.assertEqual({item["reason_code"] for item in payload["coverage"]["exclusions"]},
                                 {"WOLF_UNCLASSIFIED_FORMAT_ROLE"})
                self.assertTrue(all(segment.metadata["display_binding_id"] for segment in segments))
            inventories = [json.loads(path.read_text(encoding="utf-8")) for path in (primary, independent)]
            self.assertEqual(inventories[0]["coverage"], inventories[1]["coverage"])
            self.assertEqual(inventories[0]["raw_occurrences"], inventories[1]["raw_occurrences"])
            self.assertEqual(manifest["coverage"], inventories[0]["coverage"])
            segments = read_segments(primary)
            first_script = next(segment for segment in segments if segment.source == text[11])
            self.assertEqual(first_script.next, text[14])
            notification = next(segment for segment in segments if segment.source == text[14])
            self.assertEqual(notification.previous, text[11])
            for path in batches.glob("batch_*.json"):
                payload = json.loads(path.read_text(encoding="utf-8"))
                for index, item in enumerate(payload["items"]):
                    item["target"] = "TARGET_" + str(index)
                save(path, payload)
            result = apply_batches(batches, translated)
            self.assertEqual(result["applied_occurrences"], 7)
            actual = json.loads((translated / "events.json").read_text(encoding="utf-8"))
            for key in ("engine", "module", "metadata", "debug"):
                self.assertEqual(json.dumps(actual[key], ensure_ascii=False).encode("utf-8"),
                                 json.dumps(structured[key], ensure_ascii=False).encode("utf-8"))
            for index in (3, 4):
                self.assertEqual(actual["scenes"][0]["lines"][index], structured["scenes"][0]["lines"][index])
            self.assertTrue(actual["scenes"][0]["lines"][2]["text"].startswith("@putselect TARGET_"))
            self.assertEqual((translated / "unsupported.json").read_bytes(), original[Path("unsupported.json")])
            actual_script = json.loads((translated / script_path).read_text(encoding="utf-8"))
            for index in (0, 2, 3, 4):
                self.assertEqual(actual_script["files"][0]["lines"][index], script["files"][0]["lines"][index])
            for relative, content in original.items():
                self.assertEqual((source / relative).read_bytes(), content)

    def test_unsupported_display_role_is_pending_not_non_display(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            save(source / "unknown.json", {"labels": [row(chr(0x3042))]})
            manifest = prepare_batches(source, root / "batches")
            primary, independent = write_inventory_pair(
                source, root / "batches", root / "primary.json", root / "independent.json")
            self.assertEqual(manifest["item_count"], 0)
            for inventory in (primary, independent):
                self.assertEqual(read_segments(inventory), [])
                coverage = json.loads(inventory.read_text(encoding="utf-8"))["coverage"]
                self.assertFalse(coverage["declared_complete"])
                self.assertEqual(coverage["display_scope"]["unclassified_count"], 1)
                self.assertEqual(coverage["display_scope"]["control_only_count"], 0)


if __name__ == "__main__":
    unittest.main()
