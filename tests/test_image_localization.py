from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.errors import ConfigurationError
from game_l10n.image_localization import (
    bound_local_assets,
    bound_inspection_assets,
    require_image_inspection_requested,
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


    def test_exact_asset_bindings_reject_source_changes_and_unknown_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, game, manifest = self._fixture(Path(directory))
            record_image_localization_policy(work, requested=True, user_requested=True)
            source = (game / "fixture-ui/title.png").resolve()
            self.assertEqual(bound_local_assets(work, manifest, game_roots=[game]), [
                {"path": str(source), "sha256": digest(source.read_bytes())}
            ])
            with self.assertRaisesRegex(ConfigurationError, "ASSET_SCOPE_INVALID"):
                bound_local_assets(work, manifest, game_roots=[game], asset_ids=["unknown"])
            source.write_bytes(b"changed")
            with self.assertRaisesRegex(ConfigurationError, "digest changed"):
                bound_local_assets(work, manifest, game_roots=[game])

    def test_visual_inspection_does_not_authorize_translation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, game, manifest = self._fixture(Path(directory))
            record_image_localization_policy(
                work, requested=True, user_requested=True,
                inspection_only=True, requested_scope="font_visual_inspection",
            )
            self.assertEqual(require_image_inspection_requested(work)["status"], "inspection_requested")
            self.assertFalse(public_image_localization_state(work)["translation_authorized"])
            font = game / "fixture-font.ttf"
            font.write_bytes(b"synthetic-font")
            self.assertEqual(bound_inspection_assets(work, [font], game_roots=[game]), [
                {"path": str(font.resolve()), "sha256": digest(font.read_bytes())}
            ])
            with self.assertRaisesRegex(ConfigurationError, "INSPECTION_ASSET_INVALID"):
                bound_inspection_assets(work, [game], game_roots=[game])
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["coverage"]["requested_scope"] = "font_visual_inspection"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            receipt = verify_image_ui_inventory(work, manifest, game_roots=[game])
            self.assertEqual(receipt["status"], "inspection_verified")
            self.assertFalse(public_image_localization_state(work)["translation_authorized"])
            with self.assertRaisesRegex(ConfigurationError, "USER_REQUEST_REQUIRED"):
                verify_image_localization_results(
                    work, manifest, work / "unapproved-results.json", game_roots=[game],
                )

    def test_broad_player_visible_scope_must_be_examined_and_can_include_cg_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, game, manifest = self._fixture(Path(directory))
            record_image_localization_policy(
                work, requested=True, user_requested=True,
                requested_scope="all_player_visible_text",
            )
            with self.assertRaisesRegex(ConfigurationError, "SCOPE_UNEXAMINED"):
                verify_image_ui_inventory(work, manifest, game_roots=[game])
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["coverage"]["requested_scope"] = "all_player_visible_text"
            payload["assets"][0]["ui_binding"]["kind"] = "user_identified_player_visible_asset"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(verify_image_ui_inventory(work, manifest, game_roots=[game])["target_count"], 1)
            payload["assets"] = []
            payload["coverage"]["confirmed_ui_text_asset_count"] = 0
            payload["coverage"]["uncertain_asset_count"] = 1
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(verify_image_ui_inventory(work, manifest, game_roots=[game])["status"], "scope_incomplete")
            self.assertEqual(public_image_localization_state(work)["status"], "scope_incomplete")


    def test_bound_candidate_accounting_prevents_unexamined_no_targets(self) -> None:
        from game_l10n.store import stable_hash

        with tempfile.TemporaryDirectory() as directory:
            work, game, manifest = self._fixture(Path(directory))
            record_image_localization_policy(work, requested=True, user_requested=True)
            source = (game / "fixture-ui/title.png").resolve()
            discovery = {
                "schema": "parr0t.image-inspection-candidates.v1",
                "requested_scope": "player_visible_ui_text_assets",
                "bound_local_assets": [{"path": str(source), "sha256": digest(source.read_bytes())}],
                "unexamined_sources": [],
            }
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["assets"] = []
            payload["coverage"].update(candidate_asset_count=0, confirmed_ui_text_asset_count=0)
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            original = manifest.read_bytes()
            receipt = verify_image_ui_inventory(
                work, manifest, game_roots=[game], candidate_discovery=discovery,
            )
            self.assertEqual(receipt["status"], "scope_incomplete")
            self.assertEqual(receipt["candidate_discovery"]["bound_asset_count"], 1)
            self.assertEqual(receipt["candidate_discovery"]["reported_candidate_asset_count"], 0)
            self.assertEqual(receipt["candidate_discovery"]["digest"], stable_hash(discovery))
            self.assertEqual(manifest.read_bytes(), original)
            self.assertEqual(public_image_localization_state(work)["status"], "scope_incomplete")
            with self.assertRaisesRegex(ConfigurationError, "SCOPE_REQUIRED"):
                verify_image_localization_results(work, manifest, work / "results.json", game_roots=[game])
            receipt["status"] = "no_targets"
            receipt["issue_codes"] = []
            (work / ".game-l10n/image-ui-scope-attestation.json").write_text(
                json.dumps(receipt), encoding="utf-8",
            )
            self.assertEqual(public_image_localization_state(work)["status"], "scope_required")
            payload["coverage"].update(candidate_asset_count=1, non_ui_asset_count=1)
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            complete = verify_image_ui_inventory(
                work, manifest, game_roots=[game], candidate_discovery=discovery,
            )
            self.assertEqual(complete["status"], "no_targets")

    def test_unexamined_discovery_sources_keep_image_and_inspection_scope_honest(self) -> None:
        for inspection_only in (False, True):
            with self.subTest(inspection_only=inspection_only), tempfile.TemporaryDirectory() as directory:
                work, game, manifest = self._fixture(Path(directory))
                record_image_localization_policy(
                    work, requested=True, user_requested=True, inspection_only=inspection_only,
                )
                source = (game / "fixture-ui/title.png").resolve()
                discovery = {
                    "schema": "parr0t.image-inspection-candidates.v1",
                    "requested_scope": "player_visible_ui_text_assets",
                    "bound_local_assets": [{"path": str(source), "sha256": digest(source.read_bytes())}],
                    "unexamined_sources": [{"path": "synthetic-opaque.pack", "reason": "container"}],
                }
                receipt = verify_image_ui_inventory(
                    work, manifest, game_roots=[game], candidate_discovery=discovery,
                )
                self.assertEqual(receipt["status"], "scope_incomplete")
                self.assertEqual(receipt["target_count"], 1)
                self.assertEqual(receipt["candidate_discovery"]["unexamined_source_count"], 1)
                public = public_image_localization_state(work)
                summary = public["candidate_discovery"]
                self.assertEqual(summary["unexamined_source_count"], 1)
                self.assertEqual(summary["digest"], receipt["candidate_discovery"]["digest"])
                self.assertEqual(set(summary), {
                    "digest", "bound_asset_count", "reported_candidate_asset_count", "unexamined_source_count",
                })
                self.assertIn("IMAGE_SCOPE_UNEXAMINED", public["issue_codes"])
                if inspection_only:
                    self.assertEqual(public["inventory_status"], "scope_incomplete")
                    self.assertFalse(public["translation_authorized"])
                else:
                    self.assertEqual(public["status"], "scope_incomplete")

    def test_discovery_rejects_scope_hash_and_asset_authority_drift(self) -> None:
        for mutation in ("scope", "hash", "unbound"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                work, game, manifest = self._fixture(Path(directory))
                record_image_localization_policy(work, requested=True, user_requested=True)
                source = (game / "fixture-ui/title.png").resolve()
                discovery = {
                    "schema": "parr0t.image-inspection-candidates.v1",
                    "requested_scope": "player_visible_ui_text_assets",
                    "bound_local_assets": [{"path": str(source), "sha256": digest(source.read_bytes())}],
                    "unexamined_sources": [],
                }
                if mutation == "scope":
                    discovery["requested_scope"] = "different_requested_scope"
                elif mutation == "hash":
                    discovery["bound_local_assets"][0]["sha256"] = "0" * 64
                else:
                    discovery["bound_local_assets"] = []
                with self.assertRaisesRegex(ConfigurationError, "CANDIDATE"):
                    verify_image_ui_inventory(
                        work, manifest, game_roots=[game], candidate_discovery=discovery,
                    )
                self.assertEqual(public_image_localization_state(work)["status"], "scope_required")


    def test_reverification_cannot_drop_current_discovery_context_or_incomplete_hold(self) -> None:
        for inspection_only in (False, True):
            with self.subTest(inspection_only=inspection_only), tempfile.TemporaryDirectory() as directory:
                work, game, manifest = self._fixture(Path(directory))
                record_image_localization_policy(
                    work, requested=True, user_requested=True, inspection_only=inspection_only,
                )
                source = (game / "fixture-ui/title.png").resolve()
                discovery = {
                    "schema": "parr0t.image-inspection-candidates.v1",
                    "requested_scope": "player_visible_ui_text_assets",
                    "bound_local_assets": [{"path": str(source), "sha256": digest(source.read_bytes())}],
                    "unexamined_sources": [{"path": "synthetic.pack", "reason": "container"}],
                }
                receipt = verify_image_ui_inventory(
                    work, manifest, game_roots=[game], candidate_discovery=discovery,
                )
                self.assertEqual(receipt["status"], "scope_incomplete")
                scope_path = work / ".game-l10n/image-ui-scope-attestation.json"
                original_scope = scope_path.read_bytes()
                with self.assertRaisesRegex(ConfigurationError, "CANDIDATE_DISCOVERY_REQUIRED"):
                    verify_image_ui_inventory(work, manifest, game_roots=[game])
                self.assertEqual(scope_path.read_bytes(), original_scope)
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                payload["assets"] = []
                payload["coverage"].update(
                    candidate_asset_count=0, confirmed_ui_text_asset_count=0,
                    uncertain_asset_count=0, non_ui_asset_count=0,
                )
                manifest.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ConfigurationError, "CANDIDATE_DISCOVERY_REQUIRED"):
                    verify_image_ui_inventory(work, manifest, game_roots=[game])
                self.assertEqual(scope_path.read_bytes(), original_scope)
                revalidated = verify_image_ui_inventory(
                    work, manifest, game_roots=[game], candidate_discovery=discovery,
                )
                self.assertEqual(revalidated["status"], "scope_incomplete")
                self.assertEqual(revalidated["candidate_discovery"]["bound_asset_count"], 1)
                public = public_image_localization_state(work)
                if inspection_only:
                    self.assertEqual(public["inventory_status"], "scope_incomplete")
                    self.assertFalse(public["translation_authorized"])
                else:
                    self.assertEqual(public["status"], "scope_incomplete")


    def test_unready_workspace_image_cache_never_becomes_an_original_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, game, manifest = self._fixture(Path(directory))
            record_image_localization_policy(work, requested=True, user_requested=True)
            cache = work / "inputs/image-assets"
            cache.mkdir()
            image = cache / "synthetic-extracted.png"
            image.write_bytes(b"synthetic-derived-image")
            (work / "inputs/image-inspection-candidates.json").write_text(json.dumps({
                "schema": "parr0t.image-inspection-candidates.v1",
                "requested_scope": "player_visible_ui_text_assets",
                "bound_local_assets": [{"path": str(image.resolve()), "sha256": digest(image.read_bytes())}],
                "unexamined_sources": [],
            }), encoding="utf-8")
            original_roots = [game]
            verify_image_ui_inventory(work, manifest, game_roots=original_roots)
            self.assertEqual(original_roots, [game])
            self.assertEqual(public_image_localization_state(work)["status"], "localization_required")
            for roots in ([game], [game, cache]):
                with self.subTest(roots=roots):
                    with self.assertRaisesRegex(ConfigurationError, "INSPECTION_ASSET_INVALID"):
                        bound_inspection_assets(work, [image], game_roots=roots)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["assets"][0].update(
                source_root_index=1, relative_path=image.name, source_sha256=digest(image.read_bytes()),
            )
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            for roots in ([game], [game, cache]):
                with self.subTest(roots=roots):
                    with self.assertRaisesRegex(ConfigurationError, "source root index"):
                        verify_image_ui_inventory(work, manifest, game_roots=roots)
                    with self.assertRaisesRegex(ConfigurationError, "source root index"):
                        bound_local_assets(work, manifest, game_roots=roots)


    def test_attested_derived_assets_are_exact_and_revoke_on_cache_or_source_drift(self) -> None:
        from tests.image_candidate_fixture import ready_image_candidate_fixture

        fixture, _, discovery = ready_image_candidate_fixture(self)
        work, game = fixture.work, fixture.game
        source = Path(discovery["bound_local_assets"][0]["path"])
        roots = [game]
        inventory = work / "inputs/derived-image-inventory.json"
        payload = {
            "schema": "parr0t.image-ui-inventory.v1",
            "method": "synthetic-derived-asset-consumer",
            "implementation_fingerprint": "synthetic-derived-consumer-v1",
            "coverage": {
                "declared_complete": True, "candidate_asset_count": 1,
                "confirmed_ui_text_asset_count": 1, "uncertain_asset_count": 0,
                "non_ui_asset_count": 0,
            },
            "assets": [{
                "id": "synthetic-derived", "source_root_index": 1,
                "relative_path": source.name, "source_sha256": digest(source.read_bytes()),
                "format": "png", "width": 64, "height": 32,
                "ui_binding": {"kind": "engine_ui_binding", "evidence_sha256": "a" * 64},
                "text_regions": [{
                    "id": "synthetic-region", "bbox": [2, 3, 40, 12],
                    "source_text_sha256": "b" * 64, "detection_method": "manual_transcription",
                    "visually_confirmed": True, "player_visible": True,
                }],
            }],
        }
        inventory.write_text(json.dumps(payload), encoding="utf-8")
        original_inventory = inventory.read_bytes()
        scope = verify_image_ui_inventory(
            work, inventory, game_roots=roots, candidate_discovery=discovery,
        )
        self.assertEqual(scope["status"], "verified_targets")
        self.assertEqual(roots, [game])
        self.assertEqual(scope["game_roots"], [str(game.resolve())])
        self.assertEqual(scope["source_roots"], [str(game.resolve()), str(source.parent)])
        expected = discovery["bound_local_assets"]
        self.assertEqual(bound_inspection_assets(work, [source], game_roots=roots), expected)
        self.assertEqual(bound_local_assets(work, inventory, game_roots=roots), expected)
        self.assertEqual(public_image_localization_state(work)["status"], "localization_required")
        with self.assertRaisesRegex(ConfigurationError, "RESULT_PATH_INVALID"):
            verify_image_localization_results(
                work, inventory, work / "missing-results.json", game_roots=roots,
            )

        unlisted = source.parent / "unlisted.png"
        unlisted.write_bytes(source.read_bytes())
        with self.assertRaisesRegex(ConfigurationError, "DERIVED_ASSET_UNATTESTED"):
            bound_inspection_assets(work, [unlisted], game_roots=[game, source.parent])
        payload["assets"][0]["relative_path"] = unlisted.name
        inventory.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "DERIVED_ASSET_UNATTESTED"):
            bound_local_assets(work, inventory, game_roots=roots)
        inventory.write_bytes(original_inventory)

        candidate_path = work / "inputs/image-inspection-candidates.json"
        for changed in (source, game / "packed.pak", candidate_path):
            with self.subTest(changed=changed.name):
                original = changed.read_bytes()
                try:
                    changed.write_bytes(original + b" ")
                    with self.assertRaises(ConfigurationError):
                        bound_inspection_assets(work, [source], game_roots=roots)
                    with self.assertRaises(ConfigurationError):
                        bound_local_assets(work, inventory, game_roots=roots)
                    with self.assertRaises(ConfigurationError):
                        verify_image_ui_inventory(
                            work, inventory, game_roots=roots, candidate_discovery=discovery,
                        )
                    self.assertEqual(public_image_localization_state(work)["status"], "scope_required")
                    with self.assertRaisesRegex(ConfigurationError, "SCOPE_REQUIRED"):
                        verify_image_localization_results(
                            work, inventory, work / "missing-results.json", game_roots=roots,
                        )
                finally:
                    changed.write_bytes(original)
                self.assertEqual(public_image_localization_state(work)["status"], "localization_required")


if __name__ == "__main__":
    unittest.main()
