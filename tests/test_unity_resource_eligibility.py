"""Synthetic consumer-visible resource scope and copied-payload integrity."""
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1] / 'profiles/sdk-unity-2022.3/tools'
sys.path.insert(0, str(TOOLS))
from game_l10n.formats import read_segments
from unity_inventory import scan_snapshot
from unity_inventory_cache import ScanCache, occurrence_id
from unity_native import _leaves, patch_native, scan_native
from unity_native_fixture import build_resource_fixture, build_resource_consumer_fixture
from unity_pe import PE


class ResourceEligibilityTests(unittest.TestCase):
    def inventory(self, root, raw, *, name='SyntheticPlayer.exe', method='primary'):
        source = root / name
        source.write_bytes(raw)
        result = scan_native(source, name, method=method)
        output = root / (method + '.json')
        with ScanCache(root / 'evidence.sqlite3') as cache:
            cache.store(method, name, 'synthetic-source', 'synthetic-scanner', result)
            summary = cache.inventory(method, output, 'synthetic-implementation')
            census = list(cache.occurrences(method))
        return json.loads(output.read_text(encoding='utf-8')), summary, census, result

    def test_snapshot_producer_reconciles_only_exact_resource_consumer_gaps(self):
        cases = [({}, 1, set()),
                 ({'message': True}, 1, set()),
                 ({'extra_display_argument': True}, 1, {'native_display_argument_unresolved'}),
                 ({'branch_before': True}, 0, {'native_resource_display_flow_unresolved'}),
                 ({'interprocedural': True}, 0, {'native_resource_display_flow_unresolved'}),
                 ({'ansi': True}, 0, {'native_resource_loader_unsupported'}),
                 ({'display': False}, 0, set())]
        for options, count, expected_gaps in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                snapshot = root / 'snapshot'
                snapshot.mkdir()
                raw = build_resource_consumer_fixture(**options)
                source = snapshot / 'SyntheticPlayer.exe'
                source.write_bytes(raw)
                manifest = {'schema': 'parr0t.unity-source-snapshot.v1',
                            'unity_version': '2022.3.0f1',
                            'files': [{'file': source.name, 'size': len(raw),
                                       'sha256': hashlib.sha256(raw).hexdigest()}]}
                emitted = []
                for method in ('primary', 'independent'):
                    output = root / (method + '-snapshot.json')
                    cache_path = root / (method + '-snapshot.sqlite3')
                    summary = scan_snapshot(snapshot, manifest, cache_path, output, method=method)
                    segments = read_segments(output)
                    self.assertEqual(len(segments), count)
                    self.assertTrue(all(row.metadata['unity_kind'] == 'native_resource' for row in segments))
                    self.assertTrue(all(row.metadata['location']['entry_index'] == 0 for row in segments))
                    with ScanCache(cache_path) as cache:
                        pending = cache.summary(method)['pending']
                        raw_rows = list(cache.occurrences(method))
                    reasons = {row['reason'] for row in pending}
                    self.assertTrue(expected_gaps <= reasons)
                    if not options or options.get('message'):
                        self.assertEqual(reasons, {'scanner_static_completeness_unverified'})
                    if options.get('extra_display_argument'):
                        arguments = [row for row in pending if row['reason'] == 'native_display_argument_unresolved']
                        self.assertEqual([row['register'] for row in arguments], [8])
                        self.assertNotIn('native_indirect_call_unresolved', reasons)
                    if options.get('display') is False:
                        self.assertEqual(segments, [])
                        self.assertEqual(pending, [])
                    self.assertGreater(len(raw_rows), len(segments))
                    self.assertEqual(source.read_bytes(), raw)
                    # Reuse the same content-bound producer/cache, not a second
                    # hand-built evidence path; pending and scope must survive.
                    reused = scan_snapshot(snapshot, manifest, cache_path, output, method=method)
                    self.assertEqual(reused['pending_count'], summary['pending_count'])
                    self.assertEqual(reused['reused_files'], 1)
                    emitted.append([(row.id, row.source) for row in read_segments(output)])
                self.assertEqual(*emitted)

    def test_resource_membership_is_only_census_even_in_engine_named_modules(self):
        for resource_type in (6, 11):
            if resource_type == 11:
                raw = build_resource_consumer_fixture(message=True, display=False)
            else:
                raw = build_resource_fixture()
            with tempfile.TemporaryDirectory() as directory:
                for name in ('UnityPlayer.dll', 'EngineDebug.dll', 'SyntheticGame.exe'):
                    document, summary, census, _ = self.inventory(Path(directory), raw, name=name)
                    self.assertEqual(document['segments'], [])
                    self.assertGreater(summary['unclassified_count'], 0)
                    self.assertTrue(any(row['source'] for row in census))
                    self.assertEqual(summary['pending_count'], 0)

    def test_exact_loadstring_buffer_use_selects_only_referenced_id(self):
        raw = build_resource_consumer_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventories = []
            for method in ('primary', 'independent'):
                document, summary, census, result = self.inventory(root, raw, method=method)
                self.assertEqual(summary['raw_occurrence_count'], 16)
                self.assertEqual(summary['display_candidate_count'], 1)
                self.assertEqual(summary['unclassified_count'], 1)
                self.assertEqual([row['metadata']['location']['entry_index'] for row in document['segments']], [0])
                self.assertEqual(document['segments'][0]['metadata']['display_scope_evidence']['kind'], 'positive_display_binding')
                self.assertEqual(result['pending'], [])
                self.assertEqual(census[0]['source'], census[1]['source'])
                inventories.append([(row['id'], row['source']) for row in document['segments']])
            self.assertEqual(*inventories)

    def test_module_message_to_ui_is_kept_without_promoting_other_messages(self):
        raw = build_resource_consumer_fixture(message=True)
        with tempfile.TemporaryDirectory() as directory:
            for method in ('primary', 'independent'):
                document, summary, _, _ = self.inventory(Path(directory), raw, method=method)
                self.assertEqual([row['source'] for row in document['segments']], ['SYNTHETIC SYSTEM %1!s!%n'])
                self.assertEqual(summary['unclassified_count'], 1)
                self.assertTrue(document['coverage']['declared_complete'])

    def test_unresolved_real_consumers_hold_coverage_without_promoting_tables(self):
        cases = [{'wrong_buffer': True}, {'unknown_id': True}, {'branch': True},
                 {'overwrite': True}, {'dll': True}, {'message': True, 'dll': True},
                 {'branch_before': True}, {'interprocedural': True}, {'ansi': True}]
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                for method in ('primary', 'independent'):
                    document, summary, census, result = self.inventory(Path(directory), build_resource_consumer_fixture(**case), method=method)
                    self.assertEqual(document['segments'], [])
                    self.assertTrue(any(row['source'] for row in census))
                    self.assertFalse(document['coverage']['declared_complete'])
                    self.assertGreater(summary['pending_count'], 0)
                    self.assertTrue(any(row['scope'] == 'display_dependency' for row in result['pending']))

    def test_bound_but_missing_id_and_system_fallback_are_explicit_pending(self):
        cases = [({'identifier': 31}, 'native_display_resource_missing'),
                 ({'missing_resource': True}, 'native_display_resource_missing'),
                 ({'message': True, 'system_fallback': True}, 'native_system_message_fallback_unresolved')]
        for case, reason in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                document, _, _, result = self.inventory(Path(directory), build_resource_consumer_fixture(**case))
                self.assertFalse(document['coverage']['declared_complete'])
                self.assertTrue(any(row['reason'] == reason for row in result['pending']))
                self.assertEqual(len(document['segments']), 1 if case.get('system_fallback') else 0)

    def test_menu_and_dialog_caption_roles_do_not_need_string_table_inference(self):
        title = 'SYNTHETIC CAPTION ' + chr(0x2605)
        menu = struct.pack('<HHHH', 0, 0, 0x80, 1) + title.encode('utf-16-le') + b'\0\0'
        dialog = struct.pack('<IIHhhhh', 0, 0, 0, 0, 0, 40, 20) + b'\0\0\0\0' + title.encode('utf-16-le') + b'\0\0'
        for kind, payload in ((4, menu), (5, dialog)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                document, _, _, _ = self.inventory(Path(directory), build_resource_fixture(resource_type=kind, payload=payload), name='EngineDebug.dll')
                self.assertEqual([row['source'] for row in document['segments']], [title])

    def test_selected_resource_copy_roundtrip_preserves_source_and_unowned_bytes(self):
        for message in (False, True):
            original = build_resource_consumer_fixture(message=message)
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                document, _, census, _ = self.inventory(root, original)
                selected = document['segments'][0]
                target = 'SYNTHETIC ' + ''.join(chr(code) for code in (0x2605, 0x2606)) + (' %1!s!%n' if message else '')
                replacement = {'location': selected['metadata']['location'], 'source': selected['source'], 'target': target}
                copied = patch_native(original, [replacement])
                copied_path = root / 'CopiedPlayer.exe'
                copied_path.write_bytes(copied)
                self.assertEqual((root / 'SyntheticPlayer.exe').read_bytes(), original)
                old_pe, new_pe = PE(original), PE(copied)
                for section in old_pe.sections:
                    old_bytes = original[section['offset']:section['offset'] + section['size']]
                    offset = new_pe.at(section['rva'], section['size'])
                    self.assertEqual(copied[offset:offset + section['size']], old_bytes)
                changed = scan_native(copied_path, 'SyntheticPlayer.exe')['occurrences']
                self.assertEqual([occurrence_id(row) for row in census], [occurrence_id(row) for row in changed])
                self.assertEqual([row['source'] for row in changed], [target] + [row['source'] for row in census[1:]])
                restored = patch_native(copied, [{'location': replacement['location'], 'source': target, 'target': replacement['source']}])
                self.assertEqual([(leaf['path'], leaf['data']) for leaf in _leaves(restored)[1]],
                                 [(leaf['path'], leaf['data']) for leaf in _leaves(original)[1]])


if __name__ == '__main__':
    unittest.main()
