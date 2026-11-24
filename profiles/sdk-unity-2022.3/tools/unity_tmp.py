"""Offline TextMeshPro coverage and static font application for Unity 2022.3.

Only serialized renderer/font routes are proven. No global TMP settings, rich
text font switches, runtime glyph generation, or shader execution are inferred.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import struct
import unicodedata

import unity_raw as reader
from unity_tmp_wire import Container, FormatError, pointer, pptr, replace_fields, require_references

_RENDERERS = frozenset({'TMPro.TextMeshPro', 'TMPro.TextMeshProUGUI'})
_FONT = frozenset({'TMPro.TMP_FontAsset'})
_FONT_FIELD = 'm_fontAsset'
_RENDERER_MATERIAL = 'm_sharedMaterial'
_RENDERER_FIELDS = {_FONT_FIELD, _RENDERER_MATERIAL, 'm_fontMaterial',
                    'm_fontSharedMaterials', 'm_fontMaterials', 'm_fontStyle', 'm_fontWeight'}
_FALLBACK_FIELDS = ('m_FallbackFontAssetTable', 'fallbackFontAssets')
_FACE_FIELDS = {
    'm_PointSize': ('i', 'point_size'), 'm_UnitsPerEM': ('i', 'units_per_em'),
    'm_Scale': ('f', 'scale'), 'm_LineHeight': ('f', 'line_height'),
    'm_AscentLine': ('f', 'ascent_line'), 'm_CapLine': ('f', 'cap_line'),
    'm_MeanLine': ('f', 'mean_line'), 'm_Baseline': ('f', 'baseline'),
    'm_DescentLine': ('f', 'descent_line'),
}
_FACE_SCALED_FIELDS = ('m_SuperscriptOffset', 'm_SubscriptOffset', 'm_UnderlineOffset',
                       'm_UnderlineThickness', 'm_StrikethroughOffset',
                       'm_StrikethroughThickness', 'm_TabWidth')
_RECT_FIELDS = ('m_X', 'm_Y', 'm_Width', 'm_Height')
_Asset = tuple[str | None, int]


@dataclass(frozen=True, slots=True)
class TmpRendererBinding:
    """Physical renderer and its expected wire PPtrs; zero material uses font default."""
    object_id: int
    font_object_id: int
    material_object_id: int
    container_entry: str | None = None
    font_file_id: int = 0
    material_file_id: int = 0


@dataclass(frozen=True, slots=True)
class TmpGlyphProof:
    codepoint: int
    font: _Asset
    glyph_index: int
    atlas: _Asset
    material: _Asset
    rect: tuple[int, int, int, int]
    has_ink: bool


@dataclass(frozen=True, slots=True)
class TmpCoverage:
    glyphs: tuple[TmpGlyphProof, ...]
    missing: tuple[int, ...]
    font_search_order: tuple[_Asset, ...]
    dynamic_fonts: tuple[_Asset, ...]


@dataclass(frozen=True, slots=True)
class StaticTmpGlyph:
    codepoint: int
    x: int
    y: int
    width: int
    height: int
    bearing_x: float
    bearing_y: float
    advance: float


@dataclass(frozen=True, slots=True)
class StaticTmpPage:
    """Alpha8 SDF pixels, stored bottom row first as in a Unity Texture2D."""
    width: int
    height: int
    pixels: bytes
    glyphs: tuple[StaticTmpGlyph, ...]


@dataclass(frozen=True, slots=True)
class StaticTmpFace:
    point_size: int
    line_height: float
    ascent_line: float
    descent_line: float
    cap_line: float
    mean_line: float
    baseline: float = 0.0
    scale: float = 1.0
    units_per_em: int | None = None


@dataclass(frozen=True, slots=True)
class StaticTmpFont:
    """Caller-supplied, approved raster data. One single-atlas font per page."""
    face: StaticTmpFace
    padding: int
    pages: tuple[StaticTmpPage, ...]


@dataclass(slots=True)
class _Font:
    key: _Asset
    value: dict
    wire: tuple | None
    children: tuple[_Asset, ...]
    atlases: list
    material: _Asset
    glyphs: dict
    characters: dict
    mode: int


def _require(condition, reason):
    if not condition:
        raise FormatError(reason)


def _codepoint(value):
    return type(value) is int and 0 <= value <= 0x10ffff and not 0xd800 <= value <= 0xdfff


def _integer(value):
    return type(value) is int


def _float(value):
    return type(value) in (int, float) and -3.402823466e38 <= value <= 3.402823466e38 and math.isfinite(value)


def _dict(value, reason):
    _require(isinstance(value, dict), reason)
    return value


def _list(value, reason):
    _require(isinstance(value, list), reason)
    return value


def _uint(value):
    return _integer(value) and 0 <= value <= 0xffffffff


def _can_be_blank(cp):
    return chr(cp).isspace() or unicodedata.category(chr(cp)) == 'Cf'


def _has_ink(pixels, width, rect):
    x, y, w, h = rect
    return any(max(pixels[row * width + x:row * width + x + w], default=0) >= 128
               for row in range(y, y + h))


def _rect_ok(rect, width, height):
    return (all(_integer(v) and v >= 0 for v in rect) and rect[0] + rect[2] <= width
            and rect[1] + rect[3] <= height and bool(rect[2]) == bool(rect[3]))


def _indexed(rows, field, predicate, reason):
    result = {}
    for row in _list(rows, reason):
        key = _dict(row, reason).get(field)
        _require(predicate(key) and key not in result, reason)
        result[key] = row
    return result


def _material_property(value, group, name):
    reason = 'TMP_MATERIAL_PROPERTY_MISSING_OR_AMBIGUOUS'
    saved = _dict(value.get('m_SavedProperties'), reason)
    rows = _list(saved.get(group), reason)
    matches = [(i, row.get('second')) for i, row in enumerate(rows)
               if _dict(row, reason).get('first') == name and 'second' in row]
    _require(len(matches) == 1, reason)
    (index, prop), = matches
    return ('m_SavedProperties', group, index, 'second'), prop


def _measure(value):
    width, height = value.get('m_Width'), value.get('m_Height')
    _require(all(_integer(n) and 0 < n <= 16384 for n in (width, height)),
             'TMP_ATLAS_DIMENSIONS_INVALID')
    return width, height


def _atlas_data(value):
    stream = _dict(value.get('m_StreamData'), 'TMP_ATLAS_STREAM_LAYOUT_UNSUPPORTED')
    _require(not any(stream.get(k) for k in ('offset', 'size', 'path')),
             'TMP_ATLAS_STREAM_UNSUPPORTED')
    _require(not value.get('m_PlatformBlob') and not value.get('m_IsPreProcessed') and
             not value.get('m_MipsStripped'), 'TMP_ATLAS_PLATFORM_LAYOUT_UNSUPPORTED')
    pixels = value.get('image data')
    _require(isinstance(pixels, bytes), 'TMP_ATLAS_PIXELS_MISSING')
    return pixels


def _atlas_pixels(value):
    width, height = _measure(value)
    _require((value.get('m_TextureFormat'), value.get('m_MipCount'), value.get('m_ImageCount'),
              value.get('m_TextureDimension')) == (1, 1, 1, 2), 'TMP_ATLAS_FORMAT_UNSUPPORTED')
    pixels = _atlas_data(value)
    _require(len(pixels) == width * height == value.get('m_CompleteImageSize'),
             'TMP_ATLAS_PIXELS_MISSING')
    return width, height, pixels


def _check_font_atlas(font, width, height):
    _require((font.value.get('m_AtlasWidth'), font.value.get('m_AtlasHeight')) == (width, height),
             'TMP_FONT_ATLAS_DIMENSIONS_MISMATCH')
    padding = font.value.get('m_AtlasPadding')
    _require(_integer(padding) and padding > 0, 'TMP_FONT_ATLAS_PADDING_INVALID')
    return padding


def _check_material(container, key, atlas, width, height, padding):
    data = container.native_data(key, 21)
    value = data['value']
    shader = value.get('m_Shader')
    path, main = _material_property(value, 'm_TexEnvs', '_MainTex')
    env = _dict(main, 'TMP_MATERIAL_TEXTURE_INVALID')
    require_references(data, [(('m_Shader',), shader), ((*path, 'm_Texture'), env.get('m_Texture'))])
    container.target(key, shader, 48)
    _require(container.target(key, env.get('m_Texture'), 28) == atlas, 'TMP_MATERIAL_ATLAS_MISMATCH')
    for name, expected in (('m_Scale', (1, 1)), ('m_Offset', (0, 0))):
        if name in env:
            vector = _dict(env[name], 'TMP_MATERIAL_UV_UNSUPPORTED')
            _require((vector.get('x'), vector.get('y')) == expected, 'TMP_MATERIAL_UV_UNSUPPORTED')
    for prop, expected in (('_TextureWidth', width), ('_TextureHeight', height),
                           ('_GradientScale', padding + 1)):
        _, actual = _material_property(value, 'm_Floats', prop)
        _require(_float(actual) and actual == expected, 'TMP_MATERIAL_ATLAS_METRICS_MISMATCH')
    return value


def _font(container, key):
    data, wire = container.behaviour(key, _FONT)
    value = data['value']
    mode = value.get('m_AtlasPopulationMode')
    if isinstance(mode, dict):
        mode = mode.get('value__')
    _require(_integer(mode) and mode in (0, 1, 2), 'TMP_ATLAS_POPULATION_MODE_UNSUPPORTED')
    fallback_names = [field for field in _FALLBACK_FIELDS if field in value]
    fallback_lists = [_list(value[field], 'TMP_FALLBACK_LAYOUT_UNSUPPORTED') for field in fallback_names]
    _require(bool(fallback_lists) and all(not refs or refs == fallback_lists[0] for refs in fallback_lists[1:]),
             'TMP_FALLBACK_LAYOUT_UNSUPPORTED')
    children = tuple(target for ref in fallback_lists[0]
                     if (target := container.target(key, ref, 114, nullable=True)) is not None)
    atlases = _list(value.get('m_AtlasTextures'), 'TMP_ATLAS_LAYOUT_UNSUPPORTED')
    _require(bool(atlases), 'TMP_ATLAS_REFERENCE_MISSING')
    refs = [(('material',), value.get('material'))]
    refs.extend(((fallback_names[0], i), ref) for i, ref in enumerate(fallback_lists[0]))
    refs.extend((('m_AtlasTextures', i), ref) for i, ref in enumerate(atlases))
    if 'm_AtlasTexture' in value:
        refs.append((('m_AtlasTexture',), value['m_AtlasTexture']))
    require_references(data, refs)
    for ref in atlases:
        container.target(key, ref, 28, nullable=True)
    if 'm_AtlasTexture' in value:
        legacy = container.target(key, value['m_AtlasTexture'], 28, nullable=True)
        _require(legacy is None or legacy == container.target(key, atlases[0], 28),
                 'TMP_LEGACY_ATLAS_MISMATCH')
    material = container.target(key, value.get('material'), 21)
    glyphs = _indexed(value.get('m_GlyphTable'), 'm_Index', _uint, 'TMP_GLYPH_TABLE_INVALID')
    characters = _indexed(value.get('m_CharacterTable'), 'm_Unicode', _codepoint, 'TMP_CHARACTER_TABLE_INVALID')
    return _Font(key, value, wire, children, atlases, material, glyphs, characters, mode)


def _binding(container, binding):
    _require(isinstance(binding, TmpRendererBinding) and _integer(binding.object_id) and
             binding.object_id != 0 and _integer(binding.font_object_id) and binding.font_object_id != 0 and
             _integer(binding.material_object_id) and _integer(binding.font_file_id) and
             _integer(binding.material_file_id) and (binding.container_entry is None or
             isinstance(binding.container_entry, str)), 'TMP_RENDERER_BINDING_INVALID')
    key = binding.container_entry, binding.object_id
    data, wire = container.behaviour(key, _RENDERERS, allow_prefix=True)
    fields = data['value']
    if data.get('pending_field') is not None:
        declared = {field['name'] for field in reader.managed_fields(container.types, wire[0])}
        _require((_RENDERER_FIELDS & declared) <= fields.keys(), 'TMP_RENDERER_LAYOUT_UNSUPPORTED')
    font, material = fields.get(_FONT_FIELD), fields.get(_RENDERER_MATERIAL)
    require_references(data, [((_FONT_FIELD,), font), ((_RENDERER_MATERIAL,), material)])
    _require(pointer(font) == (binding.font_file_id, binding.font_object_id) and
             pointer(material) == (binding.material_file_id, binding.material_object_id),
             'TMP_RENDERER_BINDING_MISMATCH')
    atlas_font = container.target(key, font, 114)
    shared = container.target(key, material, 21, nullable=True)
    if 'm_fontMaterial' in fields:
        instance = container.target(key, fields['m_fontMaterial'], 21, nullable=True)
        _require(instance is None or instance == shared, 'TMP_RENDERER_MATERIAL_OVERRIDE_UNSUPPORTED')
    for name in ('m_fontSharedMaterials', 'm_fontMaterials'):
        if name in fields:
            _require(not any(container.target(key, ref, 21, nullable=True)
                             for ref in _list(fields[name], 'TMP_RENDERER_MATERIAL_OVERRIDE_UNSUPPORTED')),
                     'TMP_RENDERER_MATERIAL_OVERRIDE_UNSUPPORTED')
    style, weight = fields.get('m_fontStyle', 0), fields.get('m_fontWeight', 400)
    _require(_integer(style) and _integer(weight) and weight in range(100, 1000, 100),
             'TMP_RENDERER_STYLE_UNSUPPORTED')
    return key, data, atlas_font, shared, bool(style & 2), 7 if style & 1 else weight // 100


def _fonts(container, root, italic, weight):
    """Iterative depth-first search, rejecting cycles even on unused branches."""
    found, active, pending = {}, set(), [(root, False)]
    while pending:
        key, leave = pending.pop()
        if leave:
            active.remove(key)
            continue
        _require(key not in active, 'TMP_FALLBACK_CYCLE')
        if key in found:
            continue
        active.add(key)
        font = found[key] = _font(container, key)
        for field in ('m_FontWeightTable', 'fontWeights'):
            if field in font.value:
                table = _list(font.value[field], 'TMP_FONT_WEIGHT_LAYOUT_UNSUPPORTED')
                if weight < len(table):
                    pair = _dict(table[weight], 'TMP_FONT_WEIGHT_LAYOUT_UNSUPPORTED')
                    alternate = pair.get('italicTypeface' if italic else 'regularTypeface')
                    _require(alternate is not None and pointer(alternate) == (0, 0),
                             'TMP_ALTERNATE_TYPEFACE_UNSUPPORTED')
        pending.append((key, True))
        pending.extend((child, False) for child in reversed(font.children))
    return found


def _check_glyph(font, character):
    index = character.get('m_GlyphIndex')
    _require(_uint(index) and index != 0 and (_integer(character.get('m_ElementType', 1)) and
             character.get('m_ElementType', 1) == 1), 'TMP_CHARACTER_GLYPH_INVALID')
    glyph = font.glyphs.get(index)
    _require(glyph is not None, 'TMP_CHARACTER_GLYPH_MISSING')
    atlas_index = glyph.get('m_AtlasIndex')
    _require(_integer(atlas_index) and 0 <= atlas_index < len(font.atlases), 'TMP_GLYPH_ATLAS_INDEX_INVALID')
    # TMP generates a new material at runtime for non-zero atlas indices.
    _require(atlas_index == 0, 'TMP_ATLAS_PAGE_MATERIAL_UNSUPPORTED')
    rect = _dict(glyph.get('m_GlyphRect'), 'TMP_GLYPH_RECT_INVALID')
    for row in (character, glyph):
        scale = row.get('m_Scale')
        _require(_float(scale) and scale > 0, 'TMP_GLYPH_SCALE_INVALID')
    metrics = _dict(glyph.get('m_Metrics'), 'TMP_GLYPH_METRICS_INVALID')
    for name in ('m_Width', 'm_Height', 'm_HorizontalBearingX', 'm_HorizontalBearingY', 'm_HorizontalAdvance'):
        value = metrics.get(name)
        _require(_float(value) and (name not in ('m_Width', 'm_Height') or value >= 0),
                 'TMP_GLYPH_METRICS_INVALID')
    return index, tuple(rect.get(k) for k in _RECT_FIELDS)


def _renderer_material(container, root, shared):
    atlas = container.target(root.key, root.atlases[0], 28)
    width, height = _measure(container.native(atlas, 28))
    padding = _check_font_atlas(root, width, height)
    checked = set()
    for key in {root.material, shared or root.material}:
        _check_material(container, key, atlas, width, height, padding)
        checked.add((key, atlas, padding))
    return atlas, checked


def _scan(container, bound, codepoints):
    _, _, root, shared, italic, weight = bound
    fonts = _fonts(container, root, italic, weight)
    _, checked_materials = _renderer_material(container, fonts[root], shared)
    glyphs, missing, atlas_cache = [], [], {}
    for cp in codepoints:
        for font in fonts.values():
            if cp in font.characters:
                break
        else:
            missing.append(cp)
            continue
        index, rect = _check_glyph(font, font.characters[cp])
        atlas = container.target(font.key, font.atlases[0], 28)
        if atlas not in atlas_cache:
            width, height, pixels = _atlas_pixels(container.native(atlas, 28))
            atlas_cache[atlas] = width, height, memoryview(pixels)
        width, height, pixels = atlas_cache[atlas]
        padding = _check_font_atlas(font, width, height)
        material = (shared or font.material) if font.key == root else font.material
        for key in (font.material, material):
            signature = key, atlas, padding
            if signature not in checked_materials:
                _check_material(container, key, atlas, width, height, padding)
                checked_materials.add(signature)
        _require(_rect_ok(rect, width, height), 'TMP_GLYPH_RECT_INVALID')
        ink = _has_ink(pixels, width, rect)
        _require(ink or _can_be_blank(cp), 'TMP_GLYPH_PIXELS_MISSING')
        glyphs.append(TmpGlyphProof(cp, font.key, index, atlas, material, rect, ink))
    return TmpCoverage(tuple(glyphs), tuple(missing), tuple(fonts),
                       tuple(key for key, font in fonts.items() if font.mode != 0))


def validate_tmp_renderer(raw: bytes, binding: TmpRendererBinding, codepoints, *, managed_types=None) -> TmpCoverage:
    """Validate scalar codepoints in this renderer's ordered serialized font route.

    Missing entries are reported separately from malformed/unsupported evidence
    (FormatError). Coverage of dynamic assets proves existing serialized glyphs
    only, not fallback behavior of a player that can generate new glyphs.
    Proofs identify serialized materials, not runtime-created material variants.
    """
    points = set()
    for cp in codepoints:
        _require(_codepoint(cp), 'TMP_CODEPOINT_INVALID')
        points.add(cp)
    container = Container(raw, managed_types)
    return _scan(container, _binding(container, binding), sorted(points))


def _check_static(font):
    _require(isinstance(font, StaticTmpFont) and isinstance(font.face, StaticTmpFace) and
             _integer(font.padding) and 0 < font.padding < 16384 and isinstance(font.pages, tuple) and
             bool(font.pages), 'TMP_STATIC_FONT_INVALID')
    face = font.face
    _require(_integer(face.point_size) and 0 < face.point_size <= 0x7fffffff and
             (face.units_per_em is None or (_integer(face.units_per_em) and 0 < face.units_per_em <= 0xffff)) and
             all(_float(getattr(face, name)) for fmt, name in _FACE_FIELDS.values() if fmt == 'f') and
             face.scale > 0 and face.line_height > 0 and face.ascent_line > face.descent_line,
             'TMP_STATIC_FACE_INVALID')
    points = {}
    for i, page in enumerate(font.pages):
        _require(isinstance(page, StaticTmpPage) and all(_integer(n) and 0 < n <= 16384
                 for n in (page.width, page.height)) and isinstance(page.pixels, bytes) and
                 len(page.pixels) == page.width * page.height and isinstance(page.glyphs, tuple) and
                 bool(page.glyphs), 'TMP_STATIC_PAGE_INVALID')
        rows, pixels, pad = {}, memoryview(page.pixels), font.padding
        for glyph in page.glyphs:
            _require(isinstance(glyph, StaticTmpGlyph) and _codepoint(glyph.codepoint) and
                     glyph.codepoint != 0 and glyph.codepoint not in points and
                     all(_float(v) for v in (glyph.bearing_x, glyph.bearing_y, glyph.advance)),
                     'TMP_STATIC_GLYPH_INVALID')
            rect = glyph.x, glyph.y, glyph.width, glyph.height
            _require(_rect_ok(rect, page.width, page.height), 'TMP_STATIC_GLYPH_RECT_INVALID')
            if glyph.width:
                x, y, w, h = _padded_rect(glyph, pad)
                _require(_rect_ok((x, y, w, h), page.width, page.height), 'TMP_STATIC_GLYPH_PADDING_INVALID')
                bits = ((1 << w) - 1) << x
                for row in range(y, y + h):
                    prior = rows.get(row, 0)
                    _require(not prior & bits, 'TMP_STATIC_GLYPH_RECTS_OVERLAP')
                    rows[row] = prior | bits
            _require(_can_be_blank(glyph.codepoint) or _has_ink(pixels, page.width, rect),
                     'TMP_STATIC_GLYPH_PIXELS_MISSING')
            points[glyph.codepoint] = i, rect
    return points


def _padded_rect(glyph, pad):
    return glyph.x - pad, glyph.y - pad, glyph.width + 2 * pad, glyph.height + 2 * pad


def _table(rows):
    return struct.pack('<i', len(rows)) + b''.join(rows), None


def _scalar(fmt, value):
    data = struct.pack('<' + fmt, value)
    return data, len(data)


def _font_changes(font, asset, page, texture, material, fallbacks):
    value = asset.value
    glyphs = sorted(page.glyphs, key=lambda g: g.codepoint)
    changes = {
        ('material',): (pptr(material), 12),
        ('m_AtlasPopulationMode',): _scalar('i', 0),
        ('m_AtlasPadding',): _scalar('i', font.padding),
        ('m_AtlasWidth',): _scalar('i', page.width),
        ('m_AtlasHeight',): _scalar('i', page.height),
        ('m_AtlasTextures',): _table([pptr(texture)]),
        ('m_CharacterTable',): _table([struct.pack('<iIIf', 1, g.codepoint, g.codepoint, 1.0) for g in glyphs]),
        ('m_GlyphTable',): _table([struct.pack('<I5f4ifii', g.codepoint, g.width, g.height, g.bearing_x,
             g.bearing_y, g.advance, g.x, g.y, g.width, g.height, 1.0, 0, 0) for g in glyphs]),
    }
    for field in ('m_UsedGlyphRects', 'm_FreeGlyphRects', 'm_GlyphInfoList'):
        if field in value:
            changes[(field,)] = _table([struct.pack('<4i', *_padded_rect(g, font.padding))
                                       for g in glyphs if g.width] if field == 'm_UsedGlyphRects' else [])
    for field in ('m_IsMultiAtlasTexturesEnabled', 'm_ClearDynamicDataOnBuild'):
        if field in value:
            changes[(field,)] = _scalar('B', 0)
    if 'm_AtlasTextureIndex' in value:
        changes[('m_AtlasTextureIndex',)] = _scalar('i', 0)
    if 'm_AtlasTexture' in value:
        changes[('m_AtlasTexture',)] = pptr(texture), 12
    first, *other = [field for field in _FALLBACK_FIELDS if field in value]
    changes[(first,)] = _table([pptr(pid) for pid in fallbacks])
    for field in other:
        changes[(field,)] = _table([])
    old_face = _dict(value.get('m_FaceInfo'), 'TMP_STATIC_FACE_LAYOUT_UNSUPPORTED')
    for field, (fmt, attr) in _FACE_FIELDS.items():
        number = getattr(font.face, attr)
        if field == 'm_UnitsPerEM' and number is None:
            continue
        _require(field in old_face, 'TMP_STATIC_FACE_LAYOUT_UNSUPPORTED')
        changes[('m_FaceInfo', field)] = _scalar(fmt, number)
    old_size = old_face['m_PointSize']
    _require(_integer(old_size) and old_size > 0, 'TMP_STATIC_TEMPLATE_POINT_SIZE_INVALID')
    for field in _FACE_SCALED_FIELDS:
        if field in old_face:
            number = old_face[field]
            _require(_float(number) and _float(number * font.face.point_size / old_size),
                     'TMP_STATIC_TEMPLATE_FACE_METRIC_INVALID')
            changes[('m_FaceInfo', field)] = _scalar('f', number * font.face.point_size / old_size)
    for parent, field in (('m_FontFeatureTable', 'm_GlyphPairAdjustmentRecords'),
                          ('m_KerningTable', 'kerningPairs')):
        if isinstance(value.get(parent), dict) and field in value[parent]:
            changes[(parent, field)] = _table([])
    return changes


def _texture_changes(value, page):
    pixels = _atlas_data(value)
    changes = {('image data',): (struct.pack('<i', len(page.pixels)) + page.pixels, 4 + len(pixels))}
    for name, number in (('m_Width', page.width), ('m_Height', page.height),
                         ('m_CompleteImageSize', len(page.pixels)), ('m_TextureFormat', 1),
                         ('m_MipCount', 1), ('m_ImageCount', 1), ('m_TextureDimension', 2)):
        changes[(name,)] = _scalar('i', number)
    return changes


def _material_changes(value, page, padding, texture):
    path, _ = _material_property(value, 'm_TexEnvs', '_MainTex')
    changes = {(*path, 'm_Texture'): (pptr(texture), 12)}
    for name, number in (('_TextureWidth', page.width), ('_TextureHeight', page.height),
                         ('_GradientScale', padding + 1)):
        path, _ = _material_property(value, 'm_Floats', name)
        changes[path] = _scalar('f', number)
    return changes


def apply_static_tmp_font(raw: bytes, binding: TmpRendererBinding, font: StaticTmpFont, *, managed_types=None) -> bytes:
    """Clone static font/material/atlas objects and rebind exactly this renderer.

    Additional pages are ordered fallbacks; the original font is the last
    fallback. Original assets and other renderer bytes remain untouched. Each
    cloned template must be in the renderer's serialized file, possibly within
    a UnityFS bundle. Does not read, fetch, or approve font files.
    """
    expected = _check_static(font)
    container = Container(raw, managed_types)
    key, data, root, shared, italic, weight = _binding(container, binding)
    primary = _fonts(container, root, italic, weight)[root]
    entry, renderer_id = key
    texture_key, _ = _renderer_material(container, primary, shared)
    material_key = shared or primary.material
    _require(all(asset[0] == entry for asset in (root, texture_key, material_key)),
             'TMP_STATIC_CROSS_ENTRY_TEMPLATE_UNSUPPORTED')
    texture = container.native(texture_key, 28)
    material = container.native(material_key, 21)
    objects = container.entries[entry][2]
    next_id = max(0, max(objects)) + 1
    _require(next_id + 3 * len(font.pages) - 1 <= 0x7fffffffffffffff, 'TMP_STATIC_OBJECT_ID_EXHAUSTED')
    ids = [(pid, pid + 1, pid + 2) for pid in range(next_id, next_id + 3 * len(font.pages), 3)]
    additions = []
    for i, (page, (font_id, texture_id, material_id)) in enumerate(zip(font.pages, ids, strict=True)):
        fallback_ids = [*(row[0] for row in ids[1:]), root[1]] if i == 0 else []
        font_body = container.rewrite(root, 114, _font_changes(font, primary, page, texture_id,
                                                              material_id, fallback_ids), primary.wire)
        texture_body = container.rewrite(texture_key, 28, _texture_changes(texture, page))
        material_body = container.rewrite(material_key, 21, _material_changes(material, page, font.padding, texture_id))
        additions.extend(((font_id, objects[root[1]]['type_index'], font_body),
                          (texture_id, objects[texture_key[1]]['type_index'], texture_body),
                          (material_id, objects[material_key[1]]['type_index'], material_body)))
    payload, obj, _ = container.object(key, 114)
    pointers = {_FONT_FIELD: ids[0][0], _RENDERER_MATERIAL: ids[0][2]}
    instance = data['value'].get('m_fontMaterial')
    if instance is not None and pointer(instance) != (0, 0):
        pointers['m_fontMaterial'] = ids[0][2]
    renderer = replace_fields(payload, obj, data['references'], pointers)
    result = container.append(entry, renderer_id, renderer, additions)
    check = Container(result, managed_types)
    bound = _binding(check, TmpRendererBinding(renderer_id, ids[0][0], ids[0][2], entry))
    report = _scan(check, bound, sorted(expected))
    _require(not report.missing, 'TMP_STATIC_COVERAGE_ROUNDTRIP_FAILED')
    for glyph in report.glyphs:
        page, rect = expected[glyph.codepoint]
        fid, tid, mid = ids[page]
        _require(glyph.glyph_index == glyph.codepoint and
                 (glyph.font, glyph.atlas, glyph.material, glyph.rect) == ((entry, fid), (entry, tid), (entry, mid), rect),
                 'TMP_STATIC_GLYPH_ROUNDTRIP_FAILED')
    for page, (fid, tid, mid) in zip(font.pages, ids, strict=True):
        value = _font(check, (entry, fid))
        children = tuple((entry, f) for f, _, _ in ids[1:]) + (root,) if fid == next_id else ()
        _require(value.mode == 0 and value.children == children and value.material == (entry, mid) and
                 len(value.characters) == len(value.glyphs) == len(page.glyphs) and len(value.atlases) == 1 and
                 not any(value.value.get(name) for name in ('m_AtlasTextureIndex', 'm_IsMultiAtlasTexturesEnabled',
                                                           'm_ClearDynamicDataOnBuild')) and
                 check.native((entry, tid), 28)['image data'] == page.pixels,
                 'TMP_STATIC_ASSET_ROUNDTRIP_FAILED')
        face = value.value['m_FaceInfo']
        for field, (fmt, attr) in _FACE_FIELDS.items():
            number = getattr(font.face, attr)
            if number is not None:
                expected_number, = struct.unpack('<' + fmt, struct.pack('<' + fmt, number))
                _require(face.get(field) == expected_number, 'TMP_STATIC_FACE_ROUNDTRIP_FAILED')
    return result
