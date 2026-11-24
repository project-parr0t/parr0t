from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


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
            "required_tags": ["synthetic-tag-a", "synthetic-tag-b"],
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
            self.assertEqual(plan.required_tags, ("synthetic-tag-a", "synthetic-tag-b"))

    def test_manifest_rejects_duplicate_or_unknown_locales(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(MODULE.MetadataError, "duplicates"):
                MODULE.load_plan(self._manifest(root, locales=["english", "english"]))
            with self.assertRaisesRegex(MODULE.MetadataError, "unsupported"):
                MODULE.load_plan(self._manifest(root, locales=["english", "not-a-steam-language"]))


    def test_manifest_rejects_invalid_or_duplicate_tags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(MODULE.MetadataError, "duplicates"):
                MODULE.load_plan(
                    self._manifest(root, required_tags=["Synthetic-Tag", "synthetic-tag"])
                )
            with self.assertRaisesRegex(MODULE.MetadataError, "invalid"):
                MODULE.load_plan(self._manifest(root, required_tags=[" prefixed-tag"]))
            with self.assertRaisesRegex(MODULE.MetadataError, "255"):
                MODULE.load_plan(self._manifest(root, required_tags=["x" * 256]))

    def test_required_tags_are_added_without_removing_existing_tags(self) -> None:
        self.assertEqual(
            MODULE.merge_required_tags(
                ("existing-tag", "synthetic-tag"),
                ("Synthetic-Tag", "required-tag"),
            ),
            ("existing-tag", "synthetic-tag", "required-tag"),
        )

    def test_detail_tag_decoder_uses_documented_structure_fields(self) -> None:
        raw = bytearray(MODULE.UGC_DETAILS_BUFFER_BYTES)
        tags_offset = MODULE.SteamUGCDetailsPrefix.tags.offset
        encoded = b"existing-tag,required-tag\0"
        raw[tags_offset : tags_offset + len(encoded)] = encoded
        self.assertEqual(
            MODULE.decode_details_tags(bytes(raw)),
            ("existing-tag", "required-tag"),
        )
        raw[MODULE.SteamUGCDetailsPrefix.tags_truncated.offset] = 1
        with self.assertRaisesRegex(MODULE.MetadataError, "truncated"):
            MODULE.decode_details_tags(bytes(raw))

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

    def test_full_release_reads_back_english_and_korean_only(self) -> None:
        selected = tuple(sorted(MODULE.SUPPORTED_LOCALES))
        plan = MODULE.MetadataPlan(1, 2, 3, "Test title", "Test description", selected)
        with mock.patch.object(MODULE, "SteamMetadataClient") as factory, redirect_stdout(io.StringIO()):
            client = factory.return_value.__enter__.return_value
            MODULE.synchronize(plan, Path("unused.dll"), selected)
            self.assertEqual(
                client.method_calls,
                [*(mock.call.update(plan, locale) for locale in selected),
                 mock.call.verify(plan, "english"), mock.call.verify(plan, "koreana")],
            )

    def test_rate_limit_stops_updates_and_readback(self) -> None:
        selected = ("english", "koreana", "japanese")
        plan = MODULE.MetadataPlan(1, 2, 3, "Test title", "Test description", selected)
        with mock.patch.object(MODULE, "SteamMetadataClient") as factory, redirect_stdout(io.StringIO()):
            client = factory.return_value.__enter__.return_value
            client.update.side_effect = [None, MODULE.MetadataError("Steam returned EResult 84")]
            with self.assertRaisesRegex(MODULE.MetadataError, "EResult 84"):
                MODULE.synchronize(plan, Path("unused.dll"), selected)
            self.assertEqual(client.method_calls, [
                mock.call.update(plan, "english"), mock.call.update(plan, "koreana"),
            ])

    def test_native_sends_wait_after_completion_and_stop_on_offline(self) -> None:
        client = object.__new__(MODULE.SteamMetadataClient)
        client._dll = mock.Mock()
        client._ugc = client._user = client._utils = 1
        client._next_request_at = 0.0
        client._dll.SteamAPI_ISteamUser_BLoggedOn.side_effect = [True, True, False]
        client._dll.SteamAPI_ISteamUtils_IsAPICallCompleted.return_value = True
        times = []
        now = 10.0

        def advance(delay: float) -> None:
            nonlocal now
            now += delay

        def send(_ugc: int) -> int:
            times.append(now)
            return len(times)

        with mock.patch.object(MODULE.time, "monotonic", side_effect=lambda: now), mock.patch.object(
            MODULE.time, "sleep", side_effect=advance
        ):
            first = client._send(send)
            advance(0.4)
            client._wait_for_completion(first)
            client._send(send)
            with self.assertRaisesRegex(MODULE.MetadataError, "offline"):
                client._send(send)
        self.assertEqual(times, [10.0, 11.4])

if __name__ == "__main__":
    unittest.main()
