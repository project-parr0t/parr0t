from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from game_l10n.coverage import validate_inventory_display_scope, verify_inventory_pair
from game_l10n.formats import read_segments
from game_l10n.rpg_maker_mv.adapter import (
    RpgMakerMvError, _Extractor, apply_translations, extract_game, verify_patch,
)
from tests.test_rpg_maker_mv_adapter import (
    _fixture_game, _result, _rows, _source_hash, _write_json,
)


def _plugin(game: Path, text: str) -> Path:
    (game / "www/js/plugins.js").write_text(
        'var $plugins = [{"name":"SyntheticDisplay","status":true}];', encoding="utf-8",
    )
    path = game / "www/js/plugins/SyntheticDisplay.js"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class DisplayEligibilityConsumerTests(unittest.TestCase):
    def test_internal_metadata_stays_out_of_targets_and_semantic_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _fixture_game(root)
            data = game / "www/data"
            private = "EDITOR_ONLY_" + chr(0x3042)
            notes = "NOTE_ONLY_" + chr(0x3044)
            comment = "COMMENT_ONLY_" + chr(0x3046)
            visible_map = "ENGINE_DEBUG_VISIBLE_" + chr(0x3048)
            _write_json(data / "MapInfos.json", [None, {"id": 1, "name": private}])
            system = json.loads((data / "System.json").read_text(encoding="utf-8"))
            system["terms"]["messages"] = {"error": "ENGINE_ERROR_VISIBLE"}
            _write_json(data / "System.json", system)
            actors = json.loads((data / "Actors.json").read_text(encoding="utf-8"))
            actors[1]["note"] = notes
            _write_json(data / "Actors.json", actors)
            document = json.loads((data / "Map001.json").read_text(encoding="utf-8"))
            document["displayName"] = visible_map
            event = document["events"][1]
            event.update(name=private + "_EVENT", note=notes)
            commands = event["pages"][0]["list"]
            commands[:0] = [
                {"code": 108, "indent": 0, "parameters": [comment]},
                {"code": 111, "indent": 0, "parameters": [0, 1, 0]},
                {"code": 355, "indent": 1, "parameters": [
                    '// $gameMessage.add("COMMENT_FAKE_DISPLAY");\n'
                    'var internal = "SCRIPT_CONTROL_ONLY"; $gameMessage.add("ENGINE_SYSTEM_VISIBLE");'
                ]},
                {"code": 401, "indent": 1, "parameters": ["\\N<SPEAKER_VISIBLE>FLOW_VISIBLE"]},
                {"code": 412, "indent": 0, "parameters": []},
            ]
            _write_json(data / "Map001.json", document)
            before = _source_hash(game)
            output = root / "extract"
            report = extract_game(game, output)
            targets = read_segments(output / "segments.jsonl")
            sources = {row.source for row in targets}
            self.assertTrue({visible_map, "ENGINE_ERROR_VISIBLE", "ENGINE_SYSTEM_VISIBLE"} <= sources)
            self.assertIn("\\N<SPEAKER_VISIBLE>FLOW_VISIBLE", sources)
            semantic = json.dumps([row.metadata for row in targets], ensure_ascii=False)
            context = json.loads((output / "context-script.json").read_text(encoding="utf-8"))
            semantic += json.dumps(context, ensure_ascii=False)
            for excluded in (private, notes, comment, "COMMENT_FAKE_DISPLAY", "SCRIPT_CONTROL_ONLY"):
                self.assertNotIn(excluded, sources)
                self.assertNotIn(excluded, semantic)
            self.assertTrue(any(fact["code"] == 111 for scene in context["ordered_scenes"] for fact in scene["flow"]))
            speaker = next(row for row in targets if row.source.endswith("FLOW_VISIBLE"))
            self.assertEqual(speaker.speaker, "SPEAKER_VISIBLE")
            evidence = json.loads((output / "source-evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["ordered_scenes"][0]["commands"], commands)
            inventory = json.loads((output / "primary-inventory.json").read_text(encoding="utf-8"))
            validate_inventory_display_scope(inventory, required=True)
            self.assertEqual({row.source for row in read_segments(output / "primary-inventory.json")}, sources)
            self.assertGreater(inventory["coverage"]["display_scope"]["control_only_count"], 0)
            self.assertEqual(report["inventory_status"], "static_verified")

            results = root / "results.json"
            map_target = next(row for row in targets if row.source == visible_map)
            _write_json(results, {"results": [_result(map_target.id, "TARGET_VISIBLE_MAP")]})
            patch_root = root / "patch"
            apply_translations(output / "apply-manifest.json", results, game, patch_root)
            rebuilt = json.loads((patch_root / "www/data/Map001.json").read_text(encoding="utf-8"))
            self.assertEqual(rebuilt["displayName"], "TARGET_VISIBLE_MAP")
            self.assertEqual(rebuilt["events"], document["events"])
            self.assertEqual(json.loads((patch_root / "www/data/MapInfos.json").read_text(encoding="utf-8")),
                             [None, {"id": 1, "name": private}])
            self.assertEqual(json.loads((patch_root / "www/data/Actors.json").read_text(encoding="utf-8")), actors)
            self.assertEqual(_source_hash(game), before)
            self.assertTrue(verify_patch(output / "apply-manifest.json", game, patch_root)["source_unchanged"])

    def test_explicit_map_info_consumers_get_display_only_projections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _fixture_game(root)
            map_path = game / "www/data/Map001.json"
            document = json.loads(map_path.read_text(encoding="utf-8"))
            script = 'var control = $dataMapInfos[1].name; $gameMessage.add($dataMapInfos[1].name);'
            document["events"][1]["pages"][0]["list"] = [
                {"code": 355, "indent": 0, "parameters": [script]},
            ]
            _write_json(map_path, document)
            plugin_text = 'window.drawText($dataMapInfos[1].name, 0); var control = "KEEP_CONTROL";'
            plugin = _plugin(game, plugin_text)
            before = _source_hash(game)
            output = root / "extract"
            extract_game(game, output)
            rows = _rows(output / "segments.jsonl")
            projections = [row for row in rows if row["metadata"]["kind"] == "script_display_projection"]
            self.assertEqual(len(projections), 2)
            self.assertTrue(all(row["source"] == "SOURCE_MAP_INFO" for row in projections))
            self.assertFalse(any(row["metadata"]["file"] == "MapInfos.json" for row in rows))
            results = root / "results.json"
            _write_json(results, {"results": [_result(row["id"], "TARGET_PROJECTED") for row in projections]})
            patch_root = root / "patch"
            apply_translations(output / "apply-manifest.json", results, game, patch_root)
            patched_map = patch_root / "www/data/Map001.json"
            rebuilt = json.loads(patched_map.read_text(encoding="utf-8"))
            rebuilt_script = rebuilt["events"][1]["pages"][0]["list"][0]["parameters"][0]
            self.assertEqual(rebuilt_script, 'var control = $dataMapInfos[1].name; $gameMessage.add("TARGET_PROJECTED");')
            self.assertEqual((patch_root / "www/js/plugins/SyntheticDisplay.js").read_text(encoding="utf-8"),
                             'window.drawText("TARGET_PROJECTED", 0); var control = "KEEP_CONTROL";')
            self.assertEqual(json.loads((patch_root / "www/data/MapInfos.json").read_text(encoding="utf-8"))[1]["name"],
                             "SOURCE_MAP_INFO")
            self.assertEqual(_source_hash(game), before)
            self.assertEqual(plugin.read_text(encoding="utf-8"), plugin_text)
            verify_patch(output / "apply-manifest.json", game, patch_root)

            # Even a mapped JSON script field cannot authorize edits to its control surroundings.
            rebuilt["events"][1]["pages"][0]["list"][0]["parameters"][0] = rebuilt_script.replace("var control", "var changed")
            _write_json(patched_map, rebuilt)
            with self.assertRaisesRegex(RpgMakerMvError, "PATCH_CHANGED_UNMAPPED_DATA"):
                verify_patch(output / "apply-manifest.json", game, patch_root)
            _write_json(results, {"results": []})
            preserved = root / "preserved"
            apply_translations(output / "apply-manifest.json", results, game, preserved)
            self.assertEqual(json.loads((preserved / "www/data/Map001.json").read_text(encoding="utf-8")), document)
            self.assertEqual((preserved / "www/js/plugins/SyntheticDisplay.js").read_text(encoding="utf-8"), plugin_text)

    def test_dynamic_display_dependencies_stay_pending_without_admitting_all_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _fixture_game(root)
            _plugin(game, 'var labels = {entry:"UNCLASSIFIED_LITERAL"}; '
                    'window.drawText(labels.entry,0); '
                    'window.drawText($dataMapInfos[$gameVariables.value(1)].name,0);')
            output = root / "extract"
            report = extract_game(game, output)
            sources = {row.source for row in read_segments(output / "segments.jsonl")}
            self.assertNotIn("SOURCE_MAP_INFO", sources)
            self.assertNotIn("UNCLASSIFIED_LITERAL", sources)
            self.assertEqual(report["inventory_status"], "incomplete")
            self.assertEqual(report["pending_display_dependency_count"], 2)
            for filename in ("primary-inventory.json", "independent-inventory.json"):
                inventory = json.loads((output / filename).read_text(encoding="utf-8"))
                validate_inventory_display_scope(inventory, required=True)
                self.assertFalse(inventory["coverage"]["declared_complete"])
                self.assertEqual(len(inventory["coverage"]["pending_display_dependencies"]), 2)
                self.assertGreater(inventory["coverage"]["display_scope"]["unclassified_count"], 0)
            census = (output / "source-census.json").read_text(encoding="utf-8")
            self.assertNotIn("UNCLASSIFIED_LITERAL", census)
            attestation = verify_inventory_pair(output / "primary-inventory.json", output / "independent-inventory.json")
            self.assertEqual(attestation["issue_codes"], ["INVENTORY_NOT_DECLARED_COMPLETE"])
            # Pending dependencies still retain immutable source/apply evidence.
            original_plugin = (game / "www/js/plugins/SyntheticDisplay.js").read_bytes()
            self.assertEqual((output / "baseline/js/plugins/SyntheticDisplay.js").read_bytes(), original_plugin)
            results = root / "results.json"
            _write_json(results, {"results": []})
            patch_root = root / "preserved"
            apply_translations(output / "apply-manifest.json", results, game, patch_root)
            self.assertEqual((patch_root / "www/js/plugins/SyntheticDisplay.js").read_bytes(), original_plugin)
            verify_patch(output / "apply-manifest.json", game, patch_root)

    def test_primary_only_omissions_create_real_inventory_disagreements(self) -> None:
        for omitted in ("SOURCE_MAP_DISPLAY", "SOURCE_CHOICE_A", "SOURCE_DOUBLE", "SOURCE_ACTOR", "SOURCE_PLUGIN_UI"):
            with self.subTest(omitted=omitted), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                game = _fixture_game(root)
                _plugin(game, 'window.drawText("SOURCE_PLUGIN_UI",0);')
                output = root / "extract"
                original_add = _Extractor.add

                def omit_primary(extractor, filename, paths, value, *args, **kwargs):
                    if value != omitted:
                        return original_add(extractor, filename, paths, value, *args, **kwargs)
                    return None

                with patch.object(_Extractor, "add", omit_primary):
                    with self.assertRaisesRegex(RpgMakerMvError, "STATIC_SCOPE_INVENTORY_MISMATCH"):
                        extract_game(game, output)
                primary = read_segments(output / "primary-inventory.json")
                independent = read_segments(output / "independent-inventory.json")
                self.assertNotIn(omitted, {row.source for row in primary})
                self.assertIn(omitted, {row.source for row in independent})
                attestation = verify_inventory_pair(output / "primary-inventory.json", output / "independent-inventory.json")
                self.assertEqual(attestation["missing_from_primary_count"], 1)
                self.assertIn("INDEPENDENT_INVENTORY_FOUND_UNEXTRACTED_TARGETS", attestation["issue_codes"])
                self.assertFalse((output / "apply-manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
