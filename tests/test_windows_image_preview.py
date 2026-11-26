import base64
import hashlib
import os
from pathlib import Path
import struct
import tempfile
import unittest

from game_l10n.windows_image_preview import _render
from tests.test_image_localization import rgba_png


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
