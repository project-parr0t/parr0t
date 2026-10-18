from __future__ import annotations

import base64
import copy
import json
import struct
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from UnityPy.helpers import TypeTreeHelper
from UnityPy.helpers.TypeTreeNode import TypeTreeNode
from UnityPy.streams import EndianBinaryReader, EndianBinaryWriter

from game_l10n.unity import require_dependencies
from game_l10n.unity.catalog import crc, entries, options_at, patch_catalog
from game_l10n.unity.schema import (
    ManagedSchemas, native_prefix_schema, normalize_generated_schema, plain_reference_schema,
    reference_schema,
)
from game_l10n.unity.serialization import (
    read_complete_typetree, rewrite_fields, rewrite_registry_fields,
)


def node(typ, name, children=(), *, flag=0):
    def rows(typ, name, children, flag, level):
        result = [dict(m_Level=level, m_Type=typ, m_Name=name,
                       m_ByteSize=-1, m_Version=1, m_MetaFlag=flag)]
        for child in children:
            for entry in child.traverse():
                result.append({k: getattr(entry, k) for k in
                               ('m_Type', 'm_Name', 'm_ByteSize', 'm_Version', 'm_MetaFlag')} |
                              {'m_Level': entry.m_Level + level + 1})
        return result
    return TypeTreeNode.from_list(rows(typ, name, children, flag, 0))


def string(name):
    return node('string', name, [node('Array', 'Array', [node('int', 'size'),
                                                       node('char', 'data')], flag=16384)])


def aligned(value):
    raw = value.encode('utf-8')
    data = struct.pack('<i', len(raw)) + raw
    return data + b'\0' * (-len(data) % 4)


def type_record(value):
    return b''.join(aligned(value[k]) for k in ('class', 'ns', 'asm'))


TERMINAL = {'class': 'Terminus', 'ns': 'UnityEngine.DMAT', 'asm': 'FAKE_ASM'}
PAYLOAD_TYPE = {'class': 'SyntheticPayload', 'ns': 'Synthetic', 'asm': 'SyntheticAssembly'}


class SyntheticObject:
    def __init__(self, tree, raw, refs=()):
        self.node = tree
        self.raw = raw
        self.reader = types.SimpleNamespace(endian='<')
        self.assets_file = types.SimpleNamespace(ref_types=list(refs))

    def get_raw_data(self):
        return self.raw

    def _get_typetree_node(self, node=None):
        return node or self.node

    def read_typetree(self, node, check_read=True):
        return TypeTreeHelper.read_typetree(node, EndianBinaryReader(self.raw, '<'),
                                           as_dict=True, assetsfile=self.assets_file,
                                           byte_size=len(self.raw), check_read=check_read)


def registry_object(*, populated=False, tail=b'', version=1):
    typ = node('ReferencedManagedType', 'type', [string('class'), string('ns'), string('asm')])
    registry = node('ManagedReferencesRegistry', 'references', [node('int', 'version'),
        node('ReferencedObject', 'RefIds', [typ, node('ReferencedObjectData', 'data')])])
    tree = node('SyntheticRoot', 'Base', [string('m_Name'), registry])
    payload = node('SyntheticPayload', 'Base', [string('message')])
    refs = [types.SimpleNamespace(m_ClassName=PAYLOAD_TYPE['class'],
                                  m_NameSpace=PAYLOAD_TYPE['ns'],
                                  m_AssemblyName=PAYLOAD_TYPE['asm'], nodes=payload)]
    raw = aligned('SYNTHETIC_SOURCE') + struct.pack('<i', version)
    if populated:
        raw += type_record(PAYLOAD_TYPE) + aligned('SYNTHETIC_REFERENCE')
    raw += type_record(TERMINAL) + tail
    return SyntheticObject(tree, raw, refs)


class UnitySerializationTests(unittest.TestCase):
    def test_pinned_dependencies(self):
        require_dependencies()

    def test_empty_v1_registry_consumes_terminal(self):
        obj = registry_object()
        self.assertEqual(read_complete_typetree(obj),
                         {'m_Name': 'SYNTHETIC_SOURCE', 'references': {'version': 1}})

    def test_populated_v1_registry_uses_embedded_reference_schema(self):
        obj = registry_object(populated=True)
        value = read_complete_typetree(obj)
        self.assertEqual(value['references']['00000000']['data']['message'],
                         'SYNTHETIC_REFERENCE')

    def test_registry_trailing_bytes_rejected(self):
        with self.assertRaisesRegex(ValueError, 'byte_accounting'):
            read_complete_typetree(registry_object(tail=b'\0\0\0\0'))

    def test_registry_other_version_rejected(self):
        with self.assertRaisesRegex(ValueError, 'version_invalid'):
            read_complete_typetree(registry_object(version=2))

    def test_registry_exact_rewrite_preserves_reference_and_input(self):
        obj = registry_object(populated=True)
        before = obj.get_raw_data()
        output = rewrite_registry_fields(obj, obj.node, [
            {'path': ['m_Name'], 'source': 'SYNTHETIC_SOURCE', 'text': 'SYNTHETIC_TARGET_LONG'}])
        check = SyntheticObject(obj.node, output, obj.assets_file.ref_types)
        wanted = read_complete_typetree(obj)
        wanted['m_Name'] = 'SYNTHETIC_TARGET_LONG'
        self.assertEqual(read_complete_typetree(check), wanted)
        self.assertEqual(obj.get_raw_data(), before)

    def test_registry_rewrite_wrong_source_rejected(self):
        obj = registry_object()
        with self.assertRaises(AssertionError):
            rewrite_registry_fields(obj, obj.node, [
                {'path': ['m_Name'], 'source': 'SYNTHETIC_WRONG', 'text': 'SYNTHETIC_TARGET'}])

    def test_plain_field_roundtrip_and_guard(self):
        tree = node('SyntheticRoot', 'Base', [string('message'), node('int', 'stable')])
        obj = SyntheticObject(tree, aligned('SYNTHETIC_SOURCE') + struct.pack('<i', 73))
        output = rewrite_fields(obj, tree, [{'path': ['message'], 'source': 'SYNTHETIC_SOURCE',
                                           'text': 'SYNTHETIC_TARGET_LONGER'}])
        self.assertEqual(read_complete_typetree(SyntheticObject(tree, output)),
                         {'message': 'SYNTHETIC_TARGET_LONGER', 'stable': 73})
        with self.assertRaisesRegex(ValueError, 'source_mismatch'):
            rewrite_fields(obj, tree, [{'path': ['message'], 'source': 'SYNTHETIC_WRONG',
                                       'text': 'SYNTHETIC_TARGET'}])

    def test_duplicate_paths_rejected(self):
        tree = node('SyntheticRoot', 'Base', [string('message')])
        obj = SyntheticObject(tree, aligned('SYNTHETIC_SOURCE'))
        row = {'path': ['message'], 'source': 'SYNTHETIC_SOURCE', 'text': 'SYNTHETIC_TARGET'}
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            rewrite_fields(obj, tree, [row, row])

    def test_generated_string_collection_dispatch_and_scalar(self):
        scalar = string('scalar')
        collection = node('string', 'values', [node('Array', 'Array', [
            node('int', 'size'), string('data')])])
        original = node('SyntheticRoot', 'Base', [scalar, collection])
        normalized = normalize_generated_schema(original)
        self.assertEqual([c.m_Type for c in original.m_Children], ['string', 'string'])
        self.assertEqual([c.m_Type for c in normalized.m_Children], ['string', 'vector'])
        obj = SyntheticObject(normalized, aligned('SYNTHETIC_SCALAR') + struct.pack('<i', 2)
                              + aligned('SYNTHETIC_A') + aligned('SYNTHETIC_B'))
        self.assertEqual(read_complete_typetree(obj)['values'], ['SYNTHETIC_A', 'SYNTHETIC_B'])

    def test_native_prefix_is_authoritative_and_reference_prefix_removed(self):
        native = node('MonoBehaviour', 'Base', [node('int', 'm_ObjectHideFlags')])
        generated = node('MonoBehaviour', 'Base', [node('int', 'm_ObjectHideFlags', flag=16384),
                                                  string('message')])
        fixed = native_prefix_schema(generated, native)
        self.assertEqual(fixed.m_Children[0].m_MetaFlag, 0)
        self.assertEqual(len(plain_reference_schema(generated, native).m_Children), 1)
        with self.assertRaisesRegex(ValueError, 'prefix_mismatch'):
            native_prefix_schema(generated, node('MonoBehaviour', 'Base', [node('int', 'wrong')]))

    def test_missing_reference_schema_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'schema_invalid'):
            reference_schema(PAYLOAD_TYPE, types.SimpleNamespace(ref_types=[]))

    def test_metadata_sources_are_allowlisted_and_immutable(self):
        class SyntheticGenerator:
            def __init__(self, version):
                self.loaded = []
            def load_dll(self, raw):
                self.loaded.append(raw)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / 'SyntheticAssembly.dll'
            source.write_bytes(b'SYNTHETIC_METADATA_INPUT')
            with patch('UnityPy.helpers.TypeTreeGenerator.TypeTreeGenerator', SyntheticGenerator):
                schema = ManagedSchemas('2021.1.0f1', [root], [root])
                schema.verify_unchanged()
                self.assertEqual(schema.generator.loaded, [b'SYNTHETIC_METADATA_INPUT'])
                source.write_bytes(b'SYNTHETIC_CHANGED_METADATA')
                with self.assertRaisesRegex(ValueError, 'reference_changed'):
                    schema.verify_unchanged()
                with self.assertRaisesRegex(ValueError, 'root_invalid'):
                    ManagedSchemas('2021.1.0f1', [root], [root / 'synthetic-allowed'])
        with self.assertRaisesRegex(ValueError, 'version_mismatch'):
            ManagedSchemas('2021.3.0f1', [], [])


def synthetic_bundle(data):
    info = b'\0' * 16 + struct.pack('>iIIH', 1, len(data), len(data), 64)
    info += struct.pack('>iqqI', 1, 0, len(data), 0) + b'synthetic.bin\0'
    header = b'UnityFS\0' + struct.pack('>I', 7) + b'5.x.x\0' + b'2021.1.0f1\0'
    size = len(header) + 20
    pad = (-size) % 16
    return header + struct.pack('>qIII', size + pad + len(info) + len(data),
                                len(info), len(info), 64) + b'\0' * pad + info + data


def synthetic_catalog(bundle):
    asm = b'Unity.ResourceManager, Version=0.0.0.0, Culture=neutral, PublicKeyToken=null'
    typ = b'UnityEngine.ResourceManagement.ResourceProviders.AssetBundleRequestOptions'
    def record(options):
        data = json.dumps(options, separators=(',', ':')).encode('utf-16-le')
        return bytes([7, len(asm)]) + asm + bytes([len(typ)]) + typ + struct.pack('<i', len(data)) + data
    options = {'m_Hash': '0' * 32, 'm_Crc': crc(bundle), 'm_BundleSize': 1,
               'm_BundleName': 'synthetic-stable', 'm_UseCrcForCachedBundles': True}
    first = record(options)
    extra = first + record(dict(options, m_BundleName='synthetic-untouched'))
    return json.dumps({
        'm_InternalIds': ['{synthetic-root}/synthetic.bundle', '{synthetic-root}/untouched.bundle'],
        'm_ProviderIds': ['UnityEngine.ResourceManagement.ResourceProviders.AssetBundleProvider'],
        'm_EntryDataString': base64.b64encode(struct.pack('<i', 2) +
            struct.pack('<7i', 0, 0, -1, 0, 0, 0, 0) +
            struct.pack('<7i', 1, 0, -1, 0, len(first), 1, 0)).decode(),
        'm_ExtraDataString': base64.b64encode(extra).decode(),
        'm_KeyDataString': 'SYNTHETIC_KEYS_EXACT',
        'm_BucketDataString': 'SYNTHETIC_BUCKETS_EXACT',
    }, separators=(',', ':')).encode()


class UnityCatalogTests(unittest.TestCase):
    def test_changed_bundle_requires_rebound_nonzero_crc_and_cache_identity(self):
        before = synthetic_bundle(b'SYNTHETIC_SOURCE')
        changed = synthetic_bundle(b'SYNTHETIC_CHANGED_LONGER')
        catalog = synthetic_catalog(before)
        output, proof = patch_catalog(catalog, 'synthetic.bundle', changed)
        self.assertNotEqual(crc(before), crc(changed))
        self.assertEqual(proof['replacement']['m_Crc'], crc(changed))
        self.assertNotEqual(proof['replacement']['m_Crc'], 0)
        self.assertEqual(proof['replacement']['m_BundleSize'], len(changed))
        self.assertNotEqual(proof['replacement']['m_Hash'], proof['original']['m_Hash'])
        self.assertTrue(proof['replacement']['m_UseCrcForCachedBundles'])
        self.assertNotEqual(proof['extra_offset_delta'], 0)
        old, new = json.loads(catalog), json.loads(output)
        self.assertEqual(old['m_InternalIds'], new['m_InternalIds'])
        self.assertEqual(old['m_KeyDataString'], new['m_KeyDataString'])
        oldrows, _ = entries(old); newrows, _ = entries(new)
        oldopts = options_at(base64.b64decode(old['m_ExtraDataString']), oldrows[1][4])[0]
        newopts = options_at(base64.b64decode(new['m_ExtraDataString']), newrows[1][4])[0]
        self.assertEqual(oldopts, newopts)

    def test_missing_bundle_entry_rejected(self):
        bundle = synthetic_bundle(b'SYNTHETIC_SOURCE')
        with self.assertRaises(AssertionError):
            patch_catalog(synthetic_catalog(bundle), 'absent.bundle', bundle)


if __name__ == '__main__':
    unittest.main()
