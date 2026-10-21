from __future__ import annotations

import hashlib
import inspect
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from game_l10n.coverage import verify_inventory_pair
from game_l10n.rpg_maker_mv.adapter import (
    RpgMakerMvError,
    apply_translations,
    decode_js_literal,
    discover_game,
    encode_js_literal,
    extract_game,
    pack_patch,
    verify_patch,
)


class RuntimeEntryDiscoveryTests(unittest.TestCase):
    def test_unsafe_candidate_override_is_not_exposed(self):
        self.assertNotIn("keep_candidate_text", inspect.signature(apply_translations).parameters)

    def test_text_spans_support_both_raw_crlf_and_normalized_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _fixture_game(root)
            _write_json(game / "www/js/plugins.js", [])
            (game / "www/js/plugins.js").write_text('var $plugins = [{"name":"SyntheticDisplay","status":true}];')
            plugin = game / "www/js/plugins/SyntheticDisplay.js"
            plugin.parent.mkdir()
            raw = "// FIXTURE\r\nwin.drawText('SOURCE_UI',0);\r\n"
            plugin.write_bytes(b"\xef\xbb\xbf" + raw.encode())
            extracted = root / "extracted"
            extract_game(game, extracted)
            manifest_path = extracted / "apply-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            entry = next(e for e in manifest["segments"] if e["source"] == "SOURCE_UI")
            results = root / "results.json"
            _write_json(results, {"results": [_result(entry["id"], "TARGET_UI")]})
            apply_translations(manifest_path, results, game, root / "normalized")
            entry["span"] = [raw.index("'SOURCE_UI'"), raw.index("'SOURCE_UI'") + len("'SOURCE_UI'")]
            legacy = extracted / "legacy-manifest.json"
            _write_json(legacy, manifest)
            patch = root / "raw"
            apply_translations(legacy, results, game, patch)
            self.assertIn(b"\r\n", (patch / "www/js/plugins/SyntheticDisplay.js").read_bytes())
            self.assertEqual(verify_patch(legacy, game, patch)["status"], "verified")
            archive = root / "patch.zip"
            pack_patch(patch, archive)
            with zipfile.ZipFile(archive) as z:
                self.assertIn("www/js/plugins/SyntheticDisplay.js", z.namelist())

    def test_multiline_scalar_stays_in_one_field_including_preserved_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _fixture_game(root)
            items = game / "www/data/Items.json"
            _write_json(items, [None, {"id": 1, "name": "ITEM", "description": "SOURCE_A\nSOURCE_B"}])
            extracted = root / "extracted"
            extract_game(game, extracted)
            segment = next(s for s in _rows(extracted / "segments.jsonl")
                           if s["source"] == "SOURCE_A\nSOURCE_B")
            results = root / "results.json"
            _write_json(results, {"results": [_result(segment["id"], "TARGET_A\nTARGET_B")]})
            patch = root / "patch"
            report = apply_translations(extracted / "apply-manifest.json", results, game, patch,
                                        preserve_invalid=False)
            self.assertTrue(report["source_unchanged"])
            self.assertEqual(json.loads((patch / "www/data/Items.json").read_text())[1]["description"],
                             "TARGET_A\nTARGET_B")
            _write_json(results, {"results": []})
            preserved = root / "preserved"
            apply_translations(extracted / "apply-manifest.json", results, game, preserved)
            self.assertEqual(json.loads((preserved / "www/data/Items.json").read_text())[1]["description"],
                             "SOURCE_A\nSOURCE_B")

    def test_external_display_spans_apply_and_reject_unmapped_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = _fixture_game(root)
            (game / 'www/index.html').write_text('<title>SOURCE_TITLE &amp; DETAIL</title><p>IMMUTABLE</p>', encoding='utf-8')
            (game / 'www/js/plugins.js').write_text('var $plugins = [{"name":"SyntheticDisplay","status":true}];', encoding='utf-8')
            plugin = game / 'www/js/plugins/SyntheticDisplay.js'
            plugin.parent.mkdir()
            plugin.write_text("// win.drawText('COMMENT',0);\nwin.drawText('SOURCE_UI',0);\nconst hidden = 'KEY';", encoding='utf-8')
            extracted = root / 'extract'
            extract_game(game, extracted)
            manifest = json.loads((extracted / 'apply-manifest.json').read_text(encoding='utf-8'))
            rows = [r for r in manifest['segments'] if r['file'].endswith(('.html', '.js'))]
            self.assertEqual(len(rows), 2)
            results = root / 'results.json'
            _write_json(results, [{'id': r['id'], 'status': 'success', 'text': 'TARGET <&>' if r['kind'] == 'html_title' else "TARGET 'UI'"} for r in rows])
            patch = root / 'patch'
            apply_translations(extracted / 'apply-manifest.json', results, game, patch)
            self.assertIn('TARGET &lt;&amp;&gt;', (patch / 'www/index.html').read_text(encoding='utf-8'))
            self.assertIn("TARGET \\'UI\\'", (patch / 'www/js/plugins/SyntheticDisplay.js').read_text(encoding='utf-8'))
            patch_script = patch / 'www/js/plugins/SyntheticDisplay.js'
            safe_script = patch_script.read_text(encoding='utf-8')
            patch_script.write_text(safe_script.replace("'TARGET \\'UI\\''", "'TARGET'); attack(); ('UI'"), encoding='utf-8')
            with self.assertRaisesRegex(RpgMakerMvError, 'PATCH_CHANGED_UNMAPPED_DATA'):
                verify_patch(extracted / 'apply-manifest.json', game, patch)
            patch_script.write_text(safe_script, encoding='utf-8')
            (patch / 'www/index.html').write_text('<title>TARGET</title><p>CHANGED</p>', encoding='utf-8')
            with self.assertRaisesRegex(RpgMakerMvError, 'PATCH_CHANGED_UNMAPPED_DATA'):
                verify_patch(extracted / 'apply-manifest.json', game, patch)

    def test_package_main_selects_launch_payload_among_nested_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = _fixture_game(root)
            _fixture_game(game / "www")
            (game / "www" / "index.html").write_text("<html></html>", encoding="utf-8")
            _write_json(game / "package.json", {"main": "www/index.html"})
            self.assertEqual(discover_game(game).data_root, game / "www" / "data")

    def test_package_main_cannot_escape_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game = _fixture_game(Path(temporary))
            _write_json(game / "package.json", {"main": "../outside/index.html"})
            with self.assertRaisesRegex(RpgMakerMvError, "UNSAFE_RUNTIME_MAIN"):
                discover_game(game)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _fixture_game(root: Path, *, version: str = "1.6.1") -> Path:
    game = root / "fixture-game"
    data = game / "www" / "data"
    scripts = game / "www" / "js"
    scripts.mkdir(parents=True)
    (scripts / "rpg_core.js").write_text(
        f"Utils.RPGMAKER_VERSION = '{version}';\n", encoding="utf-8"
    )
    _write_json(
        data / "System.json",
        {
            "gameTitle": "SOURCE_TITLE",
            "currencyUnit": "SOURCE_CURRENCY",
            "terms": {"basic": ["SOURCE_TERM"]},
        },
    )
    _write_json(data / "MapInfos.json", [None, {"id": 1, "name": "SOURCE_MAP_INFO"}])
    _write_json(
        data / "Actors.json",
        [
            None,
            {
                "id": 1,
                "name": "SOURCE_ACTOR",
                "nickname": "SOURCE_NICKNAME",
                "profile": "SOURCE_PROFILE",
            },
        ],
    )
    _write_json(data / "CommonEvents.json", [None])
    _write_json(data / "Troops.json", [None])

    commands: list[dict[str, object]] = []
    for line_count in range(1, 5):
        commands.append({"code": 101, "indent": 0, "parameters": ["", 0, 0, 2]})
        for line_index in range(line_count):
            commands.append(
                {
                    "code": 401,
                    "indent": 0,
                    "parameters": [f"SOURCE_DIALOGUE_{line_count}_{line_index}"],
                }
            )
    commands.extend(
        [
            {"code": 105, "indent": 0, "parameters": [2, False]},
            {"code": 405, "indent": 0, "parameters": ["SOURCE_SCROLL_A"]},
            {"code": 405, "indent": 0, "parameters": ["SOURCE_SCROLL_B"]},
            {
                "code": 102,
                "indent": 0,
                "parameters": [["SOURCE_CHOICE_A", "SOURCE_CHOICE_B"], -1, 0, 2, 0],
            },
            {"code": 402, "indent": 0, "parameters": [0, "SOURCE_CHOICE_A"]},
            {"code": 402, "indent": 0, "parameters": [1, "SOURCE_CHOICE_B"]},
            {"code": 404, "indent": 0, "parameters": []},
            {"code": 356, "indent": 0, "parameters": ["D_TEXT SOURCE_PLUGIN 7"]},
            {
                "code": 355,
                "indent": 0,
                "parameters": [
                    "$gameMessage.add(\"SOURCE_DOUBLE\"); actor.setName('SOURCE_SINGLE');"
                ],
            },
            {"code": 0, "indent": 0, "parameters": []},
        ]
    )
    _write_json(
        data / "Map001.json",
        {
            "displayName": "SOURCE_MAP_DISPLAY",
            "width": 20,
            "height": 15,
            "events": [
                None,
                {
                    "id": 1,
                    "name": "FIXTURE_EVENT",
                    "pages": [{"conditions": {}, "trigger": 0, "list": commands}],
                },
            ],
        },
    )
    return game


def _fixture_static_scope_game(root: Path) -> Path:
    game = _fixture_game(root)
    data = game / "www" / "data"
    system = json.loads((data / "System.json").read_text(encoding="utf-8"))
    system["startMapId"] = 1
    _write_json(data / "System.json", system)
    _write_json(
        data / "MapInfos.json",
        [
            None,
            {"id": 1, "name": "SOURCE_MAP_ONE"},
            {"id": 2, "name": "SOURCE_MAP_TWO"},
            {"id": 3, "name": "SOURCE_MAP_THREE"},
        ],
    )
    _write_json(
        data / "CommonEvents.json",
        [
            None,
            {
                "id": 1,
                "name": "CALLED",
                "trigger": 0,
                "switchId": 0,
                "list": [{"code": 401, "indent": 0, "parameters": ["SOURCE_COMMON_CALLED"]}],
            },
            {
                "id": 2,
                "name": "UNUSED",
                "trigger": 0,
                "switchId": 0,
                "list": [{"code": 401, "indent": 0, "parameters": ["SOURCE_COMMON_UNUSED"]}],
            },
            {
                "id": 3,
                "name": "AUTORUN",
                "trigger": 1,
                "switchId": 1,
                "list": [{"code": 401, "indent": 0, "parameters": ["SOURCE_COMMON_AUTORUN"]}],
            },
        ],
    )

    def map_document(text: str, commands: list[dict[str, object]]) -> dict[str, object]:
        return {
            "displayName": text + "_DISPLAY",
            "width": 20,
            "height": 15,
            "encounterList": [],
            "events": [
                None,
                {
                    "id": 1,
                    "name": "SYNTHETIC_EVENT",
                    "pages": [{"conditions": {}, "trigger": 0, "list": commands}],
                },
            ],
        }

    _write_json(
        data / "Map001.json",
        map_document(
            "SOURCE_MAP_ONE",
            [
                {"code": 401, "indent": 0, "parameters": ["SOURCE_MAP_ONE_DIALOGUE"]},
                {"code": 201, "indent": 0, "parameters": [0, 2, 1, 1, 2, 0]},
                {"code": 117, "indent": 0, "parameters": [1]},
            ],
        ),
    )
    _write_json(
        data / "Map002.json",
        map_document(
            "SOURCE_MAP_TWO",
            [{"code": 401, "indent": 0, "parameters": ["SOURCE_MAP_TWO_DIALOGUE"]}],
        ),
    )
    _write_json(
        data / "Map003.json",
        map_document(
            "SOURCE_MAP_THREE",
            [{"code": 401, "indent": 0, "parameters": ["SOURCE_MAP_THREE_UNUSED"]}],
        ),
    )
    return game


def _rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _result(identifier: str, text: str, status: str = "reviewed") -> dict[str, str]:
    return {"segment_id": identifier, "text": text, "status": status}


def _source_hash(game: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((game / "www" / "data").glob("*.json")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


class RpgMakerMvAdapterTests(unittest.TestCase):
    def test_static_display_scope_excludes_only_independently_unreachable_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _fixture_static_scope_game(root)
            extracted = root / "scope-extracted"
            report = extract_game(game, extracted)
            sources = {str(row["source"]): row for row in _rows(extracted / "segments.jsonl")}

            self.assertIn("SOURCE_MAP_TWO_DIALOGUE", sources)
            self.assertIn("SOURCE_COMMON_CALLED", sources)
            self.assertIn("SOURCE_COMMON_AUTORUN", sources)
            self.assertNotIn("SOURCE_MAP_THREE_UNUSED", sources)
            self.assertNotIn("SOURCE_COMMON_UNUSED", sources)
            self.assertEqual(
                sources["SOURCE_MAP_TWO_DIALOGUE"]["metadata"]["display_scope"]["status"],
                "statically_reachable",
            )
            self.assertEqual(
                sources["SOURCE_TITLE"]["metadata"]["display_scope"]["status"],
                "conservative_retained",
            )
            scope = report["static_scope"]
            self.assertTrue(scope["methods_agree"])
            self.assertGreater(scope["proven_unused_count"], 0)
            self.assertEqual(
                scope["candidate_count"],
                scope["included_count"] + scope["proven_unused_count"],
            )
            attestation = verify_inventory_pair(
                extracted / "primary-inventory.json",
                extracted / "independent-inventory.json",
            )
            self.assertEqual(attestation["status"], "static_verified")

    def test_dynamic_map_reference_retains_other_maps_conservatively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _fixture_static_scope_game(root)
            map_path = game / "www" / "data" / "Map001.json"
            document = json.loads(map_path.read_text(encoding="utf-8"))
            commands = document["events"][1]["pages"][0]["list"]
            transfer = next(row for row in commands if row["code"] == 201)
            transfer["parameters"] = [1, 7, 1, 1, 2, 0]
            _write_json(map_path, document)

            extracted = root / "dynamic-extracted"
            report = extract_game(game, extracted)
            sources = {str(row["source"]): row for row in _rows(extracted / "segments.jsonl")}
            retained = sources["SOURCE_MAP_THREE_UNUSED"]
            self.assertEqual(
                retained["metadata"]["display_scope"]["status"],
                "conservative_retained",
            )
            self.assertGreater(report["static_scope"]["unresolved_dynamic_reference_count"], 0)

    def test_active_plugin_keeps_unproven_graph_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _fixture_static_scope_game(root)
            scripts = game / "www" / "js"
            (scripts / "plugins.js").write_text(
                'var $plugins = [{"name":"SyntheticPlugin","status":true}];',
                encoding="utf-8",
            )
            plugin = scripts / "plugins" / "SyntheticPlugin.js"
            plugin.parent.mkdir()
            plugin.write_text("window.drawText('SOURCE_PLUGIN_UI', 0);", encoding="utf-8")

            extracted = root / "plugin-extracted"
            report = extract_game(game, extracted)
            sources = {str(row["source"]): row for row in _rows(extracted / "segments.jsonl")}
            retained = sources["SOURCE_MAP_THREE_UNUSED"]
            self.assertEqual(
                retained["metadata"]["display_scope"]["status"],
                "conservative_retained",
            )
            self.assertEqual(report["static_scope"]["proven_unused_count"], 0)

    def test_extract_apply_verify_and_pack_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _fixture_game(root)
            extracted = root / "fixture-extracted"
            before = _source_hash(game)

            report = extract_game(game, extracted)
            self.assertEqual(report["version"], "1.6.1")
            self.assertTrue(report["source_unchanged"])
            segments = _rows(extracted / "segments.jsonl")
            by_source = {row["source"]: row for row in segments}

            for line_count in range(1, 5):
                source = "\n".join(
                    f"SOURCE_DIALOGUE_{line_count}_{line_index}" for line_index in range(line_count)
                )
                self.assertEqual(by_source[source]["metadata"]["required_line_count"], line_count)

            non_ascii_target = "\ud14c\uc2a4\ud2b8"
            selected = [
                _result(str(by_source["SOURCE_CHOICE_A"]["id"]), "TARGET_CHOICE_A"),
                _result(str(by_source["SOURCE_PLUGIN"]["id"]), "TARGET_PLUGIN"),
                _result(str(by_source["SOURCE_DOUBLE"]["id"]), non_ascii_target),
                _result(str(by_source["SOURCE_SINGLE"]["id"]), "TARGET_SINGLE"),
            ]
            results = root / "fixture-results.json"
            _write_json(results, {"results": selected})
            patch = root / "fixture-patch"
            build = apply_translations(
                extracted / "apply-manifest.json", results, game, patch
            )
            self.assertTrue(build["source_unchanged"])
            self.assertEqual(_source_hash(game), before)

            localized = json.loads(
                (patch / "www" / "data" / "Map001.json").read_text(encoding="utf-8")
            )
            commands = localized["events"][1]["pages"][0]["list"]
            choice = next(command for command in commands if command["code"] == 102)
            branches = [command for command in commands if command["code"] == 402]
            self.assertEqual(choice["parameters"][0][0], "TARGET_CHOICE_A")
            self.assertEqual(branches[0]["parameters"][1], "TARGET_CHOICE_A")
            plugin = next(command for command in commands if command["code"] == 356)
            self.assertEqual(plugin["parameters"][0], "D_TEXT TARGET_PLUGIN 7")
            script = next(command for command in commands if command["code"] == 355)
            self.assertIn(json.dumps(non_ascii_target, ensure_ascii=False), script["parameters"][0])
            self.assertIn("setName('TARGET_SINGLE')", script["parameters"][0])

            verified = verify_patch(extracted / "apply-manifest.json", game, patch)
            self.assertEqual(verified["status"], "verified")
            archive = root / "fixture-patch.zip"
            packed = pack_patch(patch, archive)
            self.assertEqual(packed["files"], verified["output_files"])
            with zipfile.ZipFile(archive) as bundle:
                self.assertIn("www/data/Map001.json", bundle.namelist())
                self.assertTrue(all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in bundle.infolist()))

    def test_invalid_multiline_result_is_source_preserved_or_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _fixture_game(root)
            extracted = root / "fixture-extracted"
            extract_game(game, extracted)
            segments = _rows(extracted / "segments.jsonl")
            source = "SOURCE_DIALOGUE_2_0\nSOURCE_DIALOGUE_2_1"
            segment = next(row for row in segments if row["source"] == source)
            results = root / "fixture-results.json"
            _write_json(results, {"results": [_result(str(segment["id"]), "TARGET_ONE_LINE")]})

            report = apply_translations(
                extracted / "apply-manifest.json",
                results,
                game,
                root / "fixture-patch",
            )
            self.assertEqual(report["status_counts"]["source_preserved_invalid"], 1)
            localized = json.loads(
                (root / "fixture-patch" / "www" / "data" / "Map001.json").read_text(encoding="utf-8")
            )
            dialogue = [
                command["parameters"][0]
                for command in localized["events"][1]["pages"][0]["list"]
                if command["code"] == 401
            ]
            self.assertIn("SOURCE_DIALOGUE_2_0", dialogue)
            self.assertIn("SOURCE_DIALOGUE_2_1", dialogue)

            with self.assertRaisesRegex(RpgMakerMvError, "INVALID_TRANSLATION"):
                apply_translations(
                    extracted / "apply-manifest.json",
                    results,
                    game,
                    root / "fixture-strict-patch",
                    preserve_invalid=False,
                )

    def test_immutable_baseline_detects_conflicting_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _fixture_game(root)
            extracted = root / "fixture-extracted"
            extract_game(game, extracted)
            manifest_path = extracted / "apply-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            original = next(row for row in manifest["segments"] if row["source"] == "SOURCE_TITLE")
            duplicate = dict(original)
            duplicate["id"] = "mv-synthetic-conflict"
            manifest["segments"].append(duplicate)
            manifest["segment_count"] += 1
            conflict_manifest = extracted / "fixture-conflict-manifest.json"
            _write_json(conflict_manifest, manifest)
            results = root / "fixture-results.json"
            _write_json(
                results,
                {
                    "results": [
                        _result(original["id"], "TARGET_TITLE_A"),
                        _result(duplicate["id"], "TARGET_TITLE_B"),
                    ]
                },
            )
            with self.assertRaisesRegex(RpgMakerMvError, "CONFLICTING_PATH_ASSIGNMENT"):
                apply_translations(
                    conflict_manifest,
                    results,
                    game,
                    root / "fixture-patch",
                )

    def test_verify_rejects_changes_outside_manifest_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _fixture_game(root)
            extracted = root / "fixture-extracted"
            extract_game(game, extracted)
            results = root / "fixture-results.json"
            _write_json(results, {"results": []})
            patch = root / "fixture-patch"
            apply_translations(extracted / "apply-manifest.json", results, game, patch)
            map_path = patch / "www" / "data" / "Map001.json"
            document = json.loads(map_path.read_text(encoding="utf-8"))
            document["width"] = 999
            _write_json(map_path, document)
            with self.assertRaisesRegex(RpgMakerMvError, "PATCH_CHANGED_UNMAPPED_DATA"):
                verify_patch(extracted / "apply-manifest.json", game, patch)

    def test_javascript_literals_round_trip_quote_style_and_escapes(self) -> None:
        value = "TARGET's \\ path\nnext\tcolumn"
        for quote in ('"', "'"):
            encoded = encode_js_literal(value, quote)
            self.assertEqual(encoded[0], quote)
            self.assertEqual(encoded[-1], quote)
            self.assertEqual(decode_js_literal(encoded), value)

    def test_wrong_engine_version_and_source_nested_output_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrong = _fixture_game(root, version="1.6.0")
            with self.assertRaisesRegex(RpgMakerMvError, "ENGINE_VERSION_MISMATCH"):
                discover_game(wrong)

            valid_root = root / "valid"
            valid_root.mkdir()
            game = _fixture_game(valid_root)
            with self.assertRaisesRegex(RpgMakerMvError, "OUTPUT_INSIDE_READ_ONLY_SOURCE"):
                extract_game(game, game / "fixture-output")


if __name__ == "__main__":
    unittest.main()
