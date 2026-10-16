"""Pure conservative UI metric screening; never Unity rendering evidence."""
from dataclasses import dataclass
import math
import re
from typing import Hashable, Mapping


@dataclass(frozen=True)
class Rect:
    """A root represents the canvas; child dimensions use anchors and size delta."""
    parent: Hashable | None = None
    anchor_min: tuple[float, float] = (0.0, 0.0)
    anchor_max: tuple[float, float] = (1.0, 1.0)
    size_delta: tuple[float, float] = (0.0, 0.0)


@dataclass(frozen=True)
class FontMetrics:
    """Caller-owned codepoint advances and conservative line height in font units."""
    units_per_em: float
    line_height: float
    advances: Mapping[int, float]
    fallback_advance: float


@dataclass(frozen=True)
class TextLayout:
    font_size: int
    wrap: bool = True
    truncate_vertical: bool = True
    best_fit: bool = False
    best_fit_min: int = 1
    best_fit_max: int = 1


def _positive_pair(value):
    if len(value) != 2 or any(not math.isfinite(x) or x <= 0 for x in value):
        raise ValueError('LAYOUT_DIMENSIONS_INVALID')
    return tuple(value)


def resolve_rect_size(rectangles: Mapping[Hashable, Rect], node_id: Hashable,
                      canvas_size: tuple[float, float]) -> tuple[float, float]:
    """Resolve anchor-stretch sizes at one caller-supplied canvas reference size.

    Rotation, scale, driven layout groups and runtime transforms are excluded.
    Missing parents, cycles and non-positive dimensions are rejected.
    """
    canvas_size = _positive_pair(canvas_size)
    seen = set()
    chain = []
    current = node_id
    while True:
        if current in seen:
            raise ValueError('RECT_HIERARCHY_CYCLE')
        seen.add(current)
        if current not in rectangles:
            raise ValueError('RECT_BINDING_MISSING')
        rect = rectangles[current]
        if any(len(pair) != 2 or any(not math.isfinite(x) for x in pair)
               for pair in (rect.anchor_min, rect.anchor_max, rect.size_delta)):
            raise ValueError('RECT_VALUES_INVALID')
        if rect.parent is None:
            break
        chain.append(rect)
        current = rect.parent
    size = canvas_size
    for rect in reversed(chain):
        size = _positive_pair(tuple(size[i] * (rect.anchor_max[i] - rect.anchor_min[i])
                                    + rect.size_delta[i] for i in range(2)))
    return size


def screen_text(text: str, rect_size: tuple[float, float], metrics: FontMetrics,
                layout: TextLayout, *, strip_rich_text: bool = False) -> dict:
    """Estimate codepoint wrapping, glyph coverage and bounded best-fit sizing.

    Explicit newlines are retained. Optional tag stripping is approximate.
    Shaping, kerning, word wrapping and runtime font ingestion are unverified.
    No font files, Unity objects, paths, providers or global state are used.
    """
    width, height = _positive_pair(rect_size)
    if (any(not math.isfinite(x) or x <= 0 for x in
            (metrics.units_per_em, metrics.line_height))
            or not math.isfinite(metrics.fallback_advance) or metrics.fallback_advance < 0
            or any(not math.isfinite(x) or x < 0 for x in metrics.advances.values())):
        raise ValueError('FONT_METRICS_INVALID')
    sizes = (layout.best_fit_min, layout.best_fit_max) if layout.best_fit else (layout.font_size, layout.font_size)
    if any(type(x) is not int or x <= 0 for x in sizes) or sizes[0] > sizes[1]:
        raise ValueError('FONT_SIZE_INVALID')
    plain = re.sub(r'<[^>]+>', '', text) if strip_rich_text else text
    plain = plain.replace('\r\n', '\n').replace('\r', '\n')
    missing = sorted({ord(ch) for ch in plain if not ch.isspace() and ord(ch) not in metrics.advances})

    def measure(size):
        widths = []
        for paragraph in plain.split('\n'):
            used = 0.0
            for ch in paragraph:
                advance = metrics.advances.get(ord(ch), metrics.fallback_advance) * size / metrics.units_per_em
                if layout.wrap and used and used + advance > width:
                    widths.append(used)
                    used = 0.0
                used += advance
            widths.append(used)
        return {'line_count': len(widths), 'max_line_width': max(widths),
                'conservative_height': len(widths) * metrics.line_height * size / metrics.units_per_em}

    def overflows(measurement):
        return (measurement['max_line_width'] > width + 0.01 or
                (layout.truncate_vertical and measurement['conservative_height'] > height + 0.01))

    selected = sizes[0]
    measurement = measure(selected)
    for size in range(sizes[1], sizes[0] - 1, -1):
        selected, measurement = size, measure(size)
        if not overflows(measurement):
            break
    return {'font_size_tested': selected, 'measurement': measurement,
            'conservative_overflow': overflows(measurement),
            'missing_codepoints': missing, 'rich_markup_present': '<' in text,
            'method': 'conservative_codepoint_metrics', 'runtime_verified': False}
