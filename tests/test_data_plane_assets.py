from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from game_l10n import data_plane_task
from game_l10n.control_plane import establish_control_plane_entry
from game_l10n.data_plane_task import prepare_data_plane_task, seal_data_plane_task
from game_l10n.errors import ConfigurationError
from game_l10n.image_localization import record_image_localization_policy, verify_image_ui_inventory, public_image_localization_state
from tests import test_image_localization as image_fixture
from tests.image_candidate_fixture import ready_image_candidate_fixture


class DataPlaneAssetTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path, dict]:
        work, game, inventory = image_fixture.ImageLocalizationTests._fixture(self, root)
        establish_control_plane_entry(work)
        config = {"game_roots": [str(game)], "sdk_root": str(root / "sdk")}
        return work, game, inventory, config

    def test_localizer_binds_only_verified_assets_and_validated_offline_processing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, game, inventory, config = self._fixture(Path(directory))
            record_image_localization_policy(work, requested=True, user_requested=True)
            verify_image_ui_inventory(work, inventory, game_roots=[game])
            script = Path(data_plane_task.__file__).resolve()
            processing = {"argv": [str(script)], "sha256": hashlib.sha256(script.read_bytes()).hexdigest()}
            output = work / ".game-l10n/data-plane/image_localizer"
            prepared = prepare_data_plane_task(
                work, output, worker_role="image_localizer", config=config,
                sdk_activation={"status": "active"}, offline_processing=[processing],
            )
            self.assertEqual(prepared["status"], "ready")
            task = json.loads((output / "task.json").read_text(encoding="utf-8"))
            source = (game / "fixture-ui/title.png").resolve()
            self.assertEqual(task["bound_local_assets"], [{
                "path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }])
            self.assertEqual(task["offline_processing"], [{**processing, "reusable": True}])
            self.assertEqual(task["content_source_policy"]["allowed_external_sources"], [])
            self.assertTrue(task["content_source_policy"]["semantic_content_generation"])

    def test_failed_consumption_does_not_publish_or_corrupt_a_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, game, inventory, config = self._fixture(Path(directory))
            record_image_localization_policy(work, requested=True, user_requested=True)
            verify_image_ui_inventory(work, inventory, game_roots=[game])
            script = Path(data_plane_task.__file__).resolve()
            entries = [{"argv": [str(script)], "sha256": hashlib.sha256(script.read_bytes()).hexdigest()}]
            output = work / ".game-l10n/data-plane/image_localizer"

            def reject_after_binding(*args, binding_path):
                self.assertTrue(binding_path.is_file())
                raise ConfigurationError("synthetic consumption rejection")

            with patch("game_l10n.third_party_approval.consume_offline_processing", side_effect=reject_after_binding):
                with self.assertRaisesRegex(ConfigurationError, "synthetic consumption rejection"):
                    prepare_data_plane_task(
                        work, output, worker_role="image_localizer", config=config,
                        sdk_activation={"status": "active"}, offline_processing=entries,
                    )
            self.assertFalse((output / "task.json").exists())
            self.assertFalse((output / "relay.json").exists())
            prepare_data_plane_task(
                work, output, worker_role="image_localizer", config=config,
                sdk_activation={"status": "active"},
            )
            seal_data_plane_task(work, output / "relay.json", worker_ended=True)
            previous_task = (output / "task.json").read_bytes()
            previous_relay = (output / "relay.json").read_bytes()
            with patch("game_l10n.third_party_approval.consume_offline_processing", side_effect=reject_after_binding):
                with self.assertRaisesRegex(ConfigurationError, "synthetic consumption rejection"):
                    prepare_data_plane_task(
                        work, output, worker_role="image_localizer", config=config,
                        sdk_activation={"status": "active"}, offline_processing=entries,
                    )
            self.assertEqual((output / "task.json").read_bytes(), previous_task)
            self.assertEqual((output / "relay.json").read_bytes(), previous_relay)

    def test_inspection_only_binds_explicit_files_without_translation_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, game, _, config = self._fixture(Path(directory))
            source = game / "fixture-ui/title.png"
            record_image_localization_policy(
                work, requested=True, user_requested=True,
                inspection_only=True, requested_scope="font_visual_inspection",
            )
            output = work / ".game-l10n/data-plane/image_ui_inventory"
            prepared = prepare_data_plane_task(
                work, output, worker_role="image_ui_inventory", config=config,
                sdk_activation={"status": "active"}, inspection_assets=[source],
            )
            self.assertEqual(prepared["status"], "ready")
            task = json.loads((output / "task.json").read_text(encoding="utf-8"))
            self.assertEqual([entry["path"] for entry in task["bound_local_assets"]], [str(source.resolve())])
            self.assertFalse(task["content_source_policy"]["semantic_content_generation"])
            with self.assertRaisesRegex(ConfigurationError, "USER_REQUEST_REQUIRED"):
                prepare_data_plane_task(
                    work, work / ".game-l10n/data-plane/image_localizer",
                    worker_role="image_localizer", config=config, sdk_activation={"status": "active"},
                )
            with self.assertRaises(ConfigurationError):
                prepare_data_plane_task(
                    work, work / ".game-l10n/data-plane/invalid-inspection",
                    worker_role="image_ui_inventory", config=config,
                    sdk_activation={"status": "active"}, inspection_assets=[game],
                )

    def test_full_request_automatically_binds_candidates_and_holds_unchanged_opaque_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, game, inventory, config = self._fixture(Path(directory))
            poster = game / "fixture-scenery/poster.webp"
            poster.parent.mkdir()
            poster.write_bytes(b"RIFF\x10\x00\x00\x00WEBPsynthetic")
            font = game / "face.resource"
            font.write_bytes(b"OTTOsynthetic-font")
            package = game / "packed.pak"
            package.write_bytes(b"opaque-synthetic-package")
            note = game / "notes.txt"
            note.write_text("not an inspection asset", encoding="utf-8")
            before = {path: path.read_bytes() for path in game.rglob("*") if path.is_file()}
            scope = "player_visible_ui_cg_scenery_text_assets"
            record_image_localization_policy(work, requested=True, user_requested=True, requested_scope=scope)
            output = work / ".game-l10n/data-plane/image_ui_inventory"
            prepared = prepare_data_plane_task(
                work, output, worker_role="image_ui_inventory", config=config,
                sdk_activation={"status": "active"},
            )
            self.assertEqual(prepared["status"], "ready")
            task = json.loads((output / "task.json").read_text(encoding="utf-8"))
            discovery_path = work / prepared["candidate_discovery"]
            discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
            self.assertEqual(prepared["candidate_discovery"], task["input_image_candidates"])
            self.assertEqual(hashlib.sha256(discovery_path.read_bytes()).hexdigest(), task["input_image_candidates_sha256"])
            self.assertEqual(discovery["requested_scope"], scope)
            self.assertEqual(discovery["bound_local_assets"], task["bound_local_assets"])
            self.assertEqual(
                {entry["path"] for entry in task["bound_local_assets"]},
                {str((game / "fixture-ui/title.png").resolve()), str(poster.resolve()), str(font.resolve())},
            )
            self.assertEqual([item["relative_path"] for item in discovery["unexamined_sources"]], ["packed.pak"])
            report = json.loads(inventory.read_text(encoding="utf-8"))
            report["assets"] = []
            report["coverage"] = {
                "declared_complete": True, "candidate_asset_count": 0,
                "confirmed_ui_text_asset_count": 0, "uncertain_asset_count": 0,
                "non_ui_asset_count": 0, "requested_scope": scope,
            }
            inventory.write_text(json.dumps(report), encoding="utf-8")
            (work / task["output_envelope"]).write_text(json.dumps({
                "schema": "parr0t.control-envelope.v2", "worker_role": "image_ui_inventory",
                "stage": "image_inventory", "status": "success", "target_ids": [],
                "counts": {"total": 1, "success": 1, "pending": 0}, "issue_codes": [],
                "artifact_paths": ["inputs/image-ui-inventory.json"],
                "queue_transitions": [], "route_evidence_path": "",
            }), encoding="utf-8")
            sealed = seal_data_plane_task(work, work / prepared["relay_path"], worker_ended=True)
            self.assertEqual(sealed["status"], "partial")
            self.assertEqual(public_image_localization_state(work)["status"], "scope_incomplete")
            original_task = (output / "task.json").read_bytes()
            held = prepare_data_plane_task(
                work, output, worker_role="image_ui_inventory", config=config,
                sdk_activation={"status": "active"},
            )
            self.assertEqual(held["next_action"], "prepare_and_validate_extractors")
            self.assertFalse(held["dispatch_allowed"])
            self.assertEqual((output / "task.json").read_bytes(), original_task)
            requirements_path = work / held["extractor_requirements_path"]
            self.assertEqual(hashlib.sha256(requirements_path.read_bytes()).hexdigest(), held["extractor_requirements_sha256"])
            requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
            self.assertIn("bounded-image-font-candidate-discovery", requirements["required_capabilities"])
            self.assertEqual(requirements["source_metadata"]["image_candidate_discovery"]["path"], held["candidate_discovery"])
            for path, body in before.items():
                self.assertEqual(path.read_bytes(), body)

    def test_opaque_only_source_routes_to_real_sdk_repair_then_admits_exact_derived_input(self) -> None:
        fixture, initial, payload = ready_image_candidate_fixture(self)
        self.assertEqual(initial["next_action"], "prepare_and_validate_extractors")
        self.assertFalse((fixture.work / ".game-l10n/data-plane/image_ui_inventory/task.json").exists())
        output = fixture.work / ".game-l10n/data-plane/image_ui_inventory"
        prepared = prepare_data_plane_task(
            fixture.work, output, worker_role="image_ui_inventory", config=fixture.config,
            sdk_activation=fixture.activation,
        )
        self.assertEqual(prepared["status"], "ready")
        task = json.loads((output / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(task["source_roots"], [str(fixture.game.resolve())])
        self.assertEqual(task["bound_local_assets"], payload["bound_local_assets"])
        self.assertEqual(fixture.config["game_roots"], [str(fixture.game.resolve())])
        self.assertEqual(task["image_source_roots"], [str(fixture.game.resolve()), str((fixture.work / "inputs/image-assets").resolve())])
        discovery = json.loads((fixture.work / task["input_image_candidates"]).read_text(encoding="utf-8"))
        self.assertEqual(discovery["unexamined_sources"], [])
        self.assertEqual(discovery["bound_local_assets"], payload["bound_local_assets"])
        self.assertEqual(discovery["resolved_source_bindings"], payload["source_bindings"])
        self.assertTrue((fixture.work / task["input_image_sdk_candidates"]).is_file())

    def test_successor_image_contract_requires_all_previously_resolved_sources(self) -> None:
        fixture, _, payload = ready_image_candidate_fixture(self)
        config = json.loads(json.dumps(fixture.config))
        context = config["extractor_requirements"]["source_metadata"]["image_candidate_discovery"]
        original = json.loads((fixture.work / context["path"]).read_text(encoding="utf-8"))
        next_source = fixture.game / "next.pak"
        next_source.write_bytes((fixture.game / "packed.pak").read_bytes())
        next_binding = {
            "source_root_index": 0, "relative_path": "next.pak",
            "sha256": hashlib.sha256(next_source.read_bytes()).hexdigest(), "status": "complete",
        }
        original["resolved_source_bindings"] = payload["source_bindings"]
        original["unexamined_sources"] = [{key: value for key, value in next_binding.items() if key != "status"}]
        successor = fixture.work / ".game-l10n/successor-image-discovery.json"
        successor.write_text(json.dumps(original), encoding="utf-8")
        context["path"] = successor.relative_to(fixture.work).as_posix()
        context["sha256"] = hashlib.sha256(successor.read_bytes()).hexdigest()
        sdk_output = fixture.work / "inputs/image-inspection-candidates.json"

        def validate(bindings: list[dict]) -> None:
            successor_payload = {**payload, "source_bindings": bindings}
            sdk_output.write_text(json.dumps(successor_payload), encoding="utf-8")
            evidence = {"checks": [{"outputs": {
                "inputs/image-inspection-candidates.json": hashlib.sha256(sdk_output.read_bytes()).hexdigest(),
            }}]}
            data_plane_task.validate_image_candidate_preparation(fixture.work, config, evidence)

        validate([*payload["source_bindings"], next_binding])
        with self.assertRaises(ConfigurationError):
            validate([next_binding])

    def test_image_source_inventory_input_is_metadata_not_narrative_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, game, inventory, config = self._fixture(Path(directory))
            record_image_localization_policy(work, requested=True, user_requested=True)
            primary = work / "inputs/primary.jsonl"
            primary.write_text('{"source":"PRIVATE_SYNTHETIC_NARRATIVE_NOT_AN_IMAGE_INPUT"}\n', encoding="utf-8")
            (work / ".game-l10n/inventory-attestation.json").write_text(
                json.dumps({"primary_path": str(primary)}), encoding="utf-8",
            )
            info = primary.stat()
            verified = {"file_bindings": {"primary": {"size": info.st_size, "mtime_ns": info.st_mtime_ns}}}
            with patch.object(data_plane_task, "require_inventory_attestation", return_value=verified):
                _, inputs = data_plane_task._image_inventory_candidates(
                    work, work / ".game-l10n/data-plane/image_ui_inventory", [game], inventory, config, None,
                )
            bound = work / inputs["input_image_source_inventory"]
            self.assertNotEqual(bound, primary)
            self.assertNotIn("PRIVATE_SYNTHETIC_NARRATIVE_NOT_AN_IMAGE_INPUT", bound.read_text(encoding="utf-8"))
            metadata = json.loads(bound.read_text(encoding="utf-8"))
            self.assertTrue(metadata["raw_source_text_is_not_bound"])
            self.assertEqual(metadata["primary_sha256"], hashlib.sha256(primary.read_bytes()).hexdigest())

    def test_unexamined_requested_scope_cannot_seal_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, _, inventory, config = self._fixture(Path(directory))
            record_image_localization_policy(work, requested=True, user_requested=True)
            payload = json.loads(inventory.read_text(encoding="utf-8"))
            payload["coverage"].update(candidate_asset_count=2, unexamined_asset_count=1)
            inventory.write_text(json.dumps(payload), encoding="utf-8")
            output = work / ".game-l10n/data-plane/image_ui_inventory"
            prepared = prepare_data_plane_task(
                work, output, worker_role="image_ui_inventory", config=config,
                sdk_activation={"status": "active"},
            )
            task = json.loads((output / "task.json").read_text(encoding="utf-8"))
            envelope = work / task["output_envelope"]
            envelope.write_text(json.dumps({
                "schema": "parr0t.control-envelope.v2", "worker_role": "image_ui_inventory",
                "stage": "image_inventory", "status": "success", "target_ids": [],
                "counts": {"total": 1, "success": 1, "pending": 0}, "issue_codes": [],
                "artifact_paths": ["inputs/image-ui-inventory.json"],
                "queue_transitions": [], "route_evidence_path": "",
            }), encoding="utf-8")
            sealed = seal_data_plane_task(work, work / prepared["relay_path"], worker_ended=True)
            self.assertEqual(sealed["status"], "partial")
            self.assertIn("image_scope_unexamined", sealed["issue_codes"])
            self.assertGreater(sealed["counts"]["pending"], 0)


if __name__ == "__main__":
    unittest.main()
