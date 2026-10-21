from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.release_checkpoint import (
    CHECKPOINT_SCHEMA,
    ReleaseCheckpointError,
    canonical_checkpoint_bytes,
    validate_release_checkpoint,
)


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


if __name__ == "__main__":
    unittest.main()
