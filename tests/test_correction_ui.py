from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from game_l10n.correction_ui import (
    BATCH_SCHEMA,
    PROPOSALS_SCHEMA,
    TASK_SCHEMA,
    CorrectionUIError,
    CorrectionWorkspace,
    correction_server,
)
from game_l10n.formats import write_results
from game_l10n.models import AttemptRecord, Segment, TranslationResult
from game_l10n.store import stable_hash
from game_l10n.corrections import CorrectionError
from game_l10n.release_checkpoint import LOCAL_INTEGRITY_SCHEMA, validate_release_checkpoint


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


def _prepare(
    root: Path, dependencies: dict[str, list[str]] | None = None, *,
    agent_task_size: int = 32, synthetic_count: int = 0,
) -> CorrectionWorkspace:
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
    if synthetic_count:
        segments = [
            Segment(f"synthetic-{index:04d}", f"SYNTHETIC_SOURCE_{index}")
            for index in range(synthetic_count)
        ]
        results = [
            _result(segment, f"SYNTHETIC_TARGET_{index}")
            for index, segment in enumerate(segments)
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
        agent_task_size=agent_task_size,
    )

def _proposals(task: dict) -> dict:
    selected = set(task["selected_ids"])
    return {
        "schema": PROPOSALS_SCHEMA, "task_id": task["task_id"],
        "proposals": [{
            "id": record["id"],
            "decision": "replace" if record["id"] in selected else "unchanged",
            "target": (
                "SYNTHETIC_CORRECTION_" + record["id"]
                if record["id"] in selected else record["current_target"]
            ),
            "source_sha256": record["source_sha256"],
            "context_sha256": record["context_sha256"],
        } for record in task["records"]],
    }




class CorrectionUITests(unittest.TestCase):
    def test_agent_task_size_requires_positive_integer_without_an_upper_ceiling(self) -> None:
        for invalid in (0, -1, True, 1.5, "32"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(CorrectionUIError, "positive integer"):
                    _prepare(Path(directory), agent_task_size=invalid)
        with tempfile.TemporaryDirectory() as directory:
            workspace = _prepare(Path(directory), agent_task_size=1_000_000)
            workspace.stage_flag(
                target_id="line-hello", route="agent", flag_kind="dialogue", note="Synthetic request",
            )
            self.assertEqual(workspace.export_agent_task()["task_size"], 1_000_000)

    def test_existing_single_task_state_migrates_without_rebinding_its_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = _prepare(root)
            workspace.stage_flag(
                target_id="line-hello", route="agent", flag_kind="dialogue", note="Synthetic request",
            )
            summary = workspace.export_agent_task()
            output = Path(summary["output"])
            manifest = json.loads(output.read_text(encoding="utf-8"))
            task_bytes = Path(manifest["tasks"][0]["path"]).read_bytes()
            task = json.loads(task_bytes)
            output.write_bytes(task_bytes)
            workspace._set_metadata("last_agent_task", {
                "task_id": task["task_id"],
                "bindings": {record["id"]: {
                    "source_sha256": record["source_sha256"],
                    "context_sha256": record["context_sha256"],
                } for record in task["records"]},
            })
            migrated = CorrectionWorkspace.prepare(
                state_path=workspace.path, source_path=root / "inventory.jsonl",
                checkpoint_path=root / "checkpoint.json", base_results_path=root / "released.jsonl",
                request_output_path=root / "request.json", corrected_output_path=root / "corrected.jsonl",
                agent_task_path=output, expected_inventory_fingerprint=_digest("inventory"),
                agent_task_size=64,
            )
            self.assertEqual(output.read_bytes(), task_bytes)
            self.assertIsNone(migrated._metadata_value("last_agent_task"))
            self.assertEqual(migrated.import_agent_proposals(_proposals(task))["pending_count"], 0)

    def test_exported_task_cannot_overwrite_changed_user_interpretation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = _prepare(root)
            workspace.stage_flag(
                target_id="line-hello", route="agent", flag_kind="dialogue", note="First synthetic request",
            )
            summary = workspace.export_agent_task()
            manifest = json.loads(Path(summary["output"]).read_text(encoding="utf-8"))
            task = json.loads(Path(manifest["tasks"][0]["path"]).read_text(encoding="utf-8"))
            workspace.stage_flag(
                target_id="line-hello", route="agent", flag_kind="dialogue", note="Changed synthetic request",
            )
            with self.assertRaisesRegex(CorrectionUIError, "interpretation changed"):
                workspace.import_agent_proposals(_proposals(task))
            self.assertEqual(workspace.get_record("line-hello")["annotation"]["status"], "flagged")

    def test_more_than_thirty_two_direct_user_edits_remain_exact_and_unbounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = _prepare(root, synthetic_count=40)
            for index in range(40):
                workspace.stage_flag(
                    target_id=f"synthetic-{index:04d}", route="direct",
                    flag_kind="other", note="Synthetic exact user correction",
                    replacement=f"SYNTHETIC_CORRECTION_{index}",
                )
            summary = workspace.apply_approved()
            rows = [json.loads(line) for line in (root / "corrected.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(summary["changed_count"], 40)
            self.assertEqual(summary["model_calls"], 0)
            self.assertEqual(len(rows), 40)
            self.assertEqual([row["text"] for row in rows], [
                f"SYNTHETIC_CORRECTION_{index}" for index in range(40)
            ])

    def test_large_dependency_closure_is_batched_and_partial_decisions_stay_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependencies = {f"synthetic-{index:04d}": ["synthetic-0000"] for index in range(1, 40)}
            workspace = _prepare(root, dependencies, agent_task_size=9, synthetic_count=40)
            workspace.stage_flag(
                target_id="synthetic-0000", route="agent", flag_kind="logic",
                note="Synthetic root correction",
            )
            summary = workspace.export_agent_task()
            manifest = json.loads(Path(summary["output"]).read_text(encoding="utf-8"))
            tasks = [json.loads(Path(item["path"]).read_text(encoding="utf-8")) for item in manifest["tasks"]]
            self.assertEqual(summary["closure_count"], 40)
            self.assertEqual(summary["task_count"], 5)
            self.assertEqual([len(task["records"]) for task in tasks], [9, 9, 9, 9, 4])
            self.assertEqual(
                [record["id"] for task in tasks for record in task["records"]],
                [f"synthetic-{index:04d}" for index in range(40)],
            )
            partial = _proposals(tasks[0])
            partial["proposals"] = partial["proposals"][:1]
            imported = workspace.import_agent_proposals(partial)
            self.assertEqual(imported["status"], "pending")
            self.assertEqual(imported["pending_count"], 39)
            workspace.approve_proposal("synthetic-0000")
            with self.assertRaisesRegex(CorrectionError, "targeted review"):
                workspace.apply_approved()
            for task in reversed(tasks):
                imported = workspace.import_agent_proposals(_proposals(task))
            self.assertEqual(imported["pending_count"], 0)
            self.assertEqual(workspace.get_record("synthetic-0000")["annotation"]["status"], "approved")
            for index in range(1, 40):
                target_id = f"synthetic-{index:04d}"
                approved = workspace.approve_proposal(
                    target_id, workspace.get_record(target_id)["target"],
                )
                self.assertEqual(approved["annotation"]["status"], "reviewed_unchanged")
            applied = workspace.apply_approved()
            self.assertEqual(applied["changed_count"], 1)
            self.assertEqual(applied["dependent_review_count"], 39)

    def test_positive_task_size_above_thirty_two_controls_export_and_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = _prepare(root, agent_task_size=64, synthetic_count=40)
            for index in range(40):
                workspace.stage_flag(
                    target_id=f"synthetic-{index:04d}", route="agent",
                    flag_kind="other", note="Synthetic proposal request",
                )
            summary = workspace.export_agent_task()
            manifest = json.loads(Path(summary["output"]).read_text(encoding="utf-8"))
            self.assertEqual(summary["task_size"], 64)
            self.assertEqual(summary["task_count"], 1)
            task = json.loads(Path(manifest["tasks"][0]["path"]).read_text(encoding="utf-8"))
            imported = workspace.import_agent_proposals(_proposals(task))
            self.assertEqual(imported["proposal_count"], 40)
            self.assertEqual(imported["pending_count"], 0)
            self.assertEqual(workspace.get_record("synthetic-0039")["annotation"]["status"], "proposal_ready")

    def test_retuning_task_size_preserves_exported_scopes_and_old_proposals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependencies = {f"synthetic-{index:04d}": ["synthetic-0000"] for index in range(1, 5)}
            workspace = _prepare(root, dependencies, agent_task_size=2, synthetic_count=5)
            workspace.stage_flag(target_id="synthetic-0000", route="agent", flag_kind="logic", note="Synthetic request")
            original = workspace.export_agent_task()
            manifest = json.loads(Path(original["output"]).read_text(encoding="utf-8"))
            old_files = {Path(task["path"]): Path(task["path"]).read_bytes() for task in manifest["tasks"]}
            resized = CorrectionWorkspace.prepare(
                state_path=workspace.path, source_path=root / "inventory.jsonl",
                checkpoint_path=root / "checkpoint.json", base_results_path=root / "released.jsonl",
                request_output_path=root / "request.json", corrected_output_path=root / "corrected.jsonl",
                agent_task_path=root / "agent-task.json", expected_inventory_fingerprint=_digest("inventory"),
                agent_task_size=64,
            )
            self.assertEqual(resized.status()["agent_task_size"], 64)
            updated = resized.export_agent_task()
            self.assertEqual(updated["task_count"], 1)
            for path, content in old_files.items():
                self.assertEqual(path.read_bytes(), content)
            old_task = json.loads(next(iter(old_files.values())))
            imported = resized.import_agent_proposals(_proposals(old_task))
            self.assertEqual(imported["proposal_count"], len(old_task["records"]))

    def test_prepare_rejects_valid_local_integrity_without_dependency_or_state_invention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segment = Segment("synthetic-one", "SYNTHETIC SOURCE")
            result = _result(segment, "SYNTHETIC TARGET")
            fingerprint = _digest("synthetic-inventory")
            full = _checkpoint([segment], [result], fingerprint)
            local = {
                "schema": LOCAL_INTEGRITY_SCHEMA,
                "patch_id": full["patch_id"], "release_id": full["release_id"],
                "ownership": {"owner": "Synthetic owner", "license": "MIT"},
                "source_inventory": full["source_inventory"],
                "language": {"source": "en", "target": "ko"},
                "entries": [{
                    key: full["entries"][0][key] for key in (
                        "id", "source_sha256", "target", "target_sha256", "status",
                    )
                } | {"integrity_evidence_sha256": _digest("synthetic-integrity-evidence")}],
            }
            self.assertEqual(validate_release_checkpoint(local), local)
            checkpoint = root / "local-integrity.json"
            checkpoint.write_text(json.dumps(local), encoding="utf-8")
            source = root / "inventory.jsonl"
            source.write_text(json.dumps(asdict(segment)) + "\n", encoding="utf-8")
            state = root / "corrections.sqlite3"
            request = root / "request.json"
            corrected = root / "corrected.jsonl"
            agent_task = root / "agent-task.json"
            with self.assertRaisesRegex(CorrectionUIError, "require a full release checkpoint"):
                CorrectionWorkspace.prepare(
                    state_path=state, source_path=source, checkpoint_path=checkpoint,
                    base_results_path=None, request_output_path=request,
                    corrected_output_path=corrected, agent_task_path=agent_task,
                    expected_inventory_fingerprint=fingerprint,
                )
            self.assertFalse(state.exists())
            self.assertFalse(state.with_name(f".{state.name}.tmp").exists())
            self.assertFalse(request.exists())
            self.assertFalse(corrected.exists())
            self.assertFalse(agent_task.exists())

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
            manifest = json.loads((root / "agent-task.json").read_text(encoding="utf-8"))
            task = json.loads(Path(manifest["tasks"][0]["path"]).read_text(encoding="utf-8"))
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

        self.assertEqual(summary["schema"], BATCH_SCHEMA)
        self.assertEqual(task["schema"], TASK_SCHEMA)
        self.assertEqual(summary["task_count"], 1)
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
