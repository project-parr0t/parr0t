"""Run the CLI from this exact package without interpolating code or cwd paths."""
from pathlib import Path
import runpy
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
runpy.run_module("game_l10n.cli", run_name="__main__")
