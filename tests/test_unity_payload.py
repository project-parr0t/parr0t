from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.coverage import completion_attestation, save_inventory_attestation
from game_l10n.formats import read_segments, write_results
from game_l10n.language_policy import language_binding
from game_l10n.models import TranslationResult
from game_l10n.unity.payload import compile_runtime_payload


def write_verified_inventory(root: Path, rows: list[dict]) -> Path:
    primary = root / "fixture-segments.json"
    independent = root / "fixture-segments-independent.json"
    base = {
        "schema": "parr0t.source-inventory.v1",
        "coverage": {"declared_complete": True, "exclusions": []},
        "segments": rows,
    }
    primary.write_text(
        json.dumps(
            {
                **base,
                "method": "unity-serialized-resource-walk",
                "implementation_fingerprint": "fixture-primary-v1",
            }
        ),
        encoding="utf-8",
    )
    independent.write_text(
        json.dumps(
            {
                **base,
                "method": "unity-type-index-audit",
                "implementation_fingerprint": "fixture-independent-v1",
            }
        ),
        encoding="utf-8",
    )
    save_inventory_attestation(root, primary, independent)
    return primary


class UnityPayloadTests(unittest.TestCase):
    def _incomplete_fixture(
        self, root: Path, suffix: str = ".json", *,
        accepted_status: str = "accepted_unreviewed", preserved_text: str = "SOURCE_TEXT",
    ) -> tuple[Path, Path, dict]:
        config = {
            "project": {"source_language": "en", "target_language": "ko"},
            "failure_queue": {"allow_incomplete_artifact": True},
        }
        (root / "game-l10n.json").write_text(json.dumps(config), encoding="utf-8")
        segments_path = write_verified_inventory(
            root,
            [
                {
                    "id": "fixture-preserved",
                    "source": "SOURCE_TEXT",
                    "metadata": {"unity_kind": "literal", "blocking_on_failure": True},
                },
                {
                    "id": "fixture-accepted",
                    "source": "SOURCE_MENU",
                    "metadata": {
                        "unity_kind": "string_table", "collection": "fixture_collection",
                        "entry_id": "1001",
                    },
                },
            ],
        )
        rows = [
            TranslationResult(
                segment_id="fixture-preserved", source="SOURCE_TEXT",
                text=preserved_text, status="source_preserved",
            ),
            TranslationResult(
                segment_id="fixture-accepted", source="SOURCE_MENU",
                text=chr(0xAC00) * 4, status=accepted_status,
            ),
        ]
        result_path = root / ("fixture-incomplete" + suffix)
        write_results(result_path, rows)
        inventory = json.loads(
            (root / ".game-l10n/inventory-attestation.json").read_text(encoding="utf-8")
        )
        segments = read_segments(segments_path)
        completion = completion_attestation(
            segments, rows, inventory_segments=segments, inventory=inventory,
            runtime={}, review_required=True, target_language="ko",
            language_binding=language_binding(root, config), allow_incomplete_artifact=True,
        )
        completion["result_artifact"] = {
            "path": str(result_path.resolve()),
            "size": result_path.stat().st_size,
            "sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
        }
        (root / ".game-l10n/completion-attestation.json").write_text(
            json.dumps(completion), encoding="utf-8",
        )
        (root / ".game-l10n/failure-queue.json").write_text(
            json.dumps({
                "schema": "parr0t.failure-queue.v1",
                "inventory_fingerprint": inventory["primary_fingerprint"],
                "blocking_count": 1,
                "entries": [{"target_id": "fixture-preserved", "blocking": True}],
            }),
            encoding="utf-8",
        )
        return segments_path, result_path, completion

    def test_explicit_incomplete_compiles_bound_artifact_without_relabeling_results(self) -> None:
        for suffix in (".json", ".jsonl", ".csv"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                segments, results, completion = self._incomplete_fixture(root, suffix)
                source_bytes = segments.read_bytes()
                result_bytes = results.read_bytes()
                output = root / "fixture-runtime.json"

                summary = compile_runtime_payload(
                    segments, results, output, work_dir=root, locale="ko",
                )
                payload = json.loads(output.read_text(encoding="utf-8"))

                self.assertEqual(payload["literal_strings"], {"SOURCE_TEXT": "SOURCE_TEXT"})
                self.assertEqual(
                    payload["string_tables"]["fixture_collection"]["1001"],
                    {"text": chr(0xAC00) * 4, "is_smart": False},
                )
                self.assertEqual(summary["segments"], 2)
                self.assertEqual(summary["source_preserved"], 1)
                self.assertEqual(summary["artifact_assurance"], "explicit_incomplete")
                self.assertEqual(payload["artifact_assurance"], "explicit_incomplete")
                self.assertFalse(completion["target_completion"])
                self.assertEqual(completion["semantic_review"]["reviewed_count"], 0)
                self.assertEqual(results.read_bytes(), result_bytes)
                self.assertEqual(segments.read_bytes(), source_bytes)

    def test_incomplete_requires_consent_and_exact_unchanged_compiler_input(self) -> None:
        for mutation in ("consent", "authorization", "alternate", "changed"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                segments, results, completion = self._incomplete_fixture(root)
                if mutation == "consent":
                    config_path = root / "game-l10n.json"
                    config = json.loads(config_path.read_text(encoding="utf-8"))
                    config["failure_queue"]["allow_incomplete_artifact"] = False
                    config_path.write_text(json.dumps(config), encoding="utf-8")
                elif mutation == "authorization":
                    completion["incomplete_artifact_authorized"] = False
                    (root / ".game-l10n/completion-attestation.json").write_text(
                        json.dumps(completion), encoding="utf-8",
                    )
                elif mutation == "alternate":
                    alternate = root / "fixture-alternate.json"
                    alternate.write_bytes(results.read_bytes())
                    results = alternate
                else:
                    results.write_bytes(results.read_bytes() + b"\n")
                output = root / "fixture-runtime.json"

                with self.assertRaisesRegex(ValueError, "blocking"):
                    compile_runtime_payload(
                        segments, results, output, work_dir=root, locale="ko",
                    )
                self.assertFalse(output.exists())

    def test_explicit_incomplete_does_not_bypass_unreviewed_or_source_preservation_safety(self) -> None:
        for mutation in ("translated", "failed", "changed_source_preserved"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                segments, results, _ = self._incomplete_fixture(
                    root,
                    accepted_status=mutation if mutation != "changed_source_preserved" else "accepted_unreviewed",
                    preserved_text="CHANGED_SOURCE" if mutation == "changed_source_preserved" else "SOURCE_TEXT",
                )
                output = root / "fixture-runtime.json"

                with self.assertRaises(ValueError):
                    compile_runtime_payload(
                        segments, results, output, work_dir=root, locale="ko",
                    )
                self.assertFalse(output.exists())

    def test_compiles_reviewed_targets_by_engine_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reviewed = root / "fixture-reviewed.json"
            output = root / "fixture-runtime.json"
            segments = write_verified_inventory(
                root,
                [
                    {
                        "id": "fixture-table-1",
                        "source": "SOURCE_MENU",
                        "metadata": {
                            "unity_kind": "string_table",
                            "collection": "fixture_collection",
                            "entry_id": "1001",
                            "is_smart": True,
                            "ui_path": "FixtureRoot/FixtureLabel",
                        },
                    },
                    {
                        "id": "fixture-asset-1",
                        "source": "SOURCE_SCENE",
                        "metadata": {"unity_kind": "text_asset", "asset_key": "fixture_asset"},
                    },
                    {
                        "id": "fixture-literal-1",
                        "source": "SOURCE_LABEL",
                        "metadata": {"unity_kind": "literal"},
                    },
                ],
            )
            reviewed.write_text(
                json.dumps(
                    {
                        "results": [
                            {"id": "fixture-table-1", "source": "SOURCE_MENU", "text": "TARGET_MENU", "status": "reviewed"},
                            {"id": "fixture-asset-1", "source": "SOURCE_SCENE", "text": "TARGET_SCENE", "status": "reviewed"},
                            {"id": "fixture-literal-1", "source": "SOURCE_LABEL", "text": "SOURCE_LABEL", "status": "source_preserved"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            summary = compile_runtime_payload(
                segments, reviewed, output, work_dir=root, locale="ko-KR"
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(summary["segments"], 3)
        self.assertEqual(summary["source_preserved"], 1)
        self.assertEqual(
            payload["string_tables"]["fixture_collection"]["1001"],
            {"text": "TARGET_MENU", "is_smart": True},
        )
        self.assertEqual(payload["text_assets"], {"fixture_asset": "TARGET_SCENE"})
        self.assertEqual(payload["literal_strings"], {"SOURCE_LABEL": "SOURCE_LABEL"})
        self.assertEqual(payload["ui_paths"], ["FixtureRoot/FixtureLabel"])
        self.assertTrue(payload["inventory_fingerprint"])

    def test_refuses_unreviewed_results_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reviewed = root / "fixture-reviewed.json"
            segments = write_verified_inventory(
                root,
                [
                    {
                        "id": "fixture-1",
                        "source": "SOURCE_TEXT",
                        "metadata": {"unity_kind": "literal"},
                    }
                ],
            )
            reviewed.write_text(
                json.dumps(
                    {
                        "results": [
                            {"id": "fixture-1", "source": "SOURCE_TEXT", "text": "TARGET_TEXT", "status": "translated"}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                compile_runtime_payload(
                    segments,
                    reviewed,
                    root / "fixture-runtime.json",
                    work_dir=root,
                    locale="ko",
                )

    def test_refuses_to_overwrite_source_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = write_verified_inventory(
                root,
                [{"id": "fixture-1", "source": "SOURCE_TEXT", "metadata": {"unity_kind": "literal"}}],
            )
            reviewed = root / "fixture-reviewed.json"
            reviewed.write_text('{"results": []}', encoding="utf-8")
            with self.assertRaises(ValueError):
                compile_runtime_payload(
                    segments, reviewed, segments, work_dir=root, locale="ko"
                )

    def test_blocking_failure_queue_holds_runtime_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = write_verified_inventory(
                root,
                [{"id": "fixture-1", "source": "SOURCE_TEXT", "metadata": {"unity_kind": "literal"}}],
            )
            reviewed = root / "fixture-reviewed.json"
            reviewed.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "id": "fixture-1",
                                "source": "SOURCE_TEXT",
                                "text": "SOURCE_TEXT",
                                "status": "source_preserved",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            attestation = json.loads(
                (root / ".game-l10n" / "inventory-attestation.json").read_text(
                    encoding="utf-8"
                )
            )
            (root / ".game-l10n" / "failure-queue.json").write_text(
                json.dumps(
                    {
                        "schema": "parr0t.failure-queue.v1",
                        "inventory_fingerprint": attestation["primary_fingerprint"],
                        "blocking_count": 1,
                        "entries": [{"target_id": "fixture-1", "blocking": True}],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "blocking"):
                compile_runtime_payload(
                    segments,
                    reviewed,
                    root / "fixture-runtime.json",
                    work_dir=root,
                    locale="ko",
                )


if __name__ == "__main__":
    unittest.main()
