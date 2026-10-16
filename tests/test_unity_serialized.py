"""Synthetic opaque-object fixtures; no player files or font binaries."""
import importlib.util
from pathlib import Path
import struct
import sys
import types
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / 'profiles/sdk-unity-2022.3/tools'
spec = importlib.util.spec_from_file_location('synthetic_serialized', TOOLS / 'unity_serialized.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def fixture():
    payloads = [b'SOURCE_ONE', b'UNOWNED']
    table = b''.join(struct.pack('<qqIi', i + 1, i * 16, len(x), 1)
                     for i, x in enumerate(payloads))
    raw = bytearray(b'H' * 48 + table + b'M' * (160 - 48 - len(table)))
    struct.pack_into('>q', raw, 24, 192)
    for value in payloads:
        raw.extend(value + b'G' * (16 - len(value)))
    return bytes(raw)


def fake_env(raw):
    objects = {}
    for i in range(2):
        pid, offset, size, typ = struct.unpack_from('<qqIi', raw, 48 + i * 24)
        obj = types.SimpleNamespace(path_id=pid, byte_start=160 + offset,
                                    byte_size=size, type_id=typ,
                                    type=types.SimpleNamespace(name='Synthetic'))
        obj.get_raw_data = lambda obj=obj: raw[obj.byte_start:obj.byte_start + obj.byte_size]
        objects[pid] = obj
    file = types.SimpleNamespace(objects=objects,
                header=types.SimpleNamespace(data_offset=160, metadata_size=112))
    return types.SimpleNamespace(file=file, objects=list(objects.values()))


class SerializedTests(unittest.TestCase):
    def test_relocation_preserves_unowned_bytes_metadata_and_gap(self):
        raw = fixture()
        with patch.object(m, 'environment', side_effect=fake_env):
            out = m.rebuild_serialized(raw, {1: b'TARGET_ONE_LONGER_SYNTHETIC'})
        env = fake_env(out)
        self.assertEqual(env.file.objects[1].get_raw_data(), b'TARGET_ONE_LONGER_SYNTHETIC')
        self.assertEqual(env.file.objects[2].get_raw_data(), b'UNOWNED')
        self.assertEqual(struct.unpack_from('>q', out, 24)[0], len(out))
        self.assertEqual(out[96:160], raw[96:160])
        second = env.file.objects[2].byte_start
        self.assertEqual(out[second - 6:second], b'G' * 6)
        self.assertEqual(second % 8, 0)

    def test_unchanged_rebuild_is_identical(self):
        with patch.object(m, 'environment', side_effect=fake_env):
            self.assertEqual(m.rebuild_serialized(fixture(), {}), fixture())

    def test_unknown_object_and_nonbytes_rejected(self):
        with patch.object(m, 'environment', side_effect=fake_env):
            for changes, code in [({3: b'TARGET'}, 'OBJECT_BINDING_MISSING'),
                                  ({1: 'TARGET'}, 'OBJECT_PAYLOAD_INVALID')]:
                with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                    m.rebuild_serialized(fixture(), changes)

    def test_outside_object_and_overlap_rejected(self):
        for offset, size, code in [(16, 999, 'OBJECT_BOUNDARY_INVALID'),
                                   (0, 7, 'OBJECT_REGIONS_OVERLAP')]:
            raw = bytearray(fixture())
            struct.pack_into('<qI', raw, 80, offset, size)
            with patch.object(m, 'environment', side_effect=fake_env):
                with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                    m.rebuild_serialized(bytes(raw), {})

    def test_header_and_metadata_boundary_rejected(self):
        for offset, metadata in [(999, 112), (160, 999), (47, 0)]:
            env = fake_env(fixture())
            env.file.header.data_offset = offset
            env.file.header.metadata_size = metadata
            with self.subTest(offset=offset, metadata=metadata):
                with self.assertRaisesRegex(ValueError, 'SERIALIZED_HEADER_BOUNDARY_INVALID'):
                    m.object_table(fixture(), env)

    def test_object_table_must_be_inside_metadata(self):
        env = fake_env(fixture())
        env.file.header.metadata_size = 20
        with self.assertRaisesRegex(ValueError, 'OBJECT_TABLE_OUTSIDE_METADATA'):
            m.object_table(fixture(), env)

    def test_string_utf8_alignment(self):
        text = 'SOURCE_\u03a9_{0}'
        raw = m.pack_string(text)
        self.assertEqual(m.string_at(raw, 0), (text, len(raw)))
        self.assertEqual(len(raw) % 4, 0)

    def test_string_malformed_boundaries_and_encoding(self):
        for raw, start, code in [(b'', 0, 'STRING_BOUNDARY_INVALID'),
                (b'xxxx', -1, 'STRING_BOUNDARY_INVALID'),
                (struct.pack('<i', -1), 0, 'STRING_BOUNDARY_INVALID'),
                (struct.pack('<i', 999), 0, 'STRING_BOUNDARY_INVALID'),
                (struct.pack('<i', 1) + b'A', 0, 'STRING_PADDING_INVALID'),
                (struct.pack('<i', 1) + b'\xff\0\0\0', 0, 'STRING_UTF8_INVALID')]:
            with self.subTest(code=code, start=start), self.assertRaisesRegex(ValueError, code):
                m.string_at(raw, start)

    def test_ambiguous_missing_and_empty_binding_rejected(self):
        for raw, needle in [(b'TABTAB', b'TAB'), (b'ONE', b'TAB'), (b'ONE', b'')]:
            with self.subTest(needle=needle), self.assertRaisesRegex(ValueError, 'BINDING_BOUNDARY_AMBIGUOUS'):
                m.unique_boundary(raw, needle)

    def test_environment_rejects_short_or_wrong_layout(self):
        with self.assertRaisesRegex(ValueError, 'SERIALIZED_HEADER_BOUNDARY_INVALID'):
            m.environment(b'')
        for name, version, endian, code in [('BundleFile', 22, '<', 'STANDALONE_SERIALIZED_FILE_REQUIRED'),
                ('SerializedFile', 21, '<', 'SERIALIZED_LAYOUT_NOT_BOUND'),
                ('SerializedFile', 22, '>', 'SERIALIZED_LAYOUT_NOT_BOUND')]:
            file = type(name, (), {})()
            file.header = types.SimpleNamespace(version=version, endian=endian)
            unity = types.SimpleNamespace(load=lambda raw: types.SimpleNamespace(file=file))
            with patch.dict(sys.modules, {'UnityPy': unity}):
                with self.subTest(name=name, version=version, endian=endian), self.assertRaisesRegex(ValueError, code):
                    m.environment(b'H' * 48)

    def test_font_delta_preserves_nonpayload_fields(self):
        original = {'m_FontData': [], 'm_FontNames': ['Synthetic Sans'],
                    'm_FallbackFonts': [{'m_FileID': 2, 'm_PathID': 7}], 'm_FontSize': 16}
        tree = dict(original)
        obj = types.SimpleNamespace(type=types.SimpleNamespace(name='Font'))
        obj.read_typetree = lambda: dict(tree)
        obj.save_typetree = lambda value: (tree.update(value), b'SYNTHETIC_FONT')[1]
        env = types.SimpleNamespace(file=types.SimpleNamespace(objects={1: obj}))
        with patch.object(m, 'environment', return_value=env), patch.object(m, 'rebuild_serialized', return_value=b'OUTPUT') as rebuild:
            self.assertEqual(m.replace_font_payload(b'INPUT', 1, b'FONT', ['Synthetic Target Sans']), b'OUTPUT')
        self.assertEqual(tree['m_FallbackFonts'], original['m_FallbackFonts'])
        self.assertEqual(tree['m_FontSize'], 16)
        self.assertEqual(tree['m_FontData'], list(b'FONT'))
        self.assertEqual(tree['m_FontNames'], ['Synthetic Target Sans'])
        rebuild.assert_called_once_with(b'INPUT', {1: b'SYNTHETIC_FONT'})

    def test_font_binding_and_empty_inputs_rejected(self):
        env = types.SimpleNamespace(file=types.SimpleNamespace(objects={}))
        with patch.object(m, 'environment', return_value=env):
            with self.assertRaisesRegex(ValueError, 'FONT_PAYLOAD_BINDING_INVALID'):
                m.replace_font_payload(b'INPUT', 1, b'FONT', ['Synthetic Sans'])
        env.file.objects[1] = types.SimpleNamespace(type=types.SimpleNamespace(name='Font'))
        for data, names in [(b'', ['Synthetic Sans']), (b'FONT', []), (b'FONT', ['']), (b'FONT', 'NAME')]:
            with patch.object(m, 'environment', return_value=env):
                with self.subTest(data=data, names=names), self.assertRaisesRegex(ValueError, 'FONT_PAYLOAD_BINDING_INVALID'):
                    m.replace_font_payload(b'INPUT', 1, data, names)


if __name__ == '__main__':
    unittest.main()
