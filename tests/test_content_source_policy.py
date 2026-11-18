from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from game_l10n.content_source_policy import (
    PROVENANCE_SCHEMA,
    VALIDATION_SESSION_CONTRACT_SCHEMA,
    content_source_policy,
    create_content_source_grant,
    validate_output_provenance,
)
from game_l10n.models import Segment
from game_l10n.native_pass import prepare_native_tasks
from game_l10n.native_followup import (
    CANDIDATE_METADATA_KEY,
    _candidate_segment,
    prepare_native_followup,
)
from game_l10n.native_review import _bound_result
from game_l10n.native_translation_retry import start_native_translation_retries
from game_l10n.store import stable_hash, stable_hash_sequence


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


class ContentSourcePolicyTests(unittest.TestCase):
    def test_fresh_translation_task_is_default_deny_and_payload_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / "source.jsonl"
            segment = Segment("synthetic-a", "SYNTHETIC SOURCE")
            _write_jsonl(source, [asdict(segment)])

            prepared = prepare_native_tasks(
                work,
                source,
                [segment],
                work / "tasks",
                route_binding={"provider": "native-luna", "attempt": 1},
            )
            manifest = json.loads(Path(prepared["manifest"]).read_text(encoding="utf-8"))
            task = json.loads((work / "tasks" / "task-0001.jsonl").read_text(encoding="utf-8"))

            self.assertEqual(manifest["content_source_policy"]["effect"], "deny_external_content")
            self.assertFalse(manifest["content_source_policy"]["content_network_access"])
            self.assertEqual(task["content_source_policy"], manifest["content_source_policy"])

    def test_explicit_user_grant_is_exactly_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            grant_path, grant = create_content_source_grant(
                work,
                authority="explicit_user",
                scope_id="synthetic-session",
                task_kinds=["translation"],
                routes=["native-luna"],
                external_sources=["https://example.invalid/exact-source"],
                user_confirmed=True,
            )
            policy = content_source_policy(
                work,
                task_kind="translation",
                route="native-luna",
                scope_id="synthetic-session",
                grant_path=grant_path,
            )

            self.assertEqual(policy["authorization"], "explicit_user")
            self.assertEqual(policy["grant_id"], grant["grant_id"])
            self.assertTrue(policy["content_network_access"])
            authorized_provenance = {
                "schema": PROVENANCE_SCHEMA,
                "mode": "direct_model",
                "model": "native-luna",
                "route": "native-luna",
                "policy_digest": policy["policy_digest"],
                "external_sources_used": [
                    "https://example.invalid/exact-source"
                ],
            }
            self.assertEqual(
                validate_output_provenance(
                    authorized_provenance,
                    policy=policy,
                    model="native-luna",
                    route="native-luna",
                ),
                authorized_provenance,
            )
            with self.assertRaisesRegex(
                ValueError, "E_CONTENT_SOURCE_GRANT_SCOPE_MISMATCH"
            ):
                content_source_policy(
                    work,
                    task_kind="review",
                    route="native-luna",
                    scope_id="synthetic-session",
                    grant_path=grant_path,
                )

    def test_native_resume_inherits_exact_bound_grant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / "source.jsonl"
            segment = Segment("synthetic-a", "SYNTHETIC SOURCE")
            _write_jsonl(source, [asdict(segment)])
            grant_path, _ = create_content_source_grant(
                work,
                authority="explicit_user",
                scope_id="synthetic-native-session",
                task_kinds=["translation"],
                routes=["native-pass:translation"],
                external_sources=["https://example.invalid/exact-source"],
                user_confirmed=True,
            )
            first = prepare_native_tasks(
                work,
                source,
                [segment],
                work / "tasks",
                route_binding={"provider": "native-luna", "attempt": 1},
                content_source_grant=grant_path,
                content_source_scope="synthetic-native-session",
            )
            resumed = prepare_native_tasks(
                work,
                source,
                [segment],
                work / "tasks",
                route_binding={"provider": "native-luna", "attempt": 1},
            )

            self.assertTrue(resumed["cached"])
            self.assertEqual(
                resumed["content_source_policy"],
                first["content_source_policy"],
            )

    def test_validation_session_grant_requires_matching_immutable_contract(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            contract = work / ".game-l10n" / "validation-session.json"
            contract.parent.mkdir(parents=True)
            contract.write_text(
                json.dumps(
                    {
                        "schema": VALIDATION_SESSION_CONTRACT_SCHEMA,
                        "contract_id": "synthetic-validation",
                        "scope_id": "synthetic-scope",
                        "task_kinds": ["triage"],
                        "routes": ["native-sol"],
                        "external_sources": ["https://example.invalid/review-source"],
                        "external_content_access": True,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            grant_path, _ = create_content_source_grant(
                work,
                authority="validation_session",
                scope_id="synthetic-scope",
                task_kinds=["triage"],
                routes=["native-sol"],
                external_sources=["https://example.invalid/review-source"],
                validation_contract=contract,
            )
            policy = content_source_policy(
                work,
                task_kind="triage",
                route="native-sol",
                scope_id="synthetic-scope",
                grant_path=grant_path,
            )
            self.assertEqual(policy["authorization"], "validation_session")

    def test_unauthorized_external_provenance_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            policy = content_source_policy(
                work, task_kind="translation", route="native-luna"
            )
            provenance = {
                "schema": PROVENANCE_SCHEMA,
                "mode": "direct_model",
                "model": "native-luna",
                "route": "native-luna",
                "policy_digest": policy["policy_digest"],
                "external_sources_used": ["https://example.invalid/undeclared"],
            }
            with self.assertRaisesRegex(
                ValueError, "E_EXTERNAL_CONTENT_PROVENANCE_DENIED"
            ):
                validate_output_provenance(
                    provenance,
                    policy=policy,
                    model="native-luna",
                    route="native-luna",
                )

    def test_missing_semantic_provenance_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = content_source_policy(
                Path(directory),
                task_kind="translation",
                route="native-pass:translation",
            )
            with self.assertRaisesRegex(
                ValueError, "E_OUTPUT_PROVENANCE_INVALID"
            ):
                validate_output_provenance(
                    None,
                    policy=policy,
                    model="provider/exact-model",
                    route="native-luna",
                )

    def test_validation_authority_cannot_grant_candidate_production(self) -> None:
        for task_kind in ("translation", "rewrite"):
            with self.subTest(task_kind=task_kind), tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                contract = work / ".game-l10n" / f"{task_kind}.json"
                contract.parent.mkdir(parents=True)
                contract.write_text(
                    json.dumps(
                        {
                            "schema": VALIDATION_SESSION_CONTRACT_SCHEMA,
                            "contract_id": f"synthetic-{task_kind}",
                            "scope_id": "synthetic-validation",
                            "task_kinds": [task_kind],
                            "routes": ["native-pass:translation"],
                            "external_sources": [
                                "https://example.invalid/source"
                            ],
                            "external_content_access": True,
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    ValueError, "E_CONTENT_SOURCE_GRANT_SCOPE_MISMATCH"
                ):
                    create_content_source_grant(
                        work,
                        authority="validation_session",
                        scope_id="synthetic-validation",
                        task_kinds=[task_kind],
                        routes=["native-pass:translation"],
                        external_sources=[
                            "https://example.invalid/source"
                        ],
                        validation_contract=contract,
                    )

    def test_model_and_route_alias_are_bound_separately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = content_source_policy(
                Path(directory),
                task_kind="translation",
                route="native-pass:translation",
            )
            provenance = {
                "schema": PROVENANCE_SCHEMA,
                "mode": "direct_model",
                "model": "provider/exact-model",
                "route": "native-luna",
                "policy_digest": policy["policy_digest"],
                "external_sources_used": [],
            }
            self.assertEqual(
                validate_output_provenance(
                    provenance,
                    policy=policy,
                    model="provider/exact-model",
                    route="native-luna",
                ),
                provenance,
            )
            with self.assertRaisesRegex(
                ValueError, "E_OUTPUT_PROVENANCE_INVALID"
            ):
                validate_output_provenance(
                    provenance,
                    policy=policy,
                    model="native-luna",
                    route="native-luna",
                )

    def test_triage_and_strong_review_grants_are_not_interchangeable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            for task_kind in ("triage", "strong_review"):
                grant_path, _ = create_content_source_grant(
                    work,
                    authority="explicit_user",
                    scope_id=f"scope-{task_kind}",
                    task_kinds=[task_kind],
                    routes=["native-pass:review"],
                    external_sources=[
                        f"https://example.invalid/{task_kind}"
                    ],
                    user_confirmed=True,
                )
                policy = content_source_policy(
                    work,
                    task_kind=task_kind,
                    route="native-pass:review",
                    scope_id=f"scope-{task_kind}",
                    grant_path=grant_path,
                )
                self.assertEqual(policy["task_kind"], task_kind)
                other = (
                    "strong_review"
                    if task_kind == "triage"
                    else "triage"
                )
                with self.assertRaisesRegex(
                    ValueError, "E_CONTENT_SOURCE_GRANT_SCOPE_MISMATCH"
                ):
                    content_source_policy(
                        work,
                        task_kind=other,
                        route="native-pass:review",
                        scope_id=f"scope-{task_kind}",
                        grant_path=grant_path,
                    )

    def test_content_provenance_survives_candidate_and_review_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = content_source_policy(
                Path(directory),
                task_kind="translation",
                route="native-pass:translation",
            )
            provenance = {
                "schema": PROVENANCE_SCHEMA,
                "mode": "direct_model",
                "model": "provider/exact-model",
                "route": "native-luna",
                "policy_digest": policy["policy_digest"],
                "external_sources_used": [],
            }
            segment = Segment("synthetic-provenance", "SOURCE")
            candidate = _candidate_segment(
                segment,
                {
                    "target_id": segment.id,
                    "status": "success",
                    "text": "TARGET",
                    "provider": "native-luna",
                    "candidate_generation": 1,
                    "candidate_digest": stable_hash("TARGET"),
                    "content_provenance": provenance,
                },
            )
            self.assertEqual(
                candidate.metadata[CANDIDATE_METADATA_KEY][
                    "content_provenance"
                ],
                provenance,
            )
            self.assertEqual(
                _bound_result(candidate).metadata["content_provenance"],
                provenance,
            )

    def test_unrelated_primary_manifest_cannot_authorize_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source_a = work / "source-a.jsonl"
            source_b = work / "source-b.jsonl"
            result_a = work / "result-a.jsonl"
            segment_a = Segment("synthetic-a", "SOURCE A")
            segment_b = Segment("synthetic-a", "SOURCE B")
            _write_jsonl(source_a, [asdict(segment_a)])
            _write_jsonl(source_b, [asdict(segment_b)])
            _write_jsonl(
                result_a,
                [{
                    "target_id": segment_a.id,
                    "status": "error",
                    "text": "",
                    "provider": "native-luna",
                    "issues": ["synthetic_failure"],
                }],
            )
            prepared = prepare_native_tasks(
                work,
                source_b,
                [segment_b],
                work / "tasks-b",
                route_binding={
                    "provider": "native-luna",
                    "model": "provider/exact-model",
                    "attempt": 1,
                },
            )
            manifest_path = Path(prepared["manifest"])
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            lineage = {
                "manifest": manifest_path.relative_to(work).as_posix(),
                "manifest_contract_fingerprint": manifest[
                    "contract_fingerprint"
                ],
                "source_fingerprint": manifest["source_fingerprint"],
                "execution_namespace": manifest["execution_namespace"],
                "content_source_policy_digest": manifest[
                    "content_source_policy"
                ]["policy_digest"],
                "route_binding": manifest["route_binding"],
            }
            with self.assertRaisesRegex(
                ValueError, "E_CONTENT_SOURCE_POLICY_IMMUTABLE"
            ):
                start_native_translation_retries(
                    work,
                    source_a,
                    [segment_a],
                    result_a,
                    work / "retry",
                    content_policy=manifest["content_source_policy"],
                    primary_lineage=lineage,
                )

    def test_fallback_bundles_inherit_denied_and_granted_retry_policy(self) -> None:
        for granted in (False, True):
            with self.subTest(granted=granted), tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                source = work / "source.jsonl"
                results = work / "results.jsonl"
                segment = Segment("synthetic-fallback", "SOURCE")
                _write_jsonl(source, [asdict(segment)])
                _write_jsonl(
                    results,
                    [{
                        "target_id": segment.id,
                        "status": "error",
                        "text": "",
                        "provider": "native-luna",
                        "issues": ["synthetic_failure"],
                    }],
                )
                grant_path = None
                scope = ""
                if granted:
                    scope = "synthetic-fallback-scope"
                    grant_path, _ = create_content_source_grant(
                        work,
                        authority="explicit_user",
                        scope_id=scope,
                        task_kinds=["translation"],
                        routes=["native-pass:translation"],
                        external_sources=[
                            "https://example.invalid/fallback-source"
                        ],
                        user_confirmed=True,
                    )
                policy = content_source_policy(
                    work,
                    task_kind="translation",
                    route="native-pass:translation",
                    scope_id=scope,
                    grant_path=grant_path,
                )
                source_fingerprint = stable_hash_sequence([asdict(segment)])
                lineage = {
                    "source_fingerprint": source_fingerprint,
                    "content_source_policy_digest": policy["policy_digest"],
                }
                prepared = prepare_native_followup(
                    work,
                    source,
                    [segment],
                    results,
                    work / "followup",
                    exchange_prefix="synthetic",
                    content_source_policy=policy,
                    retry_lineage=lineage,
                    retry_route_plan=[
                        {
                            "provider": "native-luna",
                            "model": "provider/exact-model",
                            "max_retries": 0,
                        }
                    ],
                )
                bundle = work / prepared["fallback_bundles"][0]
                exchange = json.loads(
                    (bundle / "exchange-manifest.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(exchange["content_source_policy"], policy)
                self.assertEqual(exchange["retry_lineage"], lineage)

    def test_retry_state_cannot_be_mutated_to_widen_permission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / "source.jsonl"
            results = work / "results.jsonl"
            state_dir = work / "retry"
            segment = Segment("synthetic-a", "SYNTHETIC SOURCE")
            _write_jsonl(source, [asdict(segment)])
            _write_jsonl(
                results,
                [
                    {
                        "target_id": segment.id,
                        "status": "error",
                        "text": "",
                        "provider": "native-luna",
                        "issues": ["synthetic_failure"],
                    }
                ],
            )
            start_native_translation_retries(
                work, source, [segment], results, state_dir
            )
            state_path = state_dir / "retry-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            grant_path, _ = create_content_source_grant(
                work,
                authority="explicit_user",
                scope_id="later-scope",
                task_kinds=["translation"],
                routes=["native-luna"],
                external_sources=["https://example.invalid/later"],
                user_confirmed=True,
            )
            state["content_source_policy"] = content_source_policy(
                work,
                task_kind="translation",
                route="native-luna",
                scope_id="later-scope",
                grant_path=grant_path,
            )
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError, "E_CONTENT_SOURCE_POLICY_IMMUTABLE"
            ):
                start_native_translation_retries(
                    work, source, [segment], results, state_dir
                )

    def test_inventory_local_inspection_remains_nonsemantic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = content_source_policy(
                Path(directory),
                task_kind="primary_inventory",
                route="local-inspection",
            )
            self.assertTrue(policy["inventory_local_inspection_allowed"])
            self.assertFalse(policy["semantic_content_generation"])
            self.assertFalse(policy["direct_model_provenance_required"])
            self.assertFalse(policy["content_network_access"])


if __name__ == "__main__":
    unittest.main()
