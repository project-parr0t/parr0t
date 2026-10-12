from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from game_l10n.wolf.tools import decompile_runtime_scripts, pack_pro_archives


class WolfToolTests(unittest.TestCase):
    def test_decompile_and_pro_pack_use_argument_arrays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "fake_tool.py"
            fake.write_text(
                """from pathlib import Path
import shutil
import sys

if sys.argv[1] == 'decompile':
    source = Path(sys.argv[2])
    output = Path(sys.argv[sys.argv.index('-o') + 1])
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output)
elif sys.argv[1] == 'pack':
    source = Path(sys.argv[2])
    output = Path(sys.argv[3])
    key = Path(sys.argv[4])
    output.write_bytes(key.read_bytes() + ('|' + source.name).encode())
else:
    raise SystemExit(3)
""",
                encoding="utf-8",
            )
            data = root / "Data"
            (data / "BasicData").mkdir(parents=True)
            (data / "MapData").mkdir()
            (data / "BasicData" / "CommonEvent.dat").write_text("commonEvent 1 {}\n", encoding="utf-8")
            (data / "MapData" / "synthetic-fixture.mps").write_text("event 1 {}\n", encoding="utf-8")
            command = [sys.executable, str(fake)]
            scripts = decompile_runtime_scripts(command, data, root / "audit")
            self.assertEqual(set(scripts), {"CommonEvent.dat", "synthetic-fixture.mps"})
            self.assertTrue(all(path.is_file() for path in scripts.values()))

            built = root / "built"
            for name in ("BasicData", "MapData", "Text_Script"):
                (built / name).mkdir(parents=True)
            key = root / "Game.wolf"
            key.write_bytes(b"pro-key-source")
            outputs = pack_pro_archives(command, built, key, root / "output")
            self.assertEqual([path.name for path in outputs], ["BasicData.wolf", "MapData.wolf", "Text_Script.wolf"])
            self.assertTrue(all(path.read_bytes().startswith(b"pro-key-source") for path in outputs))


if __name__ == "__main__":
    unittest.main()
