from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from game_l10n.correction_ui import (
    PROPOSALS_SCHEMA,
    TASK_SCHEMA,
    CorrectionUIError,
    CorrectionWorkspace,
    correction_server,
)
from game_l10n.formats import write_results
from game_l10n.models import AttemptRecord, Segment, TranslationResult
from game_l10n.store import stable_hash


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _result(segment: Segment, target: str) -> TranslationResult:
    return TranslationResult(
        segment_id=segment.id,
        source=segment.source,
        text=target,
        status="reviewed",
        attempts=[AttemptRecord("review", "fixture", "approved", candidate_generation=1)],
        candidate_generation=1,
        candidate_digest=stable_hash(target),
        review_outcome="approved",
        acceptance_authority="reviewer",
    )


def _checkpoint(
    segments: list[Segment],
    results: list[TranslationResult],
    fingerprint: str,
    dependencies: dict[str, list[str]] | None = None,
) -> dict:
    dependencies = dependencies or {}
    by_id = {result.segment_id: result for result in results}
    return {
        "schema": "parr0t.release-checkpoint/v1",
        "patch_id": "fixture-localization",
        "release_id": "r1",
        "sdk": {
            "branch": "sdk-fixture-1.0",
            "commit": "a" * 40,
            "adapter_fingerprint": _digest("adapter"),
        },
        "game_builds": [{"id": "fixture", "binding_fingerprint": _digest("build")}],
        "source_inventory": {
            "method": "fixture",
            "fingerprint": fingerprint,
            "target_count": len(segments),
        },
        "context": {
            "fingerprint": _digest("context"),
            "review_policy_fingerprint": _digest("review-policy"),
        },
        "entries": [
            {
                "id": segment.id,
                "source_sha256": _digest(segment.source),
                "target": by_id[segment.id].text,
                "target_sha256": _digest(by_id[segment.id].text),
                "location_fingerprint": _digest("location:" + segment.id),
                "dependency_fingerprint": _digest("dependency:" + segment.id),
                "review_fingerprint": _digest("review:" + segment.id),
                "status": "reviewed",
                "dependencies": dependencies.get(segment.id, []),
            }
            for segment in segments
        ],
        "locale_fallback": {
            "status": "unsupported",
            "mode": "none",
            "fallback_locale": "",
            "placeholder": "",
            "verified": False,
            "reason_code": "fixture_no_safe_fallback",
        },
    }


def _prepare(root: Path, dependencies: dict[str, list[str]] | None = None) -> CorrectionWorkspace:
    segments = [
        Segment("line-hello", "Hello, Darwin.", speaker="Butler", scene="hall"),
        Segment("ui-cargo", "Open cargo bay", scene="menu"),
        Segment("quest-road", "TAKE ME HOME, COUNTLESS ROAD", scene="quest"),
    ]
    results = [
        _result(segments[0], "Good day, Darwin."),
        _result(segments[1], "Open Cargo"),
        _result(segments[2], "Take me home, countless roads"),
    ]
    source = root / "inventory.jsonl"
    base = root / "released.jsonl"
    checkpoint = root / "checkpoint.json"
    fingerprint = _digest("inventory")
    source.write_text(
        "\n".join(json.dumps(asdict(segment), ensure_ascii=False) for segment in segments) + "\n",
        encoding="utf-8",
    )
    write_results(base, results)
    checkpoint.write_text(
        json.dumps(_checkpoint(segments, results, fingerprint, dependencies), ensure_ascii=False),
        encoding="utf-8",
    )
    return CorrectionWorkspace.prepare(
        state_path=root / "corrections.sqlite3",
        source_path=source,
        checkpoint_path=checkpoint,
        base_results_path=base,
        request_output_path=root / "request.json",
        corrected_output_path=root / "corrected.jsonl",
        agent_task_path=root / "agent-task.json",
        expected_inventory_fingerprint=fingerprint,
    )


class CorrectionUITests(unittest.TestCase):
    def test_exact_character_and_local_vector_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = _prepare(Path(directory))
            exact = workspace.search("Hello, Darwin.", mode="exact")
            character = workspace.search("Helo Darwin", mode="character")
            vector = workspace.search("countless roads take me home", mode="vector")

        self.assertEqual([row["id"] for row in exact], ["line-hello"])
        self.assertEqual(character[0]["id"], "line-hello")
        self.assertEqual(vector[0]["id"], "quest-road")
        self.assertGreater(vector[0]["score"], 0)

    def test_direct_edit_applies_only_approved_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = _prepare(root)
            workspace.stage_flag(
                target_id="quest-road",
                route="direct",
                flag_kind="reference",
                note="Preserve the song parody's cadence.",
                reference="Take Me Home, Country Roads",
                replacement="Take me home, O countless roads",
            )
            summary = workspace.apply_approved()
            rows = [json.loads(line) for line in (root / "corrected.jsonl").read_text(encoding="utf-8").splitlines()]
            request = json.loads((root / "request.json").read_text(encoding="utf-8"))

        self.assertEqual(summary["changed_count"], 1)
        self.assertEqual(summary["model_calls"], 0)
        self.assertEqual(request["changes"], [{"id": "quest-road", "target": "Take me home, O countless roads"}])
        self.assertEqual(rows[2]["text"], "Take me home, O countless roads")
        self.assertEqual(rows[0]["text"], "Good day, Darwin.")

    def test_agent_task_contains_only_flag_and_reverse_dependency_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = _prepare(root, dependencies={"ui-cargo": ["line-hello"]})
            workspace.stage_flag(
                target_id="line-hello",
                route="agent",
                flag_kind="dialogue",
                note="Make the reserved butler's formality explicit.",
                reference="Butler character voice",
            )
            summary = workspace.export_agent_task()
            task = json.loads((root / "agent-task.json").read_text(encoding="utf-8"))
            flagged = next(record for record in task["records"] if record["id"] == "line-hello")
            dependent = next(record for record in task["records"] if record["id"] == "ui-cargo")
            proposals = {
                "schema": PROPOSALS_SCHEMA,
                "task_id": task["task_id"],
                "proposals": [
                    {
                        "id": "line-hello",
                        "decision": "replace",
                        "target": "Good day, Mr. Darwin.",
                        "source_sha256": flagged["source_sha256"],
                        "context_sha256": flagged["context_sha256"],
                    },
                    {
                        "id": "ui-cargo",
                        "decision": "unchanged",
                        "target": dependent["current_target"],
                        "source_sha256": dependent["source_sha256"],
                        "context_sha256": dependent["context_sha256"],
                    },
                ],
            }
            imported = workspace.import_agent_proposals(proposals)
            workspace.approve_proposal("line-hello")
            workspace.approve_proposal("ui-cargo")
            applied = workspace.apply_approved()
            rows = [
                json.loads(line)
                for line in (root / "corrected.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(summary["schema"], TASK_SCHEMA)
        self.assertEqual(summary["selected_count"], 1)
        self.assertEqual(summary["closure_count"], 2)
        self.assertEqual({record["id"] for record in task["records"]}, {"line-hello", "ui-cargo"})
        self.assertEqual(imported["proposal_count"], 2)
        self.assertEqual(applied["changed_count"], 1)
        self.assertEqual(applied["dependent_review_count"], 1)
        self.assertEqual(rows[1]["text"], "Open Cargo")
        self.assertEqual(rows[1]["attempts"][-1]["stage"], "user-correction-dependency-review")

    def test_state_refuses_rebinding_to_changed_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _prepare(root)
            source = root / "inventory.jsonl"
            source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(CorrectionUIError, "different artifacts"):
                CorrectionWorkspace.prepare(
                    state_path=root / "corrections.sqlite3",
                    source_path=source,
                    checkpoint_path=root / "checkpoint.json",
                    base_results_path=root / "released.jsonl",
                    request_output_path=root / "request.json",
                    corrected_output_path=root / "corrected.jsonl",
                    agent_task_path=root / "agent-task.json",
                    expected_inventory_fingerprint=_digest("inventory"),
                )

    def test_http_server_binds_only_to_loopback_with_session_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = _prepare(Path(directory))
            server, token = correction_server(workspace, token="fixture-token")
            try:
                address, port = server.server_address[:2]
            finally:
                server.server_close()

        self.assertEqual(address, "127.0.0.1")
        self.assertGreater(port, 0)
        self.assertEqual(token, "fixture-token")


if __name__ == "__main__":
    unittest.main()
