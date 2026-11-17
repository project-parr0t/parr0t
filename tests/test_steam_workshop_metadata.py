from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_steam_workshop_metadata.py"
SPEC = importlib.util.spec_from_file_location("sync_steam_workshop_metadata", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class SteamWorkshopMetadataTests(unittest.TestCase):
    def _manifest(self, root: Path, **updates: object) -> Path:
        (root / "description.txt").write_text("Canonical description", encoding="utf-8")
        payload: dict[str, object] = {
            "schema": MODULE.SCHEMA,
            "app_id": 4348910,
            "published_file_id": 3838981739,
            "owner_steam_id": 76561198119795156,
            "title": "Probably Translated",
            "description_file": "description.txt",
            "locales": ["english", "koreana"],
        }
        payload.update(updates)
        path = root / "metadata.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_manifest_binds_exact_item_and_all_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = MODULE.load_plan(self._manifest(Path(directory)))
            self.assertEqual(plan.app_id, 4348910)
            self.assertEqual(plan.item_id, 3838981739)
            self.assertEqual(plan.owner_id, 76561198119795156)
            self.assertEqual(plan.title, "Probably Translated")
            self.assertEqual(plan.description, "Canonical description")
            self.assertEqual(plan.locales, ("english", "koreana"))

    def test_manifest_rejects_duplicate_or_unknown_locales(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(MODULE.MetadataError, "duplicates"):
                MODULE.load_plan(self._manifest(root, locales=["english", "english"]))
            with self.assertRaisesRegex(MODULE.MetadataError, "unsupported"):
                MODULE.load_plan(self._manifest(root, locales=["english", "not-a-steam-language"]))

    def test_description_cannot_escape_manifest_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / "outside-description.txt"
            outside.write_text("outside", encoding="utf-8")
            try:
                with self.assertRaisesRegex(MODULE.MetadataError, "escapes"):
                    MODULE.load_plan(self._manifest(root, description_file="../outside-description.txt"))
            finally:
                outside.unlink(missing_ok=True)

    def test_apply_requires_exact_item_approval(self) -> None:
        plan = MODULE.MetadataPlan(
            4348910,
            3838981739,
            76561198119795156,
            "Title",
            "Description",
            ("english",),
        )
        with self.assertRaisesRegex(MODULE.MetadataError, "exactly match"):
            MODULE.require_item_approval(plan, 123)
        MODULE.require_item_approval(plan, 3838981739)

    def test_apply_requires_exact_steam_api_hash_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "steam_api64.dll"
            library.write_bytes(b"valve-steam-api-fixture")
            digest = MODULE._sha256(library)
            with self.assertRaisesRegex(MODULE.MetadataError, "exactly match"):
                MODULE.require_steam_api_approval(library, "0" * 64)
            self.assertEqual(MODULE.require_steam_api_approval(library, digest.upper()), digest)

if __name__ == "__main__":
    unittest.main()
