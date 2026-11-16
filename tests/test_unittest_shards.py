from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_unittest_shards.py"


class UnittestShardRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")

    def run_runner(self, workers: int = 4) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PARR0T_SHARD_FIXTURE"] = str(self.root)
        return subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--root",
                str(self.root),
                "--workers",
                str(workers),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=60,
            check=False,
        )

    def test_modules_run_in_isolated_bounded_processes(self) -> None:
        for module_index in range(4):
            methods = "\n".join(
                f"    def test_{test_index}(self):\n"
                "        pathlib.Path("
                "os.environ['PARR0T_SHARD_FIXTURE'], "
                f"'pid-{module_index}-{test_index}'"
                ").write_text(str(os.getpid()))"
                for test_index in range(2)
            )
            (self.root / "tests" / f"test_parallel_{module_index}.py").write_text(
                "import os\nimport pathlib\nimport unittest\n"
                f"class ParallelFixture{module_index}(unittest.TestCase):\n"
                + methods
                + "\n",
                encoding="utf-8",
            )

        result = self.run_runner()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ran 8 tests", result.stderr)
        self.assertIn("OK", result.stderr)
        module_process_ids = [
            {
                path.read_text(encoding="utf-8")
                for path in self.root.glob(f"pid-{module_index}-*")
            }
            for module_index in range(4)
        ]
        self.assertTrue(all(len(process_ids) == 1 for process_ids in module_process_ids))
        process_ids = set().union(*module_process_ids)
        self.assertGreater(len(process_ids), 1)
        self.assertLessEqual(len(process_ids), 4)

    def test_failure_and_skip_survive_the_aggregate_summary(self) -> None:
        (self.root / "tests" / "test_results.py").write_text(
            "import unittest\n"
            "class ResultFixture(unittest.TestCase):\n"
            "    @unittest.skip('fixture')\n"
            "    def test_skip(self): pass\n"
            "    def test_failure(self): self.fail('fixture')\n",
            encoding="utf-8",
        )

        result = self.run_runner(workers=2)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Ran 2 tests", result.stderr)
        self.assertIn("FAILED (skipped=1)", result.stderr)


if __name__ == "__main__":
    unittest.main()
