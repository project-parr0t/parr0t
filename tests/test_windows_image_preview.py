import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest

from game_l10n.store import stable_hash
from game_l10n.windows_image_preview import _bind_preview_sheets, _render, _select_preview_ordinals
from tests.test_image_localization import rgba_png


class PreviewBindingTests(unittest.TestCase):
    def test_only_selected_sheet_ordinals_are_bound_to_the_exact_candidate_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            task_id = "a" * 64
            folder = root / ".game-l10n/data-plane/windows-image-preview" / task_id
            folder.mkdir(parents=True)
            assets = [{"path": "one", "sha256": "b" * 64}, {"path": "two", "sha256": "c" * 64}]
            names = ["sheet-0001.png", "sheet-0002.png"]
            image = rgba_png()
            (folder / names[1]).write_bytes(image)
            index = folder / "index.json"
            index.write_text(json.dumps({
                "schema": "parr0t.windows-image-inspection-preview.v1", "task_id": task_id,
                "asset_binding": stable_hash(assets), "sheets": names,
                "assets": [{"ordinal": i, "sheet": name, "status": "previewed"} for i, name in enumerate(names, 1)],
            }), encoding="utf-8")
            binding = _bind_preview_sheets(root, index, [2], assets)
            self.assertEqual(binding["sheets"], [{
                "path": (folder / names[1]).relative_to(root).as_posix(),
                "sha256": hashlib.sha256(image).hexdigest(), "asset_ordinals": [2],
            }])
            with self.assertRaisesRegex(ValueError, "BINDING_INVALID"):
                _bind_preview_sheets(root, index, [2], list(reversed(assets)))
            with self.assertRaisesRegex(ValueError, "BINDING_INVALID"):
                _select_preview_ordinals(binding, [1])

    def test_continuation_binds_only_the_requested_ordinal_from_a_shared_sheet(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            task_id = "a" * 64
            folder = root / ".game-l10n/data-plane/windows-image-preview" / task_id
            folder.mkdir(parents=True)
            assets = [{"path": str(n), "sha256": "b" * 64} for n in (1, 2)]
            name = "sheet-0001.png"
            (folder / name).write_bytes(rgba_png())
            index = folder / "index.json"
            index.write_text(json.dumps({
                "schema": "parr0t.windows-image-inspection-preview.v1", "task_id": task_id,
                "asset_binding": stable_hash(assets), "sheets": [name],
                "assets": [{"ordinal": i, "sheet": name, "status": "previewed"} for i in (1, 2)],
            }), encoding="utf-8")
            binding = _select_preview_ordinals(_bind_preview_sheets(root, index, [1], assets), [2])
            self.assertEqual(binding["sheets"][0]["asset_ordinals"], [2])
            self.assertEqual(binding["asset_count"], 1)


@unittest.skipUnless(os.name == 'nt', 'uses Windows WIC')
class WindowsImagePreviewTests(unittest.TestCase):
    def test_renders_exact_png_and_webp_and_marks_changed_inputs(self):
        webp = base64.b64decode('UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            png_path, webp_path = root / 'fixture.png', root / 'fixture.webp'
            png_path.write_bytes(rgba_png((8, 4)))
            webp_path.write_bytes(webp)
            assets = [{'path': str(path), 'ordinal': index, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                      for index, path in enumerate((png_path, webp_path), 1)]
            assets.append({**assets[0], 'ordinal': 3, 'sha256': '0' * 64})
            result = _render(root, assets)
            self.assertEqual(result['sheets'], ['sheet-0001.png'])
            self.assertEqual([(row['status'], row.get('width'), row.get('height')) for row in result['assets']],
                             [('previewed', 64, 32), ('previewed', 1, 1), ('changed', None, None)])
            sheet = (root / result['sheets'][0]).read_bytes()
            self.assertEqual(sheet[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(struct.unpack_from('>II', sheet, 16), (1170, 840))

    def test_original_scale_preview_keeps_native_dimensions_and_rejects_changed_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "fixture.png"
            body = rgba_png((8, 4))
            path.write_bytes(body)
            assets = [{"path": str(path), "ordinal": 7, "sha256": hashlib.sha256(body).hexdigest()},
                      {"path": str(path), "ordinal": 8, "sha256": "0" * 64}]
            rows = _render(root, assets, original_scale=True)["assets"]
            self.assertEqual([row["status"] for row in rows], ["original_scale", "changed"])
            self.assertEqual(struct.unpack_from(">II", (root / rows[0]["file"]).read_bytes(), 16), (64, 32))
            self.assertNotIn("file", rows[1])
