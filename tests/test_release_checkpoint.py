from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.release_checkpoint import (
    CHECKPOINT_SCHEMA,
    LOCAL_INTEGRITY_SCHEMA,
    ReleaseCheckpointError,
    canonical_checkpoint_bytes,
    generate_local_release_integrity,
    validate_release_checkpoint,
)
from game_l10n.models import AttemptRecord, FailureCode, TranslationResult
from game_l10n.store import stable_hash


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _checkpoint() -> dict[str, object]:
    target = "TARGET_FIXTURE_TEXT"
    return {
        "schema": CHECKPOINT_SCHEMA,
        "patch_id": "fixture-localization",
        "release_id": "r1",
        "sdk": {
            "branch": "sdk-fixture-1.0",
            "commit": "a" * 40,
            "adapter_fingerprint": _digest("fixture-adapter"),
        },
        "game_builds": [
            {
                "id": "fixture-build-1",
                "binding_fingerprint": _digest("fixture-binding"),
            }
        ],
        "source_inventory": {
            "method": "fixture-primary",
            "fingerprint": _digest("fixture-inventory"),
            "target_count": 1,
        },
        "context": {
            "fingerprint": _digest("fixture-context"),
            "review_policy_fingerprint": _digest("fixture-review-policy"),
        },
        "entries": [
            {
                "id": "fixture-target-1",
                "source_sha256": _digest("SOURCE_FIXTURE_TEXT"),
                "target": target,
                "target_sha256": _digest(target),
                "location_fingerprint": _digest("fixture-location"),
                "dependency_fingerprint": _digest("fixture-dependency"),
                "review_fingerprint": _digest("fixture-review"),
                "status": "reviewed",
                "dependencies": [],
            }
        ],
        "locale_fallback": {
            "status": "unsupported",
            "mode": "none",
            "fallback_locale": "",
            "placeholder": "",
            "verified": False,
            "reason_code": "fixture_no_safe_runtime_fallback",
        },
    }


class ReleaseCheckpointTests(unittest.TestCase):
    def test_valid_checkpoint_is_canonicalized(self) -> None:
        payload = _checkpoint()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            validated, data = canonical_checkpoint_bytes(
                path,
                expected_patch_id="fixture-localization",
                expected_release_id="r1",
            )

        self.assertEqual(validated, payload)
        self.assertEqual(
            data,
            (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
                "utf-8"
            ),
        )

    def test_raw_source_text_field_is_rejected(self) -> None:
        payload = _checkpoint()
        payload["entries"][0]["source"] = "SOURCE_FIXTURE_TEXT"  # type: ignore[index]
        with self.assertRaises(ReleaseCheckpointError):
            validate_release_checkpoint(payload)

    def test_target_digest_must_match_released_target(self) -> None:
        payload = _checkpoint()
        payload["entries"][0]["target_sha256"] = _digest("DIFFERENT")  # type: ignore[index]
        with self.assertRaises(ReleaseCheckpointError):
            validate_release_checkpoint(payload)

    def test_source_preserved_must_match_source_digest(self) -> None:
        payload = _checkpoint()
        payload["entries"][0]["status"] = "source_preserved"  # type: ignore[index]
        with self.assertRaises(ReleaseCheckpointError):
            validate_release_checkpoint(payload)

    def test_user_accepted_unreviewed_target_remains_distinct(self) -> None:
        payload = _checkpoint()
        payload["entries"][0]["status"] = "accepted_unreviewed"  # type: ignore[index]
        self.assertEqual(validate_release_checkpoint(payload), payload)

    def test_implemented_fallback_must_be_verified(self) -> None:
        payload = _checkpoint()
        payload["locale_fallback"] = {
            "status": "implemented",
            "mode": "fallback_locale",
            "fallback_locale": "en",
            "placeholder": "",
            "verified": False,
            "reason_code": "",
        }
        with self.assertRaises(ReleaseCheckpointError):
            validate_release_checkpoint(payload)

        payload["locale_fallback"]["verified"] = True  # type: ignore[index]
        self.assertEqual(validate_release_checkpoint(payload), payload)


    def test_minimal_local_integrity_omits_future_update_metadata_not_identity(self) -> None:
        full = _checkpoint()
        entry = full["entries"][0]
        local = {
            "schema": LOCAL_INTEGRITY_SCHEMA,
            "patch_id": full["patch_id"], "release_id": full["release_id"],
            "ownership": {"owner": "Fixture owner", "license": "MIT"},
            "source_inventory": full["source_inventory"],
            "language": {"source": "en", "target": "ko"},
            "entries": [{
                key: entry[key] for key in ("id", "source_sha256", "target", "target_sha256", "status")
            } | {"integrity_evidence_sha256": _digest("placeholder-and-format-check")}],
        }
        self.assertEqual(validate_release_checkpoint(local), local)
        local["entries"][0]["status"] = "accepted_unreviewed"
        self.assertEqual(validate_release_checkpoint(local)["entries"][0]["status"], "accepted_unreviewed")
        local["entries"][0]["target"] = "Changed"
        with self.assertRaises(ReleaseCheckpointError):
            validate_release_checkpoint(local)
        local["entries"][0]["target"] = entry["target"]
        del local["ownership"]
        with self.assertRaises(ReleaseCheckpointError):
            validate_release_checkpoint(local)


    def _generation_fixture(self, root: Path) -> tuple[Path, TranslationResult, dict]:
        (root / "game-l10n.json").write_text(json.dumps({
            "project": {"source_language": "en", "target_language": "ko"},
            "coverage": {"require_independent_inventory": False},
        }), encoding="utf-8")
        inventory = root / "inventory.json"
        inventory.write_text(json.dumps({
            "schema": "parr0t.source-inventory.v1", "method": "synthetic-primary",
            "implementation_fingerprint": "synthetic-generator-v1",
            "coverage": {"declared_complete": True, "exclusions": []},
            "segments": [{"id": "one", "source": "Hello {name}"}],
        }), encoding="utf-8")
        row = TranslationResult(
            segment_id="one", source="Hello {name}", text="\uc548\ub155 {name}",
            status="translated", candidate_generation=1,
            candidate_digest=stable_hash("\uc548\ub155 {name}"),
        )
        arguments = dict(
            patch_id="synthetic-patch", release_id="r1", owner="Synthetic owner",
            license_name="MIT", source_language="en", target_language="ko",
            review_required=False,
        )
        return inventory, row, arguments

    def test_generator_produces_self_contained_packagable_local_integrity(self) -> None:
        from game_l10n.filesystem_package import build_filesystem_package

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory, row, arguments = self._generation_fixture(root)
            checkpoint, evidence = generate_local_release_integrity(root, inventory, [row], **arguments)
            self.assertEqual(checkpoint["entries"][0]["status"], "unreviewed")
            self.assertEqual(checkpoint["entries"][0]["source_sha256"], _digest(row.source))
            self.assertEqual(checkpoint["entries"][0]["target_sha256"], _digest(row.text))
            self.assertEqual(checkpoint["integrity_evidence"], evidence)
            self.assertEqual(evidence["one"]["validation_issues"], [])
            self.assertEqual(evidence["one"]["acceptance_authority"], "")
            self.assertEqual(validate_release_checkpoint(checkpoint), checkpoint)
            path = root / "local-integrity.json"
            path.write_text(json.dumps(checkpoint), encoding="utf-8")
            payload = root / "payload"
            payload.mkdir()
            (payload / "synthetic-localization.dat").write_bytes(b"synthetic-localized-data")
            spec = root / "spec.json"
            spec.write_text(json.dumps({
                "patch_id": arguments["patch_id"], "release_id": arguments["release_id"],
                "display_name": "Synthetic local delivery", "checkpoint_file": path.name,
                "target": {"required_files": ["synthetic-game.exe"]},
            }), encoding="utf-8")
            package = build_filesystem_package(
                payload, spec, root / "local.zip", required_for_distribution=False,
            )
            self.assertEqual(package["checkpoint_schema"], LOCAL_INTEGRITY_SCHEMA)
            checkpoint["integrity_evidence"]["one"]["validator_source_sha256"] = "0" * 64
            with self.assertRaisesRegex(ReleaseCheckpointError, "evidence binding"):
                validate_release_checkpoint(checkpoint)

    def test_generator_rejects_source_placeholder_status_order_language_and_consent_drift(self) -> None:
        for mutation in ("source", "placeholder", "review", "accepted", "order", "language", "license", "integrity"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                inventory, row, arguments = self._generation_fixture(root)
                rows = [row]
                if mutation == "source":
                    row.source = "Changed source"
                elif mutation == "placeholder":
                    row.text = "\uc548\ub155"
                    row.candidate_digest = stable_hash(row.text)
                elif mutation == "review":
                    row.status = "reviewed"
                    row.acceptance_authority = "reviewer"
                elif mutation == "accepted":
                    row.status = "accepted_unreviewed"
                elif mutation == "order":
                    rows.append(row)
                elif mutation == "language":
                    arguments["target_language"] = "ja"
                elif mutation == "license":
                    arguments["license_name"] = ""
                else:
                    row.resolution_code = FailureCode.INTEGRITY_INVALID.value
                with self.assertRaises(ReleaseCheckpointError):
                    generate_local_release_integrity(root, inventory, rows, **arguments)

    def test_generator_preserves_real_review_and_user_acceptance_without_inventing_either(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory, row, arguments = self._generation_fixture(root)
            row.status = "reviewed"
            row.acceptance_authority = "reviewer"
            row.attempts = [AttemptRecord(
                stage="review", provider="synthetic-reviewer", status="approved",
                candidate_generation=1,
            )]
            arguments["review_required"] = True
            checkpoint, _ = generate_local_release_integrity(root, inventory, [row], **arguments)
            self.assertEqual(checkpoint["entries"][0]["status"], "reviewed")
            row.status = "accepted_unreviewed"
            row.acceptance_authority = "user_policy"
            row.attempts = []
            accepted, _ = generate_local_release_integrity(root, inventory, [row], **arguments)
            self.assertEqual(accepted["entries"][0]["status"], "accepted_unreviewed")
            row.status = "source_preserved"
            row.text = row.source
            row.acceptance_authority = ""
            preserved, _ = generate_local_release_integrity(root, inventory, [row], **arguments)
            self.assertEqual(preserved["entries"][0]["source_sha256"], preserved["entries"][0]["target_sha256"])


    def test_generator_keeps_blocking_source_preservation_on_hold_without_explicit_incomplete_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory, row, arguments = self._generation_fixture(root)
            payload = json.loads(inventory.read_text(encoding="utf-8"))
            payload["segments"][0]["metadata"] = {"blocking_on_failure": True}
            inventory.write_text(json.dumps(payload), encoding="utf-8")
            row.status = "source_preserved"
            row.text = row.source
            with self.assertRaisesRegex(ReleaseCheckpointError, "incomplete policy"):
                generate_local_release_integrity(root, inventory, [row], **arguments)
            arguments["allow_incomplete_artifact"] = True
            checkpoint, _ = generate_local_release_integrity(root, inventory, [row], **arguments)
            self.assertEqual(checkpoint["entries"][0]["status"], "source_preserved")


if __name__ == "__main__":
    unittest.main()
