import os
from pathlib import Path
import subprocess
import sys
import unittest

class StandaloneEntryTests(unittest.TestCase):
    def test_help_works_without_modifying_core_cli(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src") + os.pathsep + environment.get("PYTHONPATH", "")
        result = subprocess.run([sys.executable, "-m", "game_l10n.rpg_maker_mv", "--help"], env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)
