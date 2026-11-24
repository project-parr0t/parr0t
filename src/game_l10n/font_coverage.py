"""Read only the Unicode cmap of a bound SFNT or WOFF font."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import BinaryIO


_RANGES = {
    "hangul_syllables": range(0xAC00, 0xD7A4),
    "compatibility_jamo": range(0x3131, 0x318F),
    "hiragana": range(0x3041, 0x3097),
    "katakana": range(0x30A1, 0x30FB),
    "digits": range(0x30, 0x3A),
    "ui_symbols": tuple(map(ord, "$%+−¥₩※♪…→←♡♥！？。、\u300c\u300d\u300e\u300f")),
}


def _read_at(stream: BinaryIO, offset: int, size: int) -> bytes:
    stream.seek(offset)
    result = stream.read(size)
    if len(result) != size:
        raise ValueError("truncated font table")
    return result


def _cmap(path: Path) -> bytes:
    with path.open("rb") as stream:
        header = _read_at(stream, 0, 12)
        kind = header[:4]
        if kind == b"wOFF":
            count = struct.unpack(">H", _read_at(stream, 12, 2))[0]
            offset, entry_format = 44, ">4sIIII"
        elif kind in {b"OTTO", b"\x00\x01\x00\x00", b"true"}:
            count = struct.unpack_from(">H", header, 4)[0]
            offset, entry_format = 12, ">4sIII"
        else:
            raise ValueError("unsupported font container")
        directory = _read_at(stream, offset, count * struct.calcsize(entry_format))
        for entry in struct.iter_unpack(entry_format, directory):
            if entry[0] != b"cmap":
                continue
            if kind == b"wOFF":
                _, table_at, stored, size, _ = entry
            else:
                _, _, table_at, size = entry
                stored = size
            if not 0 < stored <= size <= 16 * 1024 * 1024:
                raise ValueError("invalid cmap size")
            raw = _read_at(stream, table_at, stored)
            if stored < size:
                decoder = zlib.decompressobj()
                raw = decoder.decompress(raw, size + 1)
                if len(raw) != size or not decoder.eof or decoder.unused_data:
                    raise ValueError("invalid WOFF cmap data")
            return raw
    raise ValueError("font has no cmap")


def _supported(cmap: bytes, wanted: set[int]) -> set[int]:
    found: set[int] = set()
    version, count = struct.unpack_from(">HH", cmap)
    directory_end = 4 + count * 8
    if version or directory_end > len(cmap):
        raise ValueError("invalid cmap directory")
    formats = set()
    offsets = {offset for platform, encoding, offset in struct.iter_unpack(
        ">HHI", memoryview(cmap)[4:directory_end],
    ) if platform == 0 or (platform == 3 and encoding in {1, 10})}
    for start in offsets:
        if start < directory_end:
            raise ValueError("invalid cmap subtable offset")
        fmt = struct.unpack_from(">H", cmap, start)[0]
        if fmt == 12:
            size, groups = struct.unpack_from(">III", cmap, start + 4)[::2]
            if size != 16 + 12 * groups or start + size > len(cmap):
                raise ValueError("truncated cmap groups")
            formats.add(fmt)
            previous_high = -1
            for low, high, glyph in struct.iter_unpack(">III", memoryview(cmap)[start + 16:start + size]):
                if not previous_high < low <= high <= 0x10FFFF or glyph + high - low > 0xFFFF:
                    raise ValueError("invalid cmap group")
                previous_high = high
                found.update(wanted.intersection(range(low + (glyph == 0), high + 1)))
        elif fmt == 4:
            size, _, double_count = struct.unpack_from(">HHH", cmap, start + 2)
            if not double_count or double_count % 2 or size < 16 + 4 * double_count or start + size > len(cmap):
                raise ValueError("invalid cmap segments")
            formats.add(fmt)
            table = memoryview(cmap)[start:start + size]
            ends = 14
            starts = ends + double_count + 2
            deltas = starts + double_count
            ranges = deltas + double_count
            previous_high = -1
            for relative in range(0, double_count, 2):
                low = struct.unpack_from(">H", table, starts + relative)[0]
                high = struct.unpack_from(">H", table, ends + relative)[0]
                if not previous_high < low <= high:
                    raise ValueError("invalid cmap segment bounds")
                previous_high = high
                delta = struct.unpack_from(">h", table, deltas + relative)[0]
                word = ranges + relative
                shift = struct.unpack_from(">H", table, word)[0]
                if shift and (word + shift < ranges + double_count or word + shift + 2 * (high - low + 1) > size):
                    raise ValueError("invalid cmap glyph offset")
                for cp in wanted.intersection(range(low, high + 1)):
                    glyph = struct.unpack_from(">H", table, word + shift + 2 * (cp - low))[0] if shift else cp
                    if (not shift or glyph) and (glyph + delta) & 0xFFFF:
                        found.add(cp)
    if not formats:
        raise ValueError("font has no supported Unicode cmap")
    return found


def _font_glyph_coverage(path: Path) -> dict[str, dict[str, int]]:
    wanted = set().union(*_RANGES.values())
    covered = _supported(_cmap(path), wanted)
    return {role: {"supported": sum(cp in covered for cp in points), "total": len(points)}
            for role, points in _RANGES.items()}
