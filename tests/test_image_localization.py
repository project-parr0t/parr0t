from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.errors import ConfigurationError
from game_l10n.image_localization import (
    public_image_localization_state,
    record_image_localization_policy,
    record_image_unreviewed_acceptance,
    verify_image_localization_results,
    verify_image_ui_inventory,
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ImageLocalizationTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path]:
        work = root / "work"
        game = root / "game"
        source = game / "fixture-ui" / "title.png"
        source.parent.mkdir(parents=True)
        work.mkdir()
        source.write_bytes(b"synthetic-source-image")
        manifest = work / "inputs" / "image-ui-inventory.json"
        manifest.parent.mkdir()
        manifest.write_text(
            json.dumps(
                {
                    "schema": "parr0t.image-ui-inventory.v1",
                    "method": "synthetic-engine-ui-bindings",
                    "implementation_fingerprint": "synthetic-image-inventory-v1",
                    "coverage": {
                        "declared_complete": True,
                        "candidate_asset_count": 1,
                        "confirmed_ui_text_asset_count": 1,
                        "uncertain_asset_count": 0,
                        "non_ui_asset_count": 0,
                    },
                    "assets": [
                        {
                            "id": "synthetic-ui-title",
                            "source_root_index": 0,
                            "relative_path": "fixture-ui/title.png",
                            "source_sha256": digest(source.read_bytes()),
                            "format": "png",
                            "width": 64,
                            "height": 32,
                            "ui_binding": {
                                "kind": "engine_ui_binding",
                                "evidence_sha256": "a" * 64,
                            },
                            "text_regions": [
                                {
                                    "id": "title-region",
                                    "bbox": [2, 3, 40, 12],
                                    "source_text_sha256": "b" * 64,
                                    "detection_method": "ocr_then_visual_confirmation",
                                    "visually_confirmed": True,
                                    "player_visible": True,
                                }
                            ],
                        }
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return work, game, manifest

    def test_image_route_is_disabled_until_explicit_user_request(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            work, game, manifest = self._fixture(Path(directory))
            self.assertEqual(public_image_localization_state(work)["status"], "disabled")
            with self.assertRaisesRegex(ConfigurationError, "USER_REQUEST_REQUIRED"):
                verify_image_ui_inventory(work, manifest, game_roots=[game])
            with self.assertRaisesRegex(ConfigurationError, "USER_REQUEST_REQUIRED"):
                record_image_localization_policy(
                    work, requested=True, user_requested=False
                )

    def test_verified_scope_and_result_are_content_bound(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            work, game, manifest = self._fixture(Path(directory))
            record_image_localization_policy(
                work, requested=True, user_requested=True
            )
            scope = verify_image_ui_inventory(work, manifest, game_roots=[game])
            self.assertEqual(scope["status"], "verified_targets")
            self.assertEqual(scope["target_count"], 1)
            self.assertEqual(
                public_image_localization_state(work)["status"],
                "localization_required",
            )

            output = work / "synthetic-output" / "title.png"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"synthetic-localized-image")
            results = work / "translations" / "image-ui-results.json"
            results.parent.mkdir()
            results.write_text(
                json.dumps(
                    {
                        "schema": "parr0t.image-ui-results.v1",
                        "inventory_sha256": scope["inventory_sha256"],
                        "assets": [
                            {
                                "id": "synthetic-ui-title",
                                "source_sha256": digest(b"synthetic-source-image"),
                                "output_path": "synthetic-output/title.png",
                                "output_sha256": digest(output.read_bytes()),
                                "width": 64,
                                "height": 32,
                                "translated_region_ids": ["title-region"],
                                "review_status": "reviewed",
                                "user_acceptance_evidence_sha256": "",
                                "integrity": {
                                    "format_valid": True,
                                    "dimensions_preserved": True,
                                    "alpha_preserved_or_not_applicable": True,
                                    "atlas_layout_preserved_or_not_applicable": True,
                                    "outside_text_regions_preserved": True,
                                    "evidence_sha256": "c" * 64,
                                },
                            }
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            attestation = verify_image_localization_results(
                work, manifest, results, game_roots=[game]
            )

            self.assertEqual(attestation["status"], "complete")
            self.assertEqual(attestation["reviewed_count"], 1)
            self.assertEqual(public_image_localization_state(work)["status"], "complete")

            result_payload = json.loads(results.read_text(encoding="utf-8"))
            result_payload["assets"][0]["review_status"] = "user_accepted_unreviewed"
            result_payload["assets"][0]["user_acceptance_evidence_sha256"] = "d" * 64
            results.write_text(json.dumps(result_payload), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "core-recorded"):
                verify_image_localization_results(
                    work, manifest, results, game_roots=[game]
                )

            acceptance = record_image_unreviewed_acceptance(
                work,
                asset_ids=["synthetic-ui-title"],
                user_requested=True,
            )
            result_payload["assets"][0]["user_acceptance_evidence_sha256"] = acceptance[
                "acceptance_digest"
            ]
            results.write_text(json.dumps(result_payload), encoding="utf-8")
            accepted = verify_image_localization_results(
                work, manifest, results, game_roots=[game]
            )
            self.assertEqual(accepted["reviewed_count"], 0)
            self.assertEqual(accepted["user_accepted_unreviewed_count"], 1)

            output.write_bytes(b"changed")
            self.assertEqual(public_image_localization_state(work)["status"], "invalid")

    def test_uncertain_or_unconfirmed_asset_cannot_enter_scope(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            work, game, manifest = self._fixture(Path(directory))
            record_image_localization_policy(
                work, requested=True, user_requested=True
            )
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["assets"][0]["text_regions"][0]["visually_confirmed"] = False
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ConfigurationError, "visually confirmed"):
                verify_image_ui_inventory(work, manifest, game_roots=[game])


if __name__ == "__main__":
    unittest.main()
