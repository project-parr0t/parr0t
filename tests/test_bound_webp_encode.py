from __future__ import annotations

import base64
import struct
import unittest

from game_l10n.bound_webp_encode import _canonical_png, _dimensions
from game_l10n.errors import ConfigurationError
from game_l10n.image_region_pixels import _decode_png
from tests.test_image_localization import rgba_png


class WebpEncoderIntegrityTests(unittest.TestCase):
    def test_rejects_lossy_and_truncated_webp(self):
        lossless = bytes.fromhex('5249464614000000574542505650384c070000002f00000000008800')
        self.assertEqual(_dimensions(lossless), (1, 1))
        with self.assertRaisesRegex(ValueError, 'NOT_LOSSLESS'):
            _dimensions(lossless[:-1])
        lossy = base64.b64decode('UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA')
        with self.assertRaisesRegex(ValueError, 'NOT_LOSSLESS'):
            _dimensions(lossy)

    def test_png_sanitization_preserves_pixels_and_rejects_wrong_dimensions(self):
        original = rgba_png((12, 4))
        canonical = _canonical_png(original, (64, 32))
        self.assertEqual(_decode_png(original, (64, 32)), _decode_png(canonical, (64, 32)))
        self.assertEqual(struct.unpack_from('>II', canonical, 16), (64, 32))
        with self.assertRaisesRegex(ConfigurationError, 'PNG_INVALID'):
            _canonical_png(original, (32, 64))
