from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "profiles"
    / "sdk-unity-2022.3"
    / "tools"
    / "unity_textasset.py"
)
SPEC = importlib.util.spec_from_file_location("fixture_unity_2022_3_textasset", MODULE_PATH)
assert SPEC and SPEC.loader
PROFILE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROFILE
SPEC.loader.exec_module(PROFILE)


class FakeTextAssetData:
    def __init__(self, name: str, script: str) -> None:
        self.m_Name = name
        self.m_Script = script

    def save(self) -> None:
        return None


class FakeObject:
    def __init__(self, path_id: int, type_name: str, data: FakeTextAssetData | None = None) -> None:
        self.path_id = path_id
        self.type = types.SimpleNamespace(name=type_name)
        self._data = data

    def read(self) -> FakeTextAssetData:
        assert self._data is not None
        return self._data

    def get_raw_data(self) -> bytes:
        if self._data is None:
            return b"SYNTHETIC_NON_TARGET_OBJECT"
        payload = self._data.m_Script
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        return self._data.m_Name.encode("utf-8") + b"\0" + bytes(payload)


class FakeEnvironment:
    def __init__(self, source_path: Path, tables: dict[str, str], saved: dict[Path, dict[str, str]]) -> None:
        self.source_path = source_path
        self.saved = saved
        self.objects = [
            FakeObject(index + 1, "TextAsset", FakeTextAssetData(name, text))
            for index, (name, text) in enumerate(tables.items())
        ]
        self.objects.append(FakeObject(100, "Texture2D"))

    def save(self, *, pack: str, out_path: str) -> None:
        assert pack == "original"
        destination = Path(out_path) / self.source_path.name
        destination.write_bytes(b"SYNTHETIC_UNITYFS_OUTPUT")
        self.saved[destination.resolve()] = {
            obj.read().m_Name: str(obj.read().m_Script)
            for obj in self.objects
            if obj.type.name == "TextAsset"
        }


class Unity2022TextAssetProfileTests(unittest.TestCase):
    def test_synthetic_extract_apply_pack_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            player = root / "fixture-player"
            data_root = player / "Fixture_Data"
            bundle = data_root / "StreamingAssets" / "Bundles" / "fixture.bundle"
            (data_root / "Managed").mkdir(parents=True)
            bundle.parent.mkdir(parents=True)
            (data_root / "globalgamemanagers").write_bytes(b"fixture 2022.3.62f1 fixture")
            bundle.write_bytes(b"SYNTHETIC_UNITYFS_INPUT")
            tables = {
                "reference-table": "[Reference]\n1=SOURCE_ONE\n2=SOURCE_{0}\n",
                "en": "[Fixture]\nOne=SOURCE_ONE\nTwo=SOURCE_{0}\n",
                "ko": "[Fixture]\nOne=OLD_ONE\nTwo=OLD_{0}\n",
            }
            saved: dict[Path, dict[str, str]] = {}

            def load_environment(path: Path) -> FakeEnvironment:
                resolved = Path(path).resolve()
                return FakeEnvironment(resolved, saved.get(resolved, tables), saved)

            inventory = root / "inventory"
            with patch.object(PROFILE, "_load_environment", side_effect=load_environment):
                extracted = PROFILE.extract_inventory(player, inventory)
                self.assertEqual(extracted["segments"], 2)
                segments = json.loads((inventory / "segments.json").read_text(encoding="utf-8"))
                reviewed = [
                    {
                        "segment_id": segment["id"],
                        "source": segment["source"],
                        "text": "TARGET_ONE" if index == 0 else "TARGET_{0}",
                        "status": "reviewed",
                    }
                    for index, segment in enumerate(segments)
                ]
                reviewed_path = root / "reviewed.json"
                reviewed_path.write_text(json.dumps(reviewed), encoding="utf-8")
                summary = json.loads(
                    (inventory / "extraction-summary.json").read_text(encoding="utf-8")
                )
                request = {
                    "schema_version": 1,
                    "game_root": str(player),
                    "segments_path": str(inventory / "segments.json"),
                    "reviewed_path": str(reviewed_path),
                    "source_bundle_relative": summary["source_bundle_relative"],
                    "source_bundle_sha256": summary["source_bundle_sha256"],
                    "report_path": str(root / "validation.json"),
                }
                request_path = root / "request.json"
                request_path.write_text(json.dumps(request), encoding="utf-8")
                output = root / "payload"
                with patch.dict(
                    os.environ,
                    {"GAME_L10N_WORKDIR": str(root), "GAME_L10N_STAGE": "pack"},
                    clear=False,
                ):
                    packed = PROFILE.build_payload(request_path, output)

            self.assertEqual(packed["status"], "valid")
            self.assertTrue((output / summary["source_bundle_relative"]).is_file())
            report = json.loads((root / "validation.json").read_text(encoding="utf-8"))
            self.assertTrue(report["source_immutability"]["identical"])
            self.assertEqual(report["integrity"]["non_target_object_byte_mismatches"], 0)

    def test_rejects_other_unity_series(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            player = Path(directory) / "fixture-player"
            data_root = player / "Fixture_Data"
            (data_root / "Managed").mkdir(parents=True)
            (data_root / "globalgamemanagers").write_bytes(b"fixture 2021.3.0f1 fixture")
            with self.assertRaisesRegex(ValueError, "UNITY_VERSION_NOT_2022_3"):
                PROFILE._require_supported_player(player)


if __name__ == "__main__":
    unittest.main()
