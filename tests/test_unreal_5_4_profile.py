from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROFILE_ROOT = (
    Path(__file__).resolve().parents[1] / "profiles" / "sdk-unreal-5.4"
)
TOOLS_ROOT = PROFILE_ROOT / "tools"
SPEC = importlib.util.spec_from_file_location(
    "fixture_unreal_5_4_profile", TOOLS_ROOT / "unreal_profile.py"
)
assert SPEC and SPEC.loader
PROFILE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROFILE
with patch.dict(os.environ, {"GAME_L10N_WORKDIR": str(PROFILE_ROOT)}, clear=False):
    SPEC.loader.exec_module(PROFILE)


class Unreal54ProfileTests(unittest.TestCase):
    def test_synthetic_locres_extract_and_apply(self) -> None:
        PROFILE.add_vendor_path()
        from pylocres import Entry, LocresFile, Namespace

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture.locres"
            segments = root / "segments.jsonl"
            reviewed = root / "reviewed.jsonl"

            locres = LocresFile()
            namespace = Namespace("FIXTURE_NAMESPACE")
            namespace.add(Entry("FIXTURE_KEY", "SOURCE_FIXTURE", 1))
            locres.add(namespace)
            locres.write(source)

            count, metadata = PROFILE.extract_segments(source, segments)
            self.assertEqual(count, 1)
            self.assertEqual(metadata["namespace_count"], 1)
            target_id = PROFILE.stable_id("FIXTURE_NAMESPACE", "FIXTURE_KEY")
            reviewed.write_text(
                json.dumps(
                    {"id": target_id, "status": "reviewed", "text": "TARGET_FIXTURE"}
                )
                + "\n",
                encoding="utf-8",
            )
            loaded = PROFILE.load_locres(source)
            self.assertEqual(PROFILE.apply_reviewed_results(loaded, reviewed), (1, 0))
            self.assertEqual(list(list(loaded)[0])[0].translation, "TARGET_FIXTURE")

    def test_engine_line_and_path_guards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Fixture-Win64-Shipping.exe"
            executable.write_bytes(b"SYNTHETIC++UE5+Release-5.4SYNTHETIC")
            self.assertEqual(PROFILE.detect_engine_version(executable), "5.4")
        self.assertEqual(
            PROFILE.require_safe_relative("Fixture/Content/File.uasset", "fixture"),
            "Fixture/Content/File.uasset",
        )
        with self.assertRaisesRegex(RuntimeError, "fixture_invalid"):
            PROFILE.require_safe_relative("../escape", "fixture")

    def test_profile_selftest_and_manifest_are_provenance_free(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(TOOLS_ROOT / "selftest.py")],
            cwd=PROFILE_ROOT,
            env={**os.environ, "GAME_L10N_WORKDIR": str(PROFILE_ROOT)},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        manifest = json.loads((PROFILE_ROOT / "profile.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["profile"], "sdk-unreal-5.4")
        self.assertFalse(manifest["contains_game_data"])
        joined = json.dumps(manifest).casefold()
        for forbidden in ("game title", "steamapps", "workspace", "dialogue"):
            self.assertNotIn(forbidden, joined)


if __name__ == "__main__":
    unittest.main()
