"""Synthetic v22 assets with real PPtrs, CLR layouts and inline SDF texels."""
from dataclasses import replace
from pathlib import Path
import struct
import sys
import unittest

TOOLS = Path(__file__).resolve().parents[1] / 'profiles/sdk-unity-2022.3/tools'
sys.path.insert(0, str(TOOLS))
import unity_raw as reader
from unity_objects_fixture import node, serialized, string, unityfs
from unity_tmp import (StaticTmpFace, StaticTmpFont, StaticTmpGlyph, StaticTmpPage,
                       TmpRendererBinding, apply_static_tmp_font, validate_tmp_renderer)

A, OMEGA, MUSIC, STAR = 0x41, 0x3a9, 0x266b, 0x2605
ASSEMBLY = 'Unity.TextMeshPro'
BINDING = TmpRendererBinding(1, 30, 60)


def ptr(pid, fid=0):
    return struct.pack('<iq', fid, pid)


def array(items):
    return struct.pack('<i', len(items)) + b''.join(items)


def named(name):
    return dict(kind='named', assembly=ASSEMBLY, name=name)


def primitive(name):
    return dict(kind='primitive', name=name)


def fields(*definitions):
    return [dict(name=name, type=spec, serialized=True, static=False) for name, spec in definitions]


def rows(element):
    return dict(kind='list', element=element)


def reference(name):
    return dict(kind='pptr', name=name)


def layouts():
    i, u, f = primitive('int32'), primitive('uint32'), primitive('float32')
    rect = fields(*[(name, i) for name in ('m_X', 'm_Y', 'm_Width', 'm_Height')])
    metrics = fields(*[(name, f) for name in ('m_Width', 'm_Height', 'm_HorizontalBearingX',
                                             'm_HorizontalBearingY', 'm_HorizontalAdvance')])
    face = fields(('m_PointSize', i), ('m_UnitsPerEM', i), *[(name, f) for name in (
        'm_Scale', 'm_LineHeight', 'm_AscentLine', 'm_CapLine', 'm_MeanLine', 'm_Baseline',
        'm_DescentLine', 'm_TabWidth')])
    glyph = fields(('m_Index', u), ('m_Metrics', named('Wire.Metrics')), ('m_GlyphRect', named('Wire.Rect')),
                   ('m_Scale', f), ('m_AtlasIndex', i), ('m_ClassDefinitionType', i))
    char = fields(('m_ElementType', i), ('m_Unicode', u), ('m_GlyphIndex', u), ('m_Scale', f))
    renderer = fields(('m_text', {'kind': 'string'}), ('m_fontAsset', reference('TMPro.TMP_FontAsset')),
        ('m_sharedMaterial', reference('UnityEngine.Material')), ('m_fontStyle', i), ('m_fontWeight', i),
        ('opaque', {'kind': 'unsupported'}))
    font = fields(('material', reference('UnityEngine.Material')), ('m_FaceInfo', named('Wire.Face')),
        ('m_AtlasPopulationMode', named('Wire.Mode')), ('m_AtlasWidth', i), ('m_AtlasHeight', i),
        ('m_AtlasPadding', i), ('m_AtlasTextures', rows(reference('UnityEngine.Texture2D'))),
        ('m_AtlasTextureIndex', i), ('m_IsMultiAtlasTexturesEnabled', primitive('bool')),
        ('m_GlyphTable', rows(named('Wire.Glyph'))), ('m_CharacterTable', rows(named('Wire.Character'))),
        ('m_UsedGlyphRects', rows(named('Wire.Rect'))), ('m_FreeGlyphRects', rows(named('Wire.Rect'))),
        ('m_FallbackFontAssetTable', rows(reference('TMPro.TMP_FontAsset'))), ('untouched', u))
    definitions = {'Wire.Rect': rect, 'Wire.Metrics': metrics, 'Wire.Face': face, 'Wire.Glyph': glyph,
        'Wire.Character': char, 'TMPro.TextMeshProUGUI': renderer, 'TMPro.TMP_FontAsset': font}
    return {**{ASSEMBLY + '::' + name: {'fields': value} for name, value in definitions.items()},
            ASSEMBLY + '::Wire.Mode': {'enum': True}}


def prefix(script):
    return ptr(0) + b'\x01NAT' + ptr(script) + string('SYNTHETIC', b'~')


def script(name):
    return string(name) + bytes(20) + string(name) + string('TMPro') + string(ASSEMBLY + '.dll')


def renderer(material):
    return (prefix(10) + string('DISPLAY', b'?') + ptr(30) + ptr(material) + struct.pack('<ii', 0, 400)
            + b'UNKNOWN-RENDERER-TAIL' + ptr(30) + ptr(material))


def font(material, texture, cp, glyph, *, fallback=(), character_glyph=None, mode=0):
    face = struct.pack('<2i8f', 24, 1024, 1, 29, 23, 20, 15, 0, -6, 10)
    glyph_data = struct.pack('<I5f4ifii', glyph, 2, 2, 0, 2, 3, 2, 3, 2, 2, 1, 0, 0)
    char = struct.pack('<iIIf', 1, cp, glyph if character_glyph is None else character_glyph, 1)
    return (prefix(11) + ptr(material) + face + struct.pack('<4i', mode, 8, 8, 1) + array([ptr(texture)])
            + struct.pack('<i', 0) + b'\x01PAD' + array([glyph_data]) + array([char]) + array([]) + array([])
            + array(list(fallback)) + b'KEEP')


def pointer_tree(level, ty, name):
    return [node(level, f'PPtr<{ty}>', name, 12), node(level + 1, 'int', 'm_FileID', 4),
            node(level + 1, 'SInt64', 'm_PathID', 8)]


def map_tree(name, value):
    return [node(2, 'map', name), node(3, 'Array', 'Array', flags=1), node(4, 'int', 'size', 4),
            node(4, 'pair', 'data'), node(5, 'string', 'first'), *value]


def material_tree():
    return [node(0, 'Material', 'Base'), node(1, 'string', 'm_Name'), *pointer_tree(1, 'Shader', 'm_Shader'),
            node(1, 'UnityPropertySheet', 'm_SavedProperties'),
            *map_tree('m_TexEnvs', [node(5, 'UnityTexEnv', 'second'), *pointer_tree(6, 'Texture', 'm_Texture')]),
            *map_tree('m_Floats', [node(5, 'float', 'second', 4)]), node(1, 'UInt32', 'untouched', 4)]


def material(texture):
    texmap = array([string('_Other') + ptr(52), string('_MainTex') + ptr(texture)])
    props = array([string(name) + struct.pack('<f', value) for name, value in (
        ('_TextureWidth', 8), ('_OtherFloat', 87), ('_TextureHeight', 8), ('_GradientScale', 2))])
    return string('SYNTHETIC', b'~') + ptr(70) + texmap + props + b'MATL'


def texture_tree():
    return [node(0, 'Texture2D', 'Base'), node(1, 'string', 'm_Name'),
            *[node(1, 'int', name, 4) for name in ('m_Width', 'm_Height', 'm_CompleteImageSize',
              'm_TextureFormat', 'm_MipCount', 'm_ImageCount', 'm_TextureDimension')],
            node(1, 'TypelessData', 'image data', meta=0x4000),
            node(1, 'StreamingInfo', 'm_StreamData'), node(2, 'UInt64', 'offset', 8),
            node(2, 'UInt32', 'size', 4), node(2, 'string', 'path'), node(1, 'UInt32', 'untouched', 4)]


def alpha(width, height, x, y):
    pixels = bytearray(width * height)
    pixels[y * width + x] = 224
    return bytes(pixels)


def texture(pixels):
    return (string('SYNTHETIC', b'~') + struct.pack('<8i', 8, 8, 64, 1, 1, 1, 2, len(pixels)) + pixels
            + b'!' * ((-len(pixels)) % 4) + bytes(12) + string('') + b'PIXL')


def fixture(*, image=None, wrong_material=False, no_glyph=False, child_pointer=None, loop=False, externals=()):
    ink = alpha(8, 8, 2, 3)
    return serialized([(114, []), (115, []), (28, texture_tree()), (21, material_tree()),
                       (48, []), (900, [])], [
        (1, 0, renderer(62 if wrong_material else 60)), (2, 0, renderer(60)),
        (10, 1, script('TextMeshProUGUI')), (11, 1, script('TMP_FontAsset')),
        (30, 0, font(60, 50, A, 12, fallback=(child_pointer or ptr(31),), mode=1)),
        (31, 0, font(61, 51, OMEGA, 608, fallback=(ptr(30),) if loop else (),
                     character_glyph=989 if no_glyph else None)),
        (32, 0, font(62, 52, MUSIC, 301)),
        (50, 2, texture(ink)), (51, 2, texture(ink if image is None else image)), (52, 2, texture(ink)),
        (60, 3, material(50)), (61, 3, material(51)), (62, 3, material(52)),
        (70, 4, b'SYNTHETIC-OPAQUE-SHADER'), (90, 5, b'UNOWNED-OBJECT')], externals=externals)


def static_font():
    pages = tuple(StaticTmpPage(w, h, alpha(w, h, 3, 2),
                   (StaticTmpGlyph(cp, 3, 2, 1, 1, 0, 1, 2),))
                   for w, h, cp in ((7, 7, OMEGA), (9, 8, STAR)))
    return StaticTmpFont(StaticTmpFace(32, 38, 30, -8, 25, 20, units_per_em=2048), 2, pages)


def object_bytes(raw):
    return {obj['path_id']: raw[obj['offset']:obj['offset'] + obj['size']]
            for obj in reader.serialized(raw)['objects']}


class OfflineTmpTests(unittest.TestCase):
    def test_renderer_glyph_index_lookup_and_ordered_real_fallback(self):
        raw = fixture()
        report = validate_tmp_renderer(raw, BINDING, [OMEGA, A, MUSIC, A], managed_types=layouts())
        self.assertEqual(report.missing, (MUSIC,))
        self.assertEqual(report.font_search_order, ((None, 30), (None, 31)))
        self.assertEqual(report.dynamic_fonts, ((None, 30),))
        self.assertEqual([(g.codepoint, g.glyph_index, g.font, g.atlas, g.material, g.has_ink) for g in report.glyphs],
                         [(A, 12, (None, 30), (None, 50), (None, 60), True),
                          (OMEGA, 608, (None, 31), (None, 51), (None, 61), True)])
        with self.assertRaisesRegex(reader.FormatError, 'TMP_RENDERER_BINDING_MISMATCH'):
            validate_tmp_renderer(raw, replace(BINDING, font_object_id=32), [MUSIC], managed_types=layouts())

    def test_character_table_is_not_glyph_or_pixel_evidence(self):
        for raw, reason in ((fixture(no_glyph=True), 'TMP_CHARACTER_GLYPH_MISSING'),
                            (fixture(image=b''), 'TMP_ATLAS_PIXELS_MISSING'),
                            (fixture(image=bytes(64)), 'TMP_GLYPH_PIXELS_MISSING'),
                            (fixture(image=alpha(8, 8, 7, 7)), 'TMP_GLYPH_PIXELS_MISSING')):
            with self.subTest(reason=reason), self.assertRaisesRegex(reader.FormatError, reason):
                validate_tmp_renderer(raw, BINDING, [OMEGA], managed_types=layouts())

    def test_renderer_material_must_point_to_the_primary_atlas(self):
        raw = fixture(wrong_material=True)
        bound = replace(BINDING, material_object_id=62)
        with self.assertRaisesRegex(reader.FormatError, 'TMP_MATERIAL_ATLAS_MISMATCH'):
            validate_tmp_renderer(raw, bound, [OMEGA], managed_types=layouts())
        with self.assertRaisesRegex(reader.FormatError, 'TMP_MATERIAL_ATLAS_MISMATCH'):
            apply_static_tmp_font(raw, bound, static_font(), managed_types=layouts())

    def test_cyclic_and_detached_fallbacks_are_not_reported_as_missing(self):
        with self.assertRaisesRegex(reader.FormatError, 'TMP_FALLBACK_CYCLE'):
            validate_tmp_renderer(fixture(loop=True), BINDING, [A], managed_types=layouts())
        detached = fixture(child_pointer=ptr(31, 1), externals=('detached.assets',))
        with self.assertRaisesRegex(reader.FormatError, 'TMP_EXTERNAL_REFERENCE_UNSUPPORTED'):
            validate_tmp_renderer(detached, BINDING, [A], managed_types=layouts())

    def test_static_application_is_renderer_local_and_preserves_unowned_bytes(self):
        raw = fixture()
        types = layouts()
        out = apply_static_tmp_font(raw, BINDING, static_font(), managed_types=types)
        old, new = object_bytes(raw), object_bytes(out)
        self.assertEqual(set(new) - set(old), set(range(91, 97)))
        self.assertEqual({k: new[k] for k in old if k != 1}, {k: v for k, v in old.items() if k != 1})
        owned = len(prefix(10) + string('DISPLAY', b'?'))
        self.assertEqual(new[1], old[1][:owned] + ptr(91) + ptr(93) + old[1][owned + 24:])
        for fid, tid, mid in ((91, 92, 93), (94, 95, 96)):
            self.assertTrue(new[fid].endswith(b'KEEP'))
            self.assertIn(b'\0PAD', new[fid])
            self.assertTrue(new[tid].endswith(b'PIXL'))
            self.assertTrue(new[mid].endswith(b'MATL'))
            self.assertIn(string('_Other') + ptr(52), new[mid])
        bound = TmpRendererBinding(1, 91, 93)
        report = validate_tmp_renderer(out, bound, [A, OMEGA, STAR], managed_types=types)
        self.assertEqual(report.missing, ())
        self.assertEqual([g.font for g in report.glyphs], [(None, 30), (None, 91), (None, 94)])
        self.assertEqual([g.atlas for g in report.glyphs], [(None, 50), (None, 92), (None, 95)])
        other = validate_tmp_renderer(out, replace(BINDING, object_id=2), [STAR, OMEGA], managed_types=types)
        self.assertEqual(other.missing, (STAR,))
        self.assertEqual(other.glyphs[0].font, (None, 31))

    def test_bundle_fallback_resolves_exact_entry_and_application_preserves_sibling(self):
        source = fixture(child_pointer=ptr(31, 1), externals=('archive:/SYNTHETIC/peer.assets',))
        peer = fixture()
        raw = unityfs([('main.assets', source, 4), ('peer.assets', peer, 4), ('opaque.resS', b'KEEP', 0)])
        bound = replace(BINDING, container_entry='main.assets')
        before = validate_tmp_renderer(raw, bound, [OMEGA], managed_types=layouts())
        self.assertEqual(before.glyphs[0].font, ('peer.assets', 31))
        out = apply_static_tmp_font(raw, bound, static_font(), managed_types=layouts())
        archive = reader.bundle(out)
        entries = {e['name']: archive['payload'][e['offset']:e['offset'] + e['size']] for e in archive['entries']}
        self.assertEqual(entries['peer.assets'], peer)
        self.assertEqual(entries['opaque.resS'], b'KEEP')
        report = validate_tmp_renderer(out, replace(bound, font_object_id=91, material_object_id=93),
                                       [OMEGA], managed_types=layouts())
        self.assertEqual(report.glyphs[0].font, ('main.assets', 91))

    def test_caller_static_page_requires_real_ink(self):
        font = static_font()
        first, second = font.pages
        bad = replace(font, pages=(replace(first, pixels=bytes(len(first.pixels))), second))
        with self.assertRaisesRegex(reader.FormatError, 'TMP_STATIC_GLYPH_PIXELS_MISSING'):
            apply_static_tmp_font(fixture(), BINDING, bad, managed_types=layouts())


if __name__ == '__main__':
    unittest.main()
