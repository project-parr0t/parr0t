import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.rpg_maker_mv.adapter import (
    RpgMakerMvError,
    _Extractor,
    apply_translations,
    extract_bound_external_text,
    extract_game,
    verify_patch,
)
from tests.test_rpg_maker_mv_adapter import _fixture_game, _result, _write_json


class BoundLiteralTests(unittest.TestCase):
    def test_indirect_and_concatenated_literals_keep_exact_mappings(self) -> None:
        text = (
            'var labels={entry:"SOURCE_LABEL"}; '
            'win.drawText(labels.entry + "SOURCE_SUFFIX",0,0);'
        )
        bindings = []
        source_hash = hashlib.sha256(text.encode()).hexdigest()
        for value in ('"SOURCE_LABEL"', '"SOURCE_SUFFIX"'):
            start = text.index(value)
            bindings.append(
                {
                    "file": "js/plugins/Synthetic.js",
                    "span": [start, start + len(value)],
                    "text_sha256": source_hash,
                }
            )
        extractor = _Extractor({})
        extract_bound_external_text(
            extractor,
            {"js/plugins/Synthetic.js": text},
            bindings,
        )
        self.assertEqual(
            [row["source"] for row in extractor.segments],
            ["SOURCE_LABEL", "SOURCE_SUFFIX"],
        )
        self.assertTrue(
            all(
                mapping["kind"] == "script_display_text"
                and mapping["paths"] == [["text"]]
                for mapping in extractor.mappings
            )
        )
        self.assertEqual(len({row["id"] for row in extractor.segments}), 2)
        with self.assertRaisesRegex(RpgMakerMvError, "BOUND_EXTERNAL_SOURCE_CHANGED"):
            extract_bound_external_text(
                _Extractor({}),
                {"js/plugins/Synthetic.js": text + " "},
                bindings,
            )

    def test_invalid_span_is_rejected(self) -> None:
        text = '"SOURCE"'
        with self.assertRaisesRegex(RpgMakerMvError, "INVALID_SPAN_MAPPING"):
            extract_bound_external_text(
                _Extractor({}),
                {"fixture.js": text},
                [
                    {
                        "file": "fixture.js",
                        "span": [-1, 3],
                        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    }
                ],
            )

    def test_bound_indirect_literal_applies_and_verifies_without_code_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _fixture_game(root)
            (game / "www/js/plugins.js").write_text(
                'var $plugins = [{"name":"SyntheticDisplay","status":true}];',
                encoding="utf8",
            )
            plugin = game / "www/js/plugins/SyntheticDisplay.js"
            plugin.parent.mkdir()
            text = (
                'var labels={entry:"SOURCE_INDIRECT"}; '
                "win.drawText(labels.entry,0,0);"
                'win.drawText("SOURCE_DIRECT",0,0);'
            )
            plugin.write_text(text, encoding="utf8")
            output = root / "extract"
            extract_game(game, output)

            literal = '"SOURCE_INDIRECT"'
            start = text.index(literal)
            extractor = _Extractor({})
            extract_bound_external_text(
                extractor,
                {"js/plugins/SyntheticDisplay.js": text},
                [
                    {
                        "file": "js/plugins/SyntheticDisplay.js",
                        "span": [start, start + len(literal)],
                        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    }
                ],
            )
            manifest_path = output / "apply-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf8"))
            manifest["segments"].extend(extractor.mappings)
            manifest["segment_count"] += 1
            _write_json(manifest_path, manifest)

            results_path = root / "results.json"
            _write_json(
                results_path,
                {"results": [_result(extractor.segments[0]["id"], "TARGET_INDIRECT")]},
            )
            patch_root = root / "patch"
            apply_translations(manifest_path, results_path, game, patch_root)
            patch = patch_root / "www/js/plugins/SyntheticDisplay.js"
            self.assertIn('entry:"TARGET_INDIRECT"', patch.read_text(encoding="utf8"))
            verify_patch(manifest_path, game, patch_root)

            patch.write_text(
                patch.read_text(encoding="utf8").replace("labels.entry", "labels.other"),
                encoding="utf8",
            )
            with self.assertRaisesRegex(RpgMakerMvError, "PATCH_CHANGED_UNMAPPED_DATA"):
                verify_patch(manifest_path, game, patch_root)
