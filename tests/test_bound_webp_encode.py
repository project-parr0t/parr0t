from __future__ import annotations

import base64
import os
import struct
import tempfile
import unittest
from pathlib import Path

from game_l10n.bound_webp_encode import _canonical_png, _check_pixels, _dimensions, _read_webp
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

    def test_pixel_check_requires_unchanged_alpha_and_unchanged_pixels_outside_regions(self):
        source = bytearray([10, 20, 30, 255] * 4)
        edited = bytearray(source)
        edited[4] = 210
        _check_pixels(source, edited, 2, [[1, 0, 1, 1]])
        with self.assertRaisesRegex(ValueError, 'OUTSIDE_TEXT_CHANGED'):
            _check_pixels(source, edited, 2, [[1, 1, 1, 1]])
        edited[7] = 10
        with self.assertRaisesRegex(ValueError, 'ALPHA_CHANGED'):
            _check_pixels(source, edited, 2, [[1, 0, 1, 1]])

    @unittest.skipUnless(os.name == 'nt', 'uses Windows WIC')
    def test_stock_decoder_reads_a_single_bound_pixel(self):
        source = base64.b64decode('UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA')
        with tempfile.TemporaryDirectory() as temp:
            pixels = _read_webp(source, (1, 1), {'SYSTEMROOT': os.environ['SYSTEMROOT']}, Path(temp))
            self.assertEqual((len(pixels), pixels[3]), (4, 255))
            with self.assertRaisesRegex(ValueError, 'DECODE_FAILED'):
                _read_webp(source, (2, 1), {'SYSTEMROOT': os.environ['SYSTEMROOT']}, Path(temp))
