"""Real in-memory synthetic UnityFS/v22 regression fixtures."""
import copy
import importlib.util
import struct
import unittest

from tests.unity_6000_4_fixtures import bundle, text_file, schema, behaviour, serialized
import unity_serialized as sdk

HAS_UNITY = importlib.util.find_spec('UnityPy') is not None


class StringTests(unittest.TestCase):
    def test_utf8_padding_and_invalid_boundaries(self):
        text = 'SYNTHETIC_\u03a9_{0}'
        payload = sdk.pack_string(text)
        self.assertEqual(sdk.string_at(payload,0),(text,len(payload)))
        self.assertEqual(len(payload)%4,0)
        for payload in [b'',struct.pack('<i',-1),struct.pack('<i',99),struct.pack('<i',1)+b'A',
                        struct.pack('<i',1)+b'\xff\0\0\0']:
            with self.subTest(payload=payload), self.assertRaises(ValueError): sdk.string_at(payload,0)


@unittest.skipUnless(HAS_UNITY, 'Install the pinned private UnityPy dependency for binary round trips')
class SerializedTests(unittest.TestCase):
    def test_typed_and_raw_changes_preserve_other_objects_and_metadata(self):
        original,_ = text_file()
        before = sdk.environment(original).file
        rows = [dict(object_id=11,field_path=['m_Label'],source='SYNTHETIC_SOURCE_{0}',target='SYNTHETIC_TARGET_LONG_{0}'),
                dict(object_id=33,start=0,end=20,source='SYNTHETIC_RAW',target='SYNTHETIC_RAW_TARGET')]
        result = sdk.apply_serialized(original,rows)
        after = sdk.environment(result).file
        self.assertEqual(after.objects[11].read_typetree()['m_Label'],'SYNTHETIC_TARGET_LONG_{0}')
        self.assertEqual(sdk.string_at(after.objects[33].get_raw_data(),0)[0],'SYNTHETIC_RAW_TARGET')
        self.assertEqual(before.objects[22].get_raw_data(),after.objects[22].get_raw_data())
        self.assertEqual(sdk.metadata(before),sdk.metadata(after))
        self.assertEqual(sdk.externals(before),sdk.externals(after))
        self.assertEqual(set(before.objects),set(after.objects))
        self.assertEqual(sdk.rebuild_serialized(original,{}),original)

    def test_stale_ambiguous_overlapping_and_mixed_bindings_rejected(self):
        raw,_ = text_file()
        typed = dict(object_id=11,field_path=['m_Label'],source='SYNTHETIC_SOURCE_{0}',target='SYNTHETIC_TARGET_{0}')
        span = dict(object_id=33,start=0,end=20,source='SYNTHETIC_RAW',target='SYNTHETIC_TARGET')
        cases = [([dict(typed,source='SYNTHETIC_STALE')],'FIELD_SOURCE_CHANGED'),
                 ([typed,typed],'FIELD_BINDING_AMBIGUOUS'),([span,span],'STRING_SPANS_OVERLAP'),
                 ([dict(span,end=12)],'STRING_SOURCE_CHANGED'),
                 ([typed,dict(span,object_id=11)],'OBJECT_BINDING_MODES_MIXED'),
                 ([dict(typed,object_id=999)],'OBJECT_BINDING_UNSUPPORTED')]
        for bindings, code in cases:
            with self.subTest(code=code),self.assertRaisesRegex(ValueError,code): sdk.apply_serialized(raw,bindings)

    def test_bundle_entry_flags_names_and_opaque_stream_remain_exact(self):
        original,_ = text_file()
        raw = bundle({'synthetic-member':original,'synthetic-opaque':b'SYNTHETIC_OPAQUE_STREAM\x00\xff'})
        env,members = sdk.bundle_members(raw)
        flags = {name:(f.flags,getattr(f,'name',name)) for name,f in env.file.files.items()}
        changed = sdk.apply_serialized(original,[dict(object_id=11,field_path=['m_Label'],
            source='SYNTHETIC_SOURCE_{0}',target='SYNTHETIC_TARGET_{0}')])
        result = sdk.rebuild_bundle(raw,{'synthetic-member':changed})
        check,after = sdk.bundle_members(result)
        self.assertEqual(after['synthetic-member'],changed)
        self.assertEqual(after['synthetic-opaque'],members['synthetic-opaque'])
        self.assertEqual(flags,{name:(f.flags,getattr(f,'name',name)) for name,f in check.file.files.items()})
        with self.assertRaisesRegex(ValueError,'OPAQUE_MEMBER_REPLACEMENT_UNSUPPORTED'):
            sdk.rebuild_bundle(raw,{'synthetic-opaque':b'SYNTHETIC_OTHER'})

    def test_bounded_enabled_alignment_repair_is_lossless(self):
        raw,records = text_file()
        obj = sdk.environment(raw).file.objects[11]
        broken = copy.deepcopy(records)
        next(r for r in broken if r['m_Name']=='m_Enabled')['m_MetaFlag'] = 0
        fixed = sdk.aligned_behaviour_node(broken)
        tree = obj.read_typetree(nodes=fixed)
        self.assertEqual(obj.save_typetree(tree,nodes=fixed),obj.get_raw_data())
        self.assertEqual(next(r for r in broken if r['m_Name']=='m_Enabled')['m_MetaFlag'],0)
        bad = copy.deepcopy(broken)
        next(r for r in bad if r['m_Name']=='m_Enabled')['m_Type']='int'
        with self.assertRaisesRegex(ValueError,'BEHAVIOUR_SCHEMA_NOT_BOUND'): sdk.aligned_behaviour_node(bad)

    def test_other_engine_line_and_textasset_specific_application_rejected(self):
        tree = behaviour()
        records = schema(tree,'MonoBehaviour')
        with self.assertRaisesRegex(ValueError,'SERIALIZED_LAYOUT_NOT_BOUND'):
            sdk.environment(serialized([(1,114,records,tree)],version='2022.3.0f1'))
        raw = serialized([(1,49,schema({'m_Name':'SYNTHETIC_TEXT'},'TextAsset'),{'m_Name':'SYNTHETIC_TEXT'})])
        with self.assertRaisesRegex(ValueError,'OBJECT_BINDING_UNSUPPORTED'):
            sdk.apply_serialized(raw,[dict(object_id=1,field_path=['m_Name'],source='SYNTHETIC_TEXT',target='SYNTHETIC_TARGET')])
