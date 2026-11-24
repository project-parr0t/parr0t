"""Compare exact pixels in bound, noninterlaced, 8-bit RGBA PNG assets."""
from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

from .errors import ConfigurationError


_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_RGBA8 = b"\x08\x06\x00\x00\x00"
_MAX_PIXELS = 16_777_216
_MAX_FILE = 128 * 1024 * 1024


def _invalid() -> ConfigurationError:
    return ConfigurationError("E_IMAGE_TEXT_SOURCE_PNG_INVALID")


def _rgba_header(header: bytes) -> bool:
    return (len(header) == 29 and header[:8] == _SIGNATURE
            and header[8:16] == b"\x00\x00\x00\x0dIHDR" and header[24:] == _RGBA8)


def _bound_png(path: Path, digest: str) -> bytes:
    with path.open("rb") as stream:
        body = stream.read(_MAX_FILE + 1)
    if len(body) > _MAX_FILE or hashlib.sha256(body).hexdigest() != digest:
        raise _invalid()
    return body


def _paeth(left: int, up: int, diagonal: int) -> int:
    dl, du, dd = abs(up - diagonal), abs(left - diagonal), abs(left + up - 2 * diagonal)
    return left if dl <= du and dl <= dd else up if du <= dd else diagonal


def _decode_png(body: bytes, dimensions: tuple[int, int]) -> bytearray:
    if not _rgba_header(body[:29]):
        raise _invalid()
    width, height = dimensions
    if (struct.unpack_from(">II", body, 16) != dimensions or not width or not height
            or width * height > _MAX_PIXELS):
        raise _invalid()
    data = memoryview(body)
    inflater = zlib.decompressobj()
    stride = width * 4 + 1
    size = height * stride
    pixels = bytearray()
    offset = 8
    have_data = False
    data_ended = False
    try:
        while offset + 12 <= len(data):
            length, kind = struct.unpack_from(">I4s", data, offset)
            start = offset + 8
            end = start + length
            if end + 4 > len(data) or zlib.crc32(data[offset + 4:end]) != struct.unpack_from(">I", data, end)[0]:
                raise _invalid()
            if kind == b"IDAT":
                if data_ended:
                    raise _invalid()
                have_data = True
                pixels.extend(inflater.decompress(data[start:end], size + 1 - len(pixels)))
                if len(pixels) > size or inflater.unused_data:
                    raise _invalid()
            elif kind == b"IEND":
                if length or end + 4 != len(data) or len(pixels) != size or not inflater.eof:
                    raise _invalid()
                break
            elif (kind in {b"acTL", b"fcTL", b"fdAT", b"tRNS"}
                  or kind == b"IHDR" and offset != 8
                  or kind[0] & 0x20 == 0 and kind not in {b"IHDR", b"PLTE"}):
                raise _invalid()
            elif have_data:
                data_ended = True
            offset = end + 4
        else:
            raise _invalid()
    except zlib.error as exc:
        raise _invalid() from exc

    for y in range(height):
        row = y * stride
        mode = pixels[row]
        if mode > 4:
            raise _invalid()
        if not mode:
            continue
        for column in range(1, stride):
            index = row + column
            left = pixels[index - 4] if column > 4 else 0
            up = pixels[index - stride] if y else 0
            if mode == 1:
                delta = left
            elif mode == 2:
                delta = up
            elif mode == 3:
                delta = (left + up) // 2
            else:
                diagonal = pixels[index - stride - 4] if y and column > 4 else 0
                delta = _paeth(left, up, diagonal)
            pixels[index] = (pixels[index] + delta) & 255
    return pixels


def verify_rgba_png_preservation(
    source: Path, output: Path, source_sha256: str, output_sha256: str,
    dimensions: tuple[int, int], regions: list[list[int]],
) -> None:
    """Independently reject pixel changes outside the verified text regions.

    The native comparison is limited to the exact decoder format. Other PNG
    variants are still handled by the SDK's existing format-specific proof.
    """
    with source.open("rb") as stream:
        if not _rgba_header(stream.read(29)):
            return
    original = _decode_png(_bound_png(source, source_sha256), dimensions)
    replacement = _decode_png(_bound_png(output, output_sha256), dimensions)
    before, after = memoryview(original), memoryview(replacement)
    width, height = dimensions
    stride = width * 4 + 1
    events = []
    for i, (x, y, w, h) in enumerate(regions):
        events.append((y, i, (x, x + w)))
        events.append((y + h, i, None))
    events.sort()
    active = {}
    intervals = []
    event_index = 0
    for row in range(height):
        changed = event_index < len(events) and events[event_index][0] == row
        while event_index < len(events) and events[event_index][0] == row:
            _, key, interval = events[event_index]
            if interval is None:
                del active[key]
            else:
                active[key] = interval
            event_index += 1
        if changed:
            intervals = sorted(active.values())
        start = row * stride + 1
        cursor = 0
        for left, right in intervals:
            if cursor < left and before[start + cursor * 4:start + left * 4] != after[start + cursor * 4:start + left * 4]:
                raise ConfigurationError("E_IMAGE_OUTSIDE_TEXT_REGION_CHANGED")
            cursor = max(cursor, right)
        if before[start + cursor * 4:start + width * 4] != after[start + cursor * 4:start + width * 4]:
            raise ConfigurationError("E_IMAGE_OUTSIDE_TEXT_REGION_CHANGED")
