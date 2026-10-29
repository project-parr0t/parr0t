from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from game_l10n.formats import read_segments
from game_l10n.models import Segment
from game_l10n.native_translation_retry import (
    STATE_NAME,
    advance_native_translation_retries,
    require_pre_fallback_retry_state,
    start_native_translation_retries,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _result(target_id: str, status: str, text: str = "", provider: str = "native-luna") -> dict:
    return {
        "target_id": target_id,
        "status": status,
        "text": text,
        "provider": provider,
        "issues": [],
    }


class NativeTranslationRetryTests(unittest.TestCase):
    def test_resume_reads_the_original_json_source_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text(
                json.dumps(
                    {
                        "segments": [
                            {"id": "a", "source": "A"},
                            {"id": "b", "source": "B"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            segments = read_segments(source)
            first = root / "first.jsonl"
            _write_jsonl(
                first,
                [_result("a", "success", "TA"), _result("b", "error")],
            )
            state_dir = root / "retry"
            start_native_translation_retries(
                root, source, segments, first, state_dir
            )
            retry = root / "retry.jsonl"
            _write_jsonl(retry, [_result("b", "success", "TB")])

            state = advance_native_translation_retries(root, state_dir, retry)

        self.assertEqual(state["next_action"], "complete")
        self.assertEqual(state["pending_count"], 0)

    def test_failed_only_luna_then_sol_attempts_gate_external_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [Segment("a", "A"), Segment("b", "B"), Segment("c", "C")]
            source = root / "source.jsonl"
            _write_jsonl(source, [asdict(segment) for segment in segments])
            first = root / "first.jsonl"
            _write_jsonl(
                first,
                [
                    _result("a", "success", "TA"),
                    _result("b", "refused"),
                    _result("c", "error"),
                ],
            )
            state_dir = root / "retry"
            state = start_native_translation_retries(
                root, source, segments, first, state_dir
            )
            self.assertEqual(state["next_provider"], "native-luna")
            self.assertEqual(state["next_attempt"], 2)
            self.assertEqual(state["pending_count"], 2)

            for attempt in range(2, 6):
                result_path = root / f"luna-{attempt}.jsonl"
                _write_jsonl(
                    result_path,
                    [_result("b", "refused"), _result("c", "error")],
                )
                state = advance_native_translation_retries(
                    root, state_dir, result_path
                )
            fifth = root / "luna-6.jsonl"
            _write_jsonl(
                fifth,
                [_result("b", "success", "TB"), _result("c", "refused")],
            )
            state = advance_native_translation_retries(root, state_dir, fifth)
            self.assertEqual(state["next_provider"], "native-sol")
            self.assertEqual(state["next_attempt"], 1)
            self.assertEqual(state["pending_count"], 1)

            for attempt in range(1, 5):
                result_path = root / f"sol-{attempt}.jsonl"
                _write_jsonl(
                    result_path,
                    [_result("c", "refused", provider="native-sol")],
                )
                state = advance_native_translation_retries(
                    root, state_dir, result_path
                )
            self.assertEqual(state["next_action"], "external_fallback")
            self.assertEqual(state["pending_count"], 1)
            final_path = root / state["pre_fallback_results"]
            final = [json.loads(line) for line in final_path.read_text().splitlines()]
            self.assertEqual([row["target_id"] for row in final], ["a", "b", "c"])
            self.assertEqual([row["status"] for row in final], ["success", "success", "error"])
            checked = require_pre_fallback_retry_state(
                root, state_dir / STATE_NAME, source, final_path
            )
            self.assertEqual(checked["next_action"], "external_fallback")


if __name__ == "__main__":
    unittest.main()
