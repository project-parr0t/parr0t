"""Synthetic metric bytes and synthetic font objects; no third-party font data."""
import copy
import importlib.util
import unittest
from tests.unity_6000_4_fixtures import font_members, font_trees, sfnt
import unity_fonts as sdk
from unity_serialized import environment, metadata


class FontGroupTests(unittest.TestCase):
    def test_dynamic_group_preserves_templates_and_wires_source_material_atlas(self):
        templates = font_trees()
        before = copy.deepcopy(templates)
        result = sdk.build_group(templates,sfnt(),names=['Synthetic Target','Regular'],
            label='SYNTHETIC_DYNAMIC',ids=[100,101,102,103],repertoire=[66,65],space_advance=500)
        source,tmp,material,texture = result
        self.assertEqual(templates,before)
        self.assertEqual(source['m_FontData'],sfnt())
        self.assertEqual(tmp['m_SourceFontFile'],sdk.pointer(100))
        self.assertEqual(tmp['m_Material'],sdk.pointer(102))
        self.assertEqual(tmp['m_AtlasTextures'],[sdk.pointer(103)])
        self.assertEqual(material['m_SavedProperties']['m_TexEnvs'][0][1]['m_Texture'],sdk.pointer(103))
        self.assertEqual(material['m_Shader'],templates[2]['m_Shader'])
        self.assertEqual(tmp['m_CreationSettings']['characterSequence'],'65,66')
        self.assertEqual(tmp['m_GlyphTable'],[])
        self.assertEqual((texture['m_Width'],texture['m_Height']),(1,1))

    def test_bad_metric_boundaries_and_atlas_templates_fail(self):
        for raw in [b'',sfnt()[:50],b'SYNTHETIC_NOT_SFNT']:
            with self.subTest(raw=raw),self.assertRaises(ValueError): sdk.native_metrics(raw)
        trees = font_trees()
        trees[3]['m_StreamData']['path']='SYNTHETIC_EXTERNAL'
        with self.assertRaisesRegex(ValueError,'DYNAMIC_ATLAS_STREAM_NOT_EMPTY'):
            sdk.build_group(trees,sfnt(),names=['Synthetic Target','Regular'],label='SYNTHETIC',
                ids=[100,101,102,103],repertoire=[65],space_advance=500)


@unittest.skipUnless(importlib.util.find_spec('UnityPy') is not None,'Install pinned private UnityPy')
class FontIntegrationTests(unittest.TestCase):
    def test_reader_clone_supports_the_pinned_slotted_unitypy_layout(self):
        members,group = font_members()
        original = environment(members['synthetic-a']).file.objects[group['template_ids'][0]]
        clone = sdk._clone_object_reader(original, 9001)
        self.assertIsNot(clone, original)
        self.assertEqual(clone.path_id, 9001)
        self.assertEqual(original.path_id, group['template_ids'][0])
        self.assertIs(clone.assets_file, original.assets_file)
        self.assertIs(clone.reader, original.reader)

    def test_real_serialized_clone_external_and_fallback_roundtrip(self):
        members,group = font_members()
        before = {name:environment(raw).file for name,raw in members.items()}
        result = sdk.integrate(members,[group],[dict(member='synthetic-b',object_id=50,group=0)])
        a,b = environment(result['synthetic-a']).file,environment(result['synthetic-b']).file
        self.assertEqual(set(a.objects),set(before['synthetic-a'].objects)|{100,101,102,103})
        for oid,obj in before['synthetic-a'].objects.items():
            self.assertEqual(obj.get_raw_data(),a.objects[oid].get_raw_data())
        self.assertEqual(before['synthetic-b'].objects[60].get_raw_data(),b.objects[60].get_raw_data())
        self.assertEqual(b.externals[0].path,'synthetic-a')
        tree = b.objects[50].read_typetree()
        self.assertEqual(tree['m_FallbackFontAssetTable'],[sdk.pointer(777),sdk.pointer(101,1)])
        self.assertEqual(metadata(a),metadata(before['synthetic-a']))
        self.assertEqual(a.objects[100].read_typetree()['m_FontData'],sfnt())
        self.assertEqual(a.objects[101].read_typetree()['m_SourceFontFile'],sdk.pointer(100))

    def test_existing_ids_and_missing_license_rejected(self):
        members,group=font_members()
        for change,code in [({'new_ids':[10,101,102,103]},'FONT_OBJECT_IDS_NOT_NEW'),
                            ({'license_confirmed':False},'FONT_INPUT_OR_LICENSE_REQUIRED')]:
            with self.subTest(code=code),self.assertRaisesRegex(ValueError,code):
                sdk.integrate(members,[dict(group,**change)],[])

    def test_fallback_group_is_an_exact_nonnegative_in_range_index(self):
        members,group=font_members()
        for index in [-1,True,0.0,1,None]:
            with self.subTest(index=index),self.assertRaisesRegex(ValueError,'FALLBACK_GROUP_BINDING_INVALID'):
                sdk.integrate(members,[group],[dict(member='synthetic-b',object_id=50,group=index)])
