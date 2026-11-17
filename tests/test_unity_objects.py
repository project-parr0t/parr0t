"""Synthetic real-format bytes; injected primary tests are not UnityPy runtime proof."""
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / 'profiles/sdk-unity-2022.3/tools'
sys.path.insert(0, str(TOOLS))
import unity_objects as objects
import unity_raw as reader
from unity_inventory_cache import ScanCache
from unity_objects_fixture import (
    string, node, serialized, text_tree, nested_tree, unityfs,
    build_fixture, build_bundle_fixture, EXPECTED_STRINGS, EXPECTED_LOCATIONS,
)




def scan(raw, *, method='independent', managed_types=None, relative_path='data/synthetic.assets'):
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / 'synthetic.assets'
        path.write_bytes(raw)
        return objects.scan_serialized(path, relative_path, method=method,
                                       managed_types=managed_types)


def replacement(pid, field, source, target, **location):
    return dict(location=dict(object_id=pid, field_path=field, **location), source=source, target=target)


def primary_nodes(flat):
    stack = []
    for value in flat:
        entry = types.SimpleNamespace(m_Type=value['type'], m_Name=value['name'], m_Children=[])
        while len(stack) > value['level']:
            stack.pop()
        if stack:
            stack[-1].m_Children.append(entry)
        else:
            root = entry
        stack.append(entry)
    return root


def managed_fixture(textasset_payload='PAYLOAD'):
    named = lambda name: dict(kind='named', assembly='Synthetic', name=name)
    flow = [dict(method_token=0x06000001, il_offset=0, sink=dict(
        assembly='UnityEngine.UI', type='UnityEngine.UI.Text', member='set_text'))]
    types_ = {
        'Synthetic::Synthetic.Panel': dict(name='Synthetic.Panel', assembly='Synthetic', enum=False,
            base=dict(kind='named', assembly='UnityEngine.CoreModule', name='UnityEngine.MonoBehaviour'), fields=[
                dict(name='enabled', type=dict(kind='primitive', name='bool'), serialized=True, static=False),
                dict(name='items', type=dict(kind='list', element=named('Synthetic.Item')), serialized=True, static=False),
                dict(name='resource', type=dict(kind='pptr', name='UnityEngine.TextAsset'), serialized=True,
                     static=False, display_consumers=flow)]),
        'Synthetic::Synthetic.Item': dict(name='Synthetic.Item', assembly='Synthetic', enum=False,
            base=dict(kind='named', assembly='mscorlib', name='System.ValueType'), fields=[
                dict(name='caption', type=dict(kind='string'), serialized=True, static=False,
                     display_consumers=flow, proof=dict(field_token=0x04000001))]),
    }
    prefix = struct.pack('<iqB', 0, 0, 1) + bytes(3) + struct.pack('<iq', 0, 2) + string('PANEL')
    fields = b'\x01\0\0\0' + struct.pack('<i', 2) + string('DUPLICATE') + string('DUPLICATE') + struct.pack('<iq', 0, 3)
    script = string('SCRIPT') + struct.pack('<i', 0) + bytes(16)
    script += string('Panel') + string('Synthetic') + string('Synthetic.dll')
    raw = serialized([(114, []), (115, []), (49, [])],
        [(1, 0, prefix + fields), (2, 1, script), (3, 2, string('ASSET') + string(textasset_payload))], trees=False)
    return raw, types_


class ObjectInventoryTests(unittest.TestCase):
    def test_nested_array_empty_duplicate_and_reference(self):
        payload = string('SAME') + struct.pack('<i', 3) + string('SAME') + string('') + string('LAST')
        payload += struct.pack('<iq', 0, 8)
        raw = serialized([(900, nested_tree()), (49, [])],
                         [(7, 0, payload), (8, 1, b'')])
        result = scan(raw)
        values = result['occurrences']
        self.assertEqual([r['source'] for r in values], ['SAME', 'SAME', '', 'LAST'])
        self.assertEqual([r['location']['field_path'] for r in values],
                         [['first'], ['items', 0, 'caption'], ['items', 1, 'caption'], ['items', 2, 'caption']])
        self.assertEqual([r['location']['field_index'] for r in values], list(range(4)))
        edge = result['dependencies'][0]
        self.assertEqual(edge['target']['object_id'], 8)
        self.assertTrue(edge['resolved'])
        self.assertFalse(edge['display_dependency'])
        self.assertTrue(all(r['classification'] == 'uncertain' for r in values))

    def test_native_ui_is_type_bound_not_field_name_heuristic(self):
        raw = serialized([(102, text_tree()), (900, text_tree(900)), (129, [
            node(0, 'PlayerSettings', 'Base'), node(1, 'string', 'productName')])],
            [(1, 0, string('UI')), (2, 1, string('RAW')), (3, 2, string('TITLE'))])
        result = scan(raw)
        self.assertEqual([r['classification'] for r in result['occurrences']],
                         ['display', 'uncertain', 'display'])
        self.assertEqual(result['pending'], [])

    def test_injected_primary_agrees_on_real_byte_physical_locators(self):
        payload = string('SAME') + struct.pack('<i', 2) + string('') + string('SAME') + struct.pack('<iq', 0, 8)
        raw = serialized([(900, nested_tree())], [(7, 0, payload)])
        fake = types.SimpleNamespace(path_id=7, type=types.SimpleNamespace(value=900),
            serialized_type=types.SimpleNamespace(node=primary_nodes(nested_tree())),
            read_typetree=lambda: {'first': 'SAME', 'items': [{'caption': ''}, {'caption': 'SAME'}],
                                  'resource': {'m_FileID': 0, 'm_PathID': 8}})
        env = types.SimpleNamespace(file=types.SimpleNamespace(objects={7: fake}, externals=[]))
        with patch.object(objects, '_load_primary', return_value=env):
            primary = scan(raw, method='primary')
        independent = scan(raw)
        self.assertEqual([(r['location'], r['source']) for r in primary['occurrences']],
                         [(r['location'], r['source']) for r in independent['occurrences']])
        self.assertEqual(primary['dependencies'][0]['location'], independent['dependencies'][0]['location'])
        self.assertNotEqual(primary['proofs'][0]['parser'], independent['proofs'][0]['parser'])

    def test_textasset_payload_and_structural_duplicates(self):
        raw = serialized([(49, [])], [(1, 0, string('LABELS') + string('{"v":["SAME","","SAME"]}'))], trees=False)
        result = scan(raw)
        tables = [r for r in result['occurrences'] if 'parser_path' in r['location']]
        self.assertEqual([r['source'] for r in tables], ['SAME', '', 'SAME'])
        self.assertEqual([r['location']['parser_path'] for r in tables], [['v', 0], ['v', 1], ['v', 2]])
        self.assertEqual(result['pending'], [])

    def test_unknown_ui_layout_and_corrupt_boundaries_are_pending(self):
        raw = serialized([(102, [])], [(1, 0, b'OPAQUE')], trees=False)
        self.assertEqual(scan(raw)['pending'][0]['reason'], 'DISPLAY_OBJECT_LAYOUT_UNRESOLVED')
        raw = serialized([(102, text_tree())], [(1, 0, struct.pack('<i', 999))])
        self.assertEqual(scan(raw)['occurrences'], [])
        self.assertTrue(scan(raw)['pending'])

    def test_overlapping_and_duplicate_object_tables_rejected(self):
        raw = serialized([(900, text_tree(900))], [(1, 0, string('A')), (2, 0, string('B'))])
        doc = reader.serialized(raw)
        for field, value in [('offset', 0), ('id', 1)]:
            damaged = bytearray(raw)
            at = doc['objects'][1]['table_pos'] + (8 if field == 'offset' else 0)
            struct.pack_into('<q', damaged, at, value)
            with self.subTest(field=field), self.assertRaises(reader.FormatError):
                reader.serialized(bytes(damaged))

    def test_primary_exact_version_guard_precedes_import(self):
        with patch.object(objects.importlib.metadata, 'version', return_value='1.25.0'):
            with self.assertRaisesRegex(ValueError, 'UNITYPY_VERSION_NOT_BOUND'):
                objects._load_primary(b'NOT_EXECUTED')

    def test_stripped_managed_nested_fields_and_pptr_display_binding(self):
        raw, types_ = managed_fixture()
        result = scan(raw, managed_types=types_)
        captions = [r for r in result['occurrences'] if r['location']['field_path'][0] == 'items']
        self.assertEqual([r['source'] for r in captions], ['DUPLICATE', 'DUPLICATE'])
        self.assertTrue(all(r['classification'] == 'display' for r in captions))
        self.assertEqual([r['location']['field_path'] for r in captions],
                         [['items', 0, 'caption'], ['items', 1, 'caption']])
        payload = next(r for r in result['occurrences'] if r['kind'] == 'textasset')
        self.assertEqual(payload['classification'], 'display')
        self.assertEqual(result['pending'], [])
        doc = reader.serialized(raw)
        fake_objects = {}
        for obj in doc['objects']:
            payload_bytes = raw[obj['offset']:obj['offset'] + obj['size']]
            fake_objects[obj['path_id']] = types.SimpleNamespace(path_id=obj['path_id'],
                type=types.SimpleNamespace(value=obj['class_id']),
                serialized_type=types.SimpleNamespace(node=None),
                get_raw_data=lambda data=payload_bytes: data)
        env = types.SimpleNamespace(file=types.SimpleNamespace(objects=fake_objects, externals=[]))
        with patch.object(objects, '_load_primary', return_value=env):
            primary = scan(raw, method='primary', managed_types=types_)
        self.assertEqual([(r['location'], r['source'], r['classification']) for r in primary['occurrences']],
                         [(r['location'], r['source'], r['classification']) for r in result['occurrences']])

    def test_display_textasset_tables_keep_raw_rows_but_hold_overlapping_projection(self):
        for payload in ('{"v":["SAME","SAME"]}', '[section]\r\nk=SAME\r\nk=SAME\r\n'):
            with self.subTest(payload=payload):
                raw, types_ = managed_fixture(payload)
                independent = scan(raw, managed_types=types_)
                doc, fake_objects = reader.serialized(raw), {}
                for obj in doc['objects']:
                    value = raw[obj['offset']:obj['offset'] + obj['size']]
                    fake_objects[obj['path_id']] = types.SimpleNamespace(path_id=obj['path_id'],
                        type=types.SimpleNamespace(value=obj['class_id']),
                        serialized_type=types.SimpleNamespace(node=None),
                        get_raw_data=lambda data=value: data)
                env = types.SimpleNamespace(file=types.SimpleNamespace(objects=fake_objects, externals=[]))
                with patch.object(objects, '_load_primary', return_value=env):
                    primary = scan(raw, method='primary', managed_types=types_)
                for method, result in [('primary', primary), ('independent', independent)]:
                    rows = [row for row in result['occurrences'] if row['kind'] == 'textasset']
                    self.assertEqual([row['source'] for row in rows], [payload, 'SAME', 'SAME'])
                    self.assertTrue(all(row['classification'] == 'uncertain' for row in rows))
                    self.assertTrue(rows[0]['display_projection_required'])
                    self.assertFalse(any(row.get('display_dependency') for row in rows[1:]))
                    self.assertTrue(any(row['reason'] == 'TEXTASSET_DISPLAY_PROJECTION_UNRESOLVED'
                                        for row in result['pending']))
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        with ScanCache(root / 'cache.sqlite3') as cache:
                            cache.store(method, 'data/synthetic.assets', 'source', 'scanner', result)
                            summary = cache.inventory(method, root / 'inventory.json', 'synthetic')
                        inventory = json.loads((root / 'inventory.json').read_text())
                        projected = [row for row in inventory['segments'] if row['metadata']['unity_kind'] == 'textasset']
                        self.assertEqual(len(projected), 1)
                        self.assertNotIn('parser_path', projected[0]['metadata']['location'])
                        self.assertTrue(projected[0]['metadata']['display_projection_required'])
                        self.assertFalse(inventory['coverage']['declared_complete'])
                        self.assertEqual(summary['raw_occurrence_count'], len(result['occurrences']))
                self.assertEqual([(row['location'], row['source']) for row in primary['occurrences']],
                                 [(row['location'], row['source']) for row in independent['occurrences']])

    def test_unsupported_managed_descriptor_remains_pending(self):
        raw, types_ = managed_fixture()
        types_['Synthetic::Synthetic.Panel']['fields'][1]['type'] = dict(
            kind='unsupported', reason='serialize_reference')
        result = scan(raw, managed_types=types_)
        self.assertIn('MANAGED_DISPLAY_LAYOUT_UNRESOLVED', [r['reason'] for r in result['pending']])

    def test_public_native_layouts_do_not_use_clr_pointer_field_storage(self):
        types_ = {'Synthetic::Values': dict(name='Values', assembly='Synthetic', base=None, enum=False,
            fields=[dict(name='color', serialized=True, static=False,
                         type=dict(kind='named', assembly='UnityEngine.CoreModule', name='UnityEngine.Color32')),
                    dict(name='content', serialized=True, static=False,
                         type=dict(kind='named', assembly='UnityEngine.IMGUIModule', name='UnityEngine.GUIContent'))])}
        raw = bytes((1, 2, 3, 4)) + string('CONTENT') + struct.pack('<iq', 0, 0) + string('TOOLTIP')
        result = reader.managed_object(raw, dict(offset=0, size=len(raw)), types_,
            dict(kind='named', assembly='Synthetic', name='Values'), 0)
        self.assertEqual(result['value']['color'], dict(r=1, g=2, b=3, a=4))
        self.assertEqual([r['source'] for r in result['strings']], ['CONTENT', 'TOOLTIP'])
        self.assertEqual(result['references'][0]['field_path'], ['content', 'm_Image'])

    def test_texture_pixels_never_reach_object_decoder(self):
        raw = serialized([(28, [node(0, 'Texture2D', 'Base'), node(1, 'TypelessData', 'image')])],
                         [(1, 0, struct.pack('<i', 4) + b'RGBA')])
        with patch.object(reader, 'tree_object', side_effect=AssertionError('pixels accessed')):
            result = scan(raw)
        self.assertEqual(result['occurrences'], [])
        self.assertEqual(result['pending'], [])

    def test_scalar_array_alignment_is_after_array_not_each_element(self):
        nodes = [node(0, 'Synthetic', 'Base'), node(1, 'vector', 'bytes'),
            node(2, 'Array', 'Array', flags=1), node(3, 'int', 'size', 4),
            node(3, 'UInt8', 'data', 1, meta=0x4000), node(1, 'string', 'caption')]
        raw = serialized([(900, nodes)], [(1, 0, struct.pack('<i', 3) + b'ABC\0' + string('AFTER_ARRAY'))])
        result = scan(raw)
        self.assertEqual([r['source'] for r in result['occurrences']], ['AFTER_ARRAY'])

    def test_pair_array_locators_agree_without_conflating_map_keys(self):
        nodes = [node(0, 'Synthetic', 'Base'), node(1, 'map', 'table'),
            node(2, 'Array', 'Array', flags=1), node(3, 'int', 'size', 4),
            node(3, 'pair', 'data'), node(4, 'string', 'first'), node(4, 'string', 'second')]
        raw = serialized([(900, nodes)], [(1, 0, struct.pack('<i', 2) +
                         string('KEY') + string('VALUE') + string('KEY') + string(''))])
        fake = types.SimpleNamespace(path_id=1, type=types.SimpleNamespace(value=900),
            serialized_type=types.SimpleNamespace(node=primary_nodes(nodes)),
            read_typetree=lambda: {'table': [('KEY', 'VALUE'), ('KEY', '')]})
        env = types.SimpleNamespace(file=types.SimpleNamespace(objects={1: fake}, externals=[]))
        with patch.object(objects, '_load_primary', return_value=env):
            primary = scan(raw, method='primary')
        independent = scan(raw)
        self.assertEqual([(r['location'], r['source']) for r in primary['occurrences']],
                         [(r['location'], r['source']) for r in independent['occurrences']])

    def test_gui_style_walk_binds_all_native_references_without_pixels(self):
        types_ = {'Synthetic::Styles': dict(name='Styles', assembly='Synthetic', base=None, enum=False,
            fields=[dict(name='style', serialized=True, static=False,
                         type=dict(kind='named', assembly='UnityEngine.IMGUIModule', name='UnityEngine.GUIStyle'))])}
        raw = string('STYLE_IDENTIFIER') + (struct.pack('<iqffff', 0, 0, 1, 1, 1, 1) * 8)
        raw += bytes(64) + struct.pack('<iqiii', 0, 0, 12, 0, 0) + b'\x01\x01\0\0'
        raw += bytes(8 + 8 + 8) + b'\0\0\0\0'
        result = reader.managed_object(raw, dict(offset=0, size=len(raw)), types_,
            dict(kind='named', assembly='Synthetic', name='Styles'), 0)
        self.assertEqual([r['source'] for r in result['strings']], ['STYLE_IDENTIFIER'])
        self.assertEqual(len(result['references']), 9)
        self.assertEqual(result['references'][-1]['field_path'], ['style', 'm_Font'])

    def test_bound_gui_background_stays_unverified_without_blocking_text_inventory(self):
        types_ = {'Synthetic::Synthetic.Styles': dict(name='Synthetic.Styles', assembly='Synthetic', base=None, enum=False,
            fields=[dict(name='style', serialized=True, static=False,
                         type=dict(kind='named', assembly='UnityEngine.IMGUIModule', name='UnityEngine.GUIStyle'))])}
        style = string('STYLE_IDENTIFIER') + struct.pack('<iqffff', 0, 2, 1, 1, 1, 1)
        style += struct.pack('<iqffff', 0, 0, 1, 1, 1, 1) * 7
        style += bytes(64) + struct.pack('<iqiii', 0, 0, 12, 0, 0) + b'\x01\x01\0\0'
        style += bytes(24) + bytes(4)
        prefix = struct.pack('<iqB', 0, 0, 1) + bytes(3) + struct.pack('<iq', 0, 3) + string('SYNTHETIC OBJECT')
        script = string('SYNTHETIC SCRIPT') + struct.pack('<i', 0) + bytes(16)
        script += string('Styles') + string('Synthetic') + string('Synthetic.dll')
        raw = serialized([(114, []), (28, []), (115, [])],
            [(1, 0, prefix + style), (2, 1, b'UNREAD_PIXEL_BYTES'), (3, 2, script)], trees=False)
        result = scan(raw, managed_types=types_)
        media = [item for item in result['pending'] if item['reason'] == 'DISPLAY_MEDIA_CONTENT_UNVERIFIED']
        self.assertEqual(len(media), 1)
        self.assertFalse(media[0]['affects_display'])
        self.assertFalse(any(item['affects_display'] for item in result['pending']))
        self.assertTrue(any(edge['target']['object_id'] == 2 for edge in result['dependencies']))

    def test_object_order_follows_physical_bytes_not_path_ids_or_table_order(self):
        raw = bytearray(serialized([(102, text_tree())], [(9, 0, string('FIRST')), (3, 0, string('SECOND'))]))
        doc = reader.serialized(raw)
        a, b = [row['table_pos'] for row in doc['objects']]
        raw[a:a + 24], raw[b:b + 24] = raw[b:b + 24], raw[a:a + 24]
        result = scan(bytes(raw))
        self.assertEqual([r['location']['object_id'] for r in result['occurrences']], [9, 3])
        self.assertEqual([r['location']['object_index'] for r in result['occurrences']], [0, 1])

    def test_public_preinventory_fixture_expectations(self):
        for raw in (build_fixture(), build_bundle_fixture()):
            result = scan(raw)
            self.assertEqual([r['source'] for r in result['occurrences']], EXPECTED_STRINGS)
            for row, expected in zip(result['occurrences'], EXPECTED_LOCATIONS):
                self.assertEqual({key: row['location'][key] for key in expected}, expected)
            self.assertEqual(result['pending'], [])


class ScriptBindingTests(unittest.TestCase):
    @staticmethod
    def script(file, *, entry=None, name='Synthetic.Panel', pid=5):
        return dict(types=[dict(kind='monoscript', file=file,
            location=dict(object_id=pid, field_path=[], container_entry=entry),
            assembly='Synthetic', name=name)], dependencies=[])

    @staticmethod
    def owner(file='data/ui.assets', *, entry=None, external='scripts.assets', script_id=5):
        return dict(types=[], pending=[dict(reason='MONOSCRIPT_DEPENDENCY_MISSING')],
            dependencies=[dict(kind='serialized_reference', file=file, source_class_id=114,
                location=dict(object_id=1, field_path=['m_Script'], container_entry=entry),
                target=dict(file_id=1, object_id=script_id, external_path=external), resolved=False)])

    def test_separately_scanned_external_script_resolves_and_reparse_clears_pending(self):
        prefix = struct.pack('<iqB', 0, 0, 1) + bytes(3) + struct.pack('<iq', 1, 5) + string('PANEL')
        source_raw = serialized([(114, [])], [(1, 0, prefix)], trees=False, externals=['scripts.assets'])
        script = string('SCRIPT') + bytes(20) + string('Panel') + string('Synthetic') + string('Synthetic.dll')
        script_raw = serialized([(115, [])], [(5, 0, script)], trees=False)
        source = scan(source_raw, relative_path='data/ui.assets')
        target = scan(script_raw, relative_path='data/scripts.assets')
        self.assertIn('MONOSCRIPT_DEPENDENCY_MISSING', [row['reason'] for row in source['pending']])
        bindings = objects.bind_script_references([source, target])
        self.assertEqual(bindings, [dict(file='data/ui.assets', container_entry=None, object_id=1,
                                         assembly='Synthetic', name='Synthetic.Panel')])
        managed = {'Synthetic::Synthetic.Panel': dict(name='Synthetic.Panel', assembly='Synthetic',
                    base=None, enum=False, fields=[]), '__script_bindings__': bindings}
        reparsed = scan(source_raw, relative_path='data/ui.assets', managed_types=managed)
        self.assertEqual(reparsed['pending'], [])

    def test_paths_are_physical_not_basename_or_clr_name_guesses(self):
        source = self.owner()
        self.assertEqual(objects.bind_script_references([source, self.script('other/scripts.assets')]), [])
        results = [source, self.script('scripts.assets'), self.script('data/scripts.assets')]
        before = repr(results)
        self.assertEqual(objects.bind_script_references(results), [])
        self.assertEqual(repr(results), before)

    def test_backslashes_and_explicit_relative_components_are_normalized(self):
        source = self.owner('data/scenes/ui.assets', external='..\\scripts.assets')
        bindings = objects.bind_script_references([source, self.script('data/scripts.assets')])
        self.assertEqual(bindings[0]['file'], 'data/scenes/ui.assets')

    def test_archive_reference_prefers_unique_same_physical_container(self):
        source = self.owner('data/source.bundle', entry='owner.assets',
                            external='archive:/catalog/shared.assets')
        local = self.script('data/source.bundle', entry='shared.assets')
        external = self.script('data/other.bundle', entry='shared.assets')
        bindings = objects.bind_script_references([source, local, external])
        self.assertEqual(bindings[0]['container_entry'], 'owner.assets')
        self.assertEqual(bindings[0]['file'], 'data/source.bundle')
        self.assertEqual(objects.bind_script_references([
            source, external, self.script('data/third.bundle', entry='shared.assets')]), [])

    def test_conflicting_descriptors_or_script_edges_remain_unbound(self):
        source = self.owner()
        self.assertEqual(objects.bind_script_references([source, self.script('data/scripts.assets'),
            self.script('data/scripts.assets', name='Synthetic.Other')]), [])
        other_edge = self.owner(external='other.assets')
        self.assertEqual(objects.bind_script_references([source, other_edge,
            self.script('data/scripts.assets'), self.script('data/other.assets')]), [])

    def test_identical_cached_records_are_deduplicated_by_physical_identity(self):
        source, script = self.owner(), self.script('data/scripts.assets')
        self.assertEqual(len(objects.bind_script_references([source, script, source, script])), 1)

    def test_local_script_prefix_survives_unknown_custom_layout(self):
        raw, _ = managed_fixture()
        result = scan(raw)
        self.assertIn('MANAGED_DISPLAY_LAYOUT_UNRESOLVED', [row['reason'] for row in result['pending']])
        bindings = objects.bind_script_references([result])
        self.assertEqual(bindings, [dict(file='data/synthetic.assets', container_entry=None,
            object_id=1, assembly='Synthetic', name='Synthetic.Panel')])


class ObjectPatchTests(unittest.TestCase):
    def test_growth_preserves_opaque_object_gap_and_unmodified_fields(self):
        first = string('ONE', b'P') + struct.pack('<i', 2) + string('SAME', b'Q') + string('') + struct.pack('<iq', 0, 2)
        raw = serialized([(900, nested_tree()), (901, [])], [(1, 0, first), (2, 1, b'OPAQUE_BYTES')])
        before = reader.serialized(raw)
        changed = objects.patch_serialized(raw, [replacement(1, ['items', 0, 'caption'], 'SAME', 'LONGER_VALUE')])
        after = reader.serialized(changed)
        obj = after['objects'][0]
        data = reader.tree_object(changed, obj, after['types'][0])
        self.assertEqual([r['source'] for r in data['strings']], ['ONE', 'LONGER_VALUE', ''])
        a, b = before['objects'][1], after['objects'][1]
        self.assertEqual(raw[a['offset']:a['offset'] + a['size']], changed[b['offset']:b['offset'] + b['size']])
        self.assertEqual(changed[obj['offset']:obj['offset'] + len(string('ONE', b'P'))], string('ONE', b'P'))
        old_metadata = bytearray(raw[:before['data_offset']])
        new_metadata = bytearray(changed[:after['data_offset']])
        old_metadata[24:32] = new_metadata[24:32] = bytes(8)
        for row in before['objects']:
            p = row['table_pos'] + 8
            old_metadata[p:p + 12] = new_metadata[p:p + 12] = bytes(12)
        self.assertEqual(old_metadata, new_metadata)

    def test_same_length_preserves_nonzero_padding_exactly(self):
        raw = serialized([(102, text_tree())], [(1, 0, string('ONE', b'P'))])
        patched = objects.patch_serialized(raw, [replacement(1, ['m_Text'], 'ONE', 'TWO')])
        original_obj = reader.serialized(raw)['objects'][0]
        offset = original_obj['offset']
        self.assertEqual(patched[:offset + 4], raw[:offset + 4])
        self.assertEqual(patched[offset + 7:], raw[offset + 7:])

    def test_roundtrip_and_empty_replacement(self):
        raw = serialized([(102, text_tree())], [(1, 0, string('SOURCE'))])
        changed = objects.patch_serialized(raw, [replacement(1, ['m_Text'], 'SOURCE', '')])
        restored = objects.patch_serialized(changed, [replacement(1, ['m_Text'], '', 'SOURCE')])
        # Relocation can introduce alignment bytes, but every original object byte returns.
        first, last = reader.serialized(raw), reader.serialized(restored)
        a, b = first['objects'][0], last['objects'][0]
        self.assertEqual(raw[a['offset']:a['offset'] + a['size']], restored[b['offset']:b['offset'] + b['size']])
        self.assertEqual(objects.patch_serialized(raw, []), raw)

    def test_source_file_object_and_field_bindings_are_checked(self):
        raw = serialized([(102, text_tree())], [(1, 0, string('SOURCE'))])
        bad = [replacement(1, ['m_Text'], 'WRONG', 'T'), replacement(9, ['m_Text'], 'SOURCE', 'T'),
               replacement(1, ['wrong'], 'SOURCE', 'T')]
        for row in bad:
            with self.subTest(location=row['location']), self.assertRaises(ValueError):
                objects.patch_serialized(raw, [row])
        row = replacement(1, ['m_Text'], 'SOURCE', 'T')
        row['file_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'SOURCE_FILE_BINDING_MISMATCH'):
            objects.patch_serialized(raw, [row])
        row['file_sha256'] = hashlib.sha256(raw).hexdigest()
        with self.assertRaisesRegex(ValueError, 'REPLACEMENT_BINDING_AMBIGUOUS'):
            objects.patch_serialized(raw, [row, row])

    def test_json_and_ini_entry_patch_preserves_unowned_payload_characters(self):
        for payload, location, expected in [
            ('{ "v" : ["SAME", "SAME"] }', dict(parser='json', parser_path=['v', 1]), '{ "v" : ["SAME", "NEW"] }'),
            ('[section]\r\nk=SAME\r\nk=SAME\r\n', dict(parser='ini', parser_path=['section', 'k', 1]),
             '[section]\r\nk=SAME\r\nk=NEW\r\n')]:
            raw = serialized([(49, [])], [(1, 0, string('TABLE') + string(payload))], trees=False)
            patched = objects.patch_serialized(raw, [replacement(1, ['m_Script'], 'SAME', 'NEW', **location)])
            result = scan(patched)
            full = next(r for r in result['occurrences'] if r['kind'] == 'textasset' and 'parser_path' not in r['location'])
            self.assertEqual(full['source'], expected)

    def test_uncompressed_bundle_rebuild_preserves_unowned_entry(self):
        entry = serialized([(102, text_tree())], [(1, 0, string('SOURCE'))])
        for flags in (0x40, 0xc0, 0x240, 0x2c0):
            raw = unityfs([('synthetic.assets', entry, 4), ('unowned.resS', b'OPAQUE_MEDIA', 0)], flags=flags)
            changed = objects.patch_serialized(raw, [replacement(1, ['m_Text'], 'SOURCE', 'BUNDLE_TARGET',
                                                                container_entry='synthetic.assets')])
            doc = reader.bundle(changed)
            unowned = doc['entries'][1]
            self.assertEqual(doc['payload'][unowned['offset']:unowned['offset'] + unowned['size']], b'OPAQUE_MEDIA')
            value = scan(changed)['occurrences'][0]
            self.assertEqual(value['source'], 'BUNDLE_TARGET')
            self.assertEqual(value['location']['container_entry'], 'synthetic.assets')

    def test_stripped_managed_patch_preserves_other_duplicate_occurrence(self):
        raw, types_ = managed_fixture()
        changed = objects.patch_serialized(raw, [replacement(1, ['items', 1, 'caption'], 'DUPLICATE', 'NEW')],
                                           managed_types=types_)
        captions = [r['source'] for r in scan(changed, managed_types=types_)['occurrences']
                    if r['location']['field_path'][0] == 'items']
        self.assertEqual(captions, ['DUPLICATE', 'NEW'])

    def test_unsupported_codec_is_not_a_fake_decompression(self):
        with self.assertRaisesRegex(ValueError, 'BUNDLE_CODEC_UNSUPPORTED'):
            reader._decode_block(b'not-compressed', 4, 99)
        with self.assertRaisesRegex(ValueError, 'BUNDLE_DECOMPRESSED_SIZE_MISMATCH'):
            reader._decode_block(b'abc', 0, 99)
        with self.assertRaisesRegex(ValueError, 'LZMA_DATA_INVALID'):
            reader._decode_block(b'\xe0' + struct.pack('<I', 4096) + b'\0', 1, 4)

    def test_texture_object_patch_never_opens_pixel_layout(self):
        raw = serialized([(28, [node(0, 'Texture2D', 'Base'), node(1, 'string', 'm_Name')])],
                         [(1, 0, string('TEXTURE_ID'))])
        with self.assertRaisesRegex(ValueError, 'PIXEL_OBJECT_PATCH_FORBIDDEN'):
            objects.patch_serialized(raw, [replacement(1, ['m_Name'], 'TEXTURE_ID', 'OTHER')])


if __name__ == '__main__':
    unittest.main()
