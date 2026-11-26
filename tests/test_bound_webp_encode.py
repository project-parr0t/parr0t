from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path

from game_l10n.bound_webp_encode import _canonical_png, _check_pixels, _dimensions, _png, _read_webp, _retry_inputs
from game_l10n.errors import ConfigurationError
from game_l10n.image_region_pixels import _decode_png
from game_l10n.store import stable_hash


class WebpEncoderIntegrityTests(unittest.TestCase):
    def test_rejects_lossy_and_truncated_webp(self):
        lossless = bytes.fromhex('5249464614000000574542505650384c070000002f00000000008800')
        self.assertEqual(_dimensions(lossless), (1, 1))
        with self.assertRaisesRegex(ValueError, 'NOT_LOSSLESS'):
            _dimensions(lossless[:-1])
        lossy = base64.b64decode('UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA')
        with self.assertRaisesRegex(ValueError, 'NOT_LOSSLESS'):
            _dimensions(lossy)

    def test_staged_png_is_rejected_before_encoding_if_pixels_escape_the_region(self):
        dimensions = (2, 2)
        row = bytes([0, 10, 20, 30, 255, 10, 20, 30, 255])
        staged = bytearray(row * 2)
        original = bytearray([10, 20, 30, 255] * 4)
        staged[14] = 90
        body = _png(staged, dimensions)
        result = _canonical_png(body, dimensions, original, [[1, 1, 1, 1]])
        self.assertEqual(_decode_png(result, dimensions), staged)
        self.assertEqual(struct.unpack_from('>II', result, 16), dimensions)
        with self.assertRaisesRegex(ValueError, 'OUTSIDE_TEXT_CHANGED'):
            _canonical_png(body, dimensions, original, [[0, 1, 1, 1]])
        with self.assertRaisesRegex(ConfigurationError, 'PNG_INVALID'):
            _canonical_png(body, (1, 2), original, [[1, 1, 1, 1]])

    def test_retry_preserves_exact_alpha_but_rejects_outside_region_edits(self):
        dimensions = (2, 2)
        scanlines = bytearray([0, 10, 20, 30, 70, 11, 21, 31, 80,
                              0, 12, 22, 32, 90, 13, 23, 33, 100])
        original = bytearray(scanlines[1:9] + scanlines[10:])
        staged = bytearray(scanlines)
        staged[14], staged[17] = 190, 255
        regions = [[1, 1, 1, 1]]
        with self.assertRaisesRegex(ValueError, "ALPHA_CHANGED"):
            _canonical_png(_png(staged, dimensions), dimensions, original, regions)
        encoded = _canonical_png(_png(staged, dimensions), dimensions, original, regions, preserve_alpha=True)
        scanlines[14] = 190
        self.assertEqual(_decode_png(encoded, dimensions), scanlines)
        staged[4] = 71
        with self.assertRaisesRegex(ValueError, "OUTSIDE_TEXT_CHANGED"):
            _canonical_png(_png(staged, dimensions), dimensions, original, regions, preserve_alpha=True)

    def test_retry_requires_unchanged_archived_inventory_and_same_model(self):
        with tempfile.TemporaryDirectory() as root:
            work = Path(root).resolve()
            stage = work / "fixture-stage"
            inventory = work / "inventory.json"
            body = b'{"assets": [{"id": "fixture", "format": "webp"}]}'
            inventory.write_bytes(body)
            old = {"worker_role": "image_localizer", "image_draft_scope": True, "control_generation": 1,
                   "language_binding": {"target_language": "ko"}, "runtime_model": "fixture-model",
                   "source_roots": [], "output_image_root": "fixture-stage/image-assets",
                   "input_image_inventory": "inventory.json",
                   "input_image_inventory_sha256": hashlib.sha256(body).hexdigest()}
            task_id = stable_hash(old)
            history = stage / "history" / task_id
            history.mkdir(parents=True)
            (history / "task.json").write_text(json.dumps({**old, "task_id": task_id}), encoding="utf-8")
            (history / "receipt.json").write_text(json.dumps({
                "task_id": task_id, "status": "error", "execution": {"complete": True},
            }), encoding="utf-8")
            reused = _retry_inputs(work, stage, old, task_id)
            self.assertEqual(reused, {"fixture": ({"id": "fixture", "format": "webp"},
                                                  stage / "image-assets" / "asset-0001.png")})
            with self.assertRaisesRegex(ValueError, 'RETRY_NOT_BOUND'):
                _retry_inputs(work, stage, {**old, "runtime_model": "different-model"}, task_id)
            inventory.write_bytes(body + b"\n")
            with self.assertRaisesRegex(ValueError, 'INVENTORY_CHANGED'):
                _retry_inputs(work, stage, old, task_id)

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
