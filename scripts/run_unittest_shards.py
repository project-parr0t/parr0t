"""Run one unittest suite in isolated parallel shards with one trusted summary."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import tempfile
import time
import unittest


MAX_WORKERS = 8


def configure_imports(root: Path) -> None:
    for path in (root, root / "src"):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)


def iter_test_ids(suite: unittest.TestSuite) -> list[str]:
    result: list[str] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            result.extend(iter_test_ids(item))
        else:
            result.append(item.id())
    return result


def discover_test_ids(root: Path) -> list[str]:
    configure_imports(root)
    suite = unittest.defaultTestLoader.discover(
        str(root / "tests"), pattern="test*.py", top_level_dir=str(root)
    )
    return iter_test_ids(suite)


def unittest_summary(stderr: str) -> tuple[int, bool, int]:
    counts = re.findall(r"^Ran (\d+) tests? in", stderr, re.MULTILINE)
    summaries = re.findall(r"^(?:OK|FAILED)(?: \(([^)]*)\))?$", stderr, re.MULTILINE)
    if not summaries:
        return (int(counts[-1]) if counts else 0), False, 0
    skipped = re.search(r"(?:^|, )skipped=(\d+)(?:, |$)", summaries[-1])
    return int(counts[-1]) if counts else 0, True, int(skipped[1]) if skipped else 0


def run_shard(root: Path, shard_file: Path) -> int:
    configure_imports(root)
    test_ids = json.loads(shard_file.read_text(encoding="utf-8"))
    if not isinstance(test_ids, list) or not all(isinstance(item, str) for item in test_ids):
        raise ValueError("E_UNITTEST_SHARD_INVALID")
    suite = unittest.defaultTestLoader.loadTestsFromNames(test_ids)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def run_parallel(root: Path, workers: int) -> int:
    test_ids = discover_test_ids(root)
    if not test_ids or len(set(test_ids)) != len(test_ids):
        print("Ran 0 tests in 0.000s", file=sys.stderr)
        print("FAILED", file=sys.stderr)
        return 1

    modules: dict[str, list[str]] = {}
    for test_id in test_ids:
        module = test_id.rsplit(".", 2)[0]
        modules.setdefault(module, []).append(test_id)
    # Large modules start first. Each module keeps unittest's discovered order
    # and a fresh process, while independent modules overlap within the bound.
    tasks = sorted(modules.items(), key=lambda item: (-len(item[1]), item[0]))
    worker_count = min(workers, len(tasks))

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="parr0t-unittest-shards-") as directory:
        shard_root = Path(directory)
        active: set[subprocess.Popen[str]] = set()
        active_lock = threading.Lock()
        stopping = threading.Event()

        def run_module(
            index: int, module: str, shard: list[str]
        ) -> tuple[int, str, subprocess.Popen[str], Path, Path]:
            shard_file = shard_root / f"shard-{index}.json"
            stdout_path = shard_root / f"shard-{index}.stdout"
            stderr_path = shard_root / f"shard-{index}.stderr"
            shard_file.write_text(json.dumps(shard), encoding="utf-8")
            stdout = stdout_path.open("w", encoding="utf-8")
            stderr = stderr_path.open("w", encoding="utf-8")
            process: subprocess.Popen[str] | None = None
            try:
                with active_lock:
                    if stopping.is_set():
                        raise concurrent.futures.CancelledError
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "--root",
                            str(root),
                            "--shard-file",
                            str(shard_file),
                        ],
                        cwd=root,
                        env=os.environ.copy(),
                        stdout=stdout,
                        stderr=stderr,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        shell=False,
                    )
                    active.add(process)
                process.wait()
                return index, module, process, stdout_path, stderr_path
            finally:
                stdout.close()
                stderr.close()
                if process is not None:
                    with active_lock:
                        active.discard(process)

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
        futures = [
            executor.submit(run_module, index, module, shard)
            for index, (module, shard) in enumerate(tasks)
        ]
        try:
            records = [future.result() for future in futures]
        except BaseException:
            stopping.set()
            for future in futures:
                future.cancel()
            with active_lock:
                running = list(active)
            for process in running:
                if process.poll() is None:
                    process.terminate()
            for process in running:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)
        records.sort(key=lambda record: record[0])

        total = skipped = 0
        complete = True
        return_codes: list[int] = []
        for index, module, process, stdout_path, stderr_path in records:
            stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
            stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
            if stdout_text:
                print(f"[module {index + 1}/{len(tasks)}: {module}]", file=sys.stdout)
                print(
                    stdout_text,
                    end="" if stdout_text.endswith("\n") else "\n",
                    file=sys.stdout,
                )
            if stderr_text:
                print(f"[module {index + 1}/{len(tasks)}: {module}]", file=sys.stderr)
                print(
                    stderr_text,
                    end="" if stderr_text.endswith("\n") else "\n",
                    file=sys.stderr,
                )
            count, summarized, shard_skipped = unittest_summary(stderr_text)
            total += count
            skipped += shard_skipped
            complete = complete and summarized
            return_codes.append(process.returncode)

    success = complete and total == len(test_ids) and all(
        code == 0 for code in return_codes
    )
    print("-" * 70, file=sys.stderr)
    print(f"Ran {total} tests in {time.monotonic() - started:.3f}s", file=sys.stderr)
    if success:
        print(f"OK (skipped={skipped})" if skipped else "OK", file=sys.stderr)
    else:
        details = f" (skipped={skipped})" if skipped else ""
        print(f"FAILED{details}", file=sys.stderr)
    return 0 if success else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--workers",
        type=int,
        choices=range(1, MAX_WORKERS + 1),
        default=max(1, min(MAX_WORKERS, os.cpu_count() or 1)),
    )
    parser.add_argument("--shard-file", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.shard_file:
        return run_shard(root, args.shard_file.resolve())
    return run_parallel(root, args.workers)


if __name__ == "__main__":
    raise SystemExit(main())
