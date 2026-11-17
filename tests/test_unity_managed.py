"""Synthetic actual PE/CLI fixtures; no assemblies, packages or runtime needed."""
import importlib.util
from pathlib import Path
import struct
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1] / 'profiles/sdk-unity-2022.3/tools'
for name in ('unity_clr', 'unity_dotnet_resources', 'unity_pe', 'unity_managed', 'unity_managed_fixture', 'unity_native_calls'):
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
m = sys.modules['unity_managed']
c = sys.modules['unity_clr']
fixture = sys.modules['unity_managed_fixture'].fixture
token = sys.modules['unity_managed_fixture'].token




class ManagedTests(unittest.TestCase):
    def scan(self, raw, method='primary'):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Synthetic.dll'
            path.write_bytes(raw)
            return m.scan_assembly(path, 'Managed/Synthetic.dll', method=method)

    def test_methods_heap_enumeration_and_duplicate_physical_uses(self):
        raw = fixture()
        primary = self.scan(raw)
        independent = self.scan(raw, 'independent')
        self.assertEqual(primary['occurrences'], independent['occurrences'])
        occurrences = primary['occurrences']
        self.assertEqual(len(occurrences), 4)
        self.assertEqual([o['location']['il_offset'] for o in occurrences], [1, 11, 4, 11])
        self.assertEqual([o['classification'] for o in occurrences], ['display', 'uncertain', 'display', 'display'])
        self.assertEqual(occurrences[0]['source'], occurrences[1]['source'])
        self.assertNotEqual(occurrences[0]['location'], occurrences[1]['location'])
        self.assertNotEqual(primary['proofs']['file']['enumeration'], independent['proofs']['file']['enumeration'])
        self.assertTrue(any(d['kind'] == 'call' for d in primary['dependencies']))

    def test_unity_layout_and_structural_field_consumer(self):
        result = self.scan(fixture())
        panel = result['types']['Synthetic::Synthetic.Panel']
        self.assertEqual(panel['base'], {'kind': 'named', 'name': 'UnityEngine.MonoBehaviour', 'assembly': 'UnityEngine.CoreModule'})
        fields = panel['fields']
        self.assertEqual([f['serialized'] for f in fields], [True, True, True, False])
        self.assertEqual(fields[2]['type'], {'kind': 'array', 'element': {'kind': 'primitive', 'name': 'int32'}})
        self.assertEqual(fields[0]['classification'], 'display')
        self.assertEqual(fields[0]['display_consumers'][0]['method_token'], 0x06000002)
        self.assertNotIn('classification', fields[1])
        self.assertTrue(self.scan(fixture(unsupported_field=True))['pending'])

    def test_selected_use_rewrite_preserves_shared_original_token(self):
        raw = fixture()
        before = self.scan(raw)
        selected = before['occurrences'][0]
        changed = m.patch_assembly(raw, [{'location': selected['location'], 'source': selected['source'], 'target': 'SYNTHETIC EXPANDED TARGET ' * 40}])
        after = self.scan(changed)
        self.assertEqual(after['occurrences'][0]['source'], 'SYNTHETIC EXPANDED TARGET ' * 40)
        self.assertEqual(after['occurrences'][1]['source'], before['occurrences'][1]['source'])
        self.assertEqual(after['occurrences'][2]['source'], before['occurrences'][2]['source'])
        self.assertNotEqual(after['occurrences'][0]['location']['string_token'], after['occurrences'][1]['location']['string_token'])
        old_image, new_image = c.Image(raw), c.Image(changed)
        self.assertEqual(len(new_image.sections), len(old_image.sections) + 1)
        self.assertEqual(old_image.streams['#~'], new_image.streams['#~'])
        self.assertEqual(old_image.streams['#Strings'], new_image.streams['#Strings'])
        self.assertEqual(old_image.body(2)['code'], new_image.body(2)['code'])
        self.assertTrue(new_image.streams['#US'].startswith(old_image.streams['#US']))
        self.assertEqual(self.scan(changed, 'independent')['occurrences'], after['occurrences'])

    def test_pe32_plus_and_fat_body(self):
        raw = fixture(codes=[b'\x00' * 70 + token(0x72, 0x70000001) + b'\x26\x2a'], pe64=True)
        occurrence = self.scan(raw)['occurrences'][0]
        self.assertEqual(occurrence['location']['il_offset'], 70)
        patched = m.patch_assembly(raw, [{'location': occurrence['location'], 'source': occurrence['source'], 'target': ''}])
        self.assertEqual(self.scan(patched)['occurrences'][0]['source'], '')

    def test_replacement_binding_validation(self):
        raw = fixture()
        row = self.scan(raw)['occurrences'][0]
        good = {'location': row['location'], 'source': row['source'], 'target': 'TARGET'}
        cases = [({**good, 'source': 'WRONG'}, 'SOURCE_MISMATCH'),
                 ({**good, 'location': {**row['location'], 'il_offset': 2}}, 'NOT_LDSTR'),
                 ({**good, 'location': {**row['location'], 'string_token': 0x700000ff}}, 'TOKEN_MISMATCH'),
                 ({**good, 'location': {**row['location'], 'method_token': 0x04000001}}, 'LOCATION')]
        for replacement, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                m.patch_assembly(raw, [replacement])
        with self.assertRaisesRegex(ValueError, 'DUPLICATE'):
            m.patch_assembly(raw, [good, good])
        self.assertIs(m.patch_assembly(raw, []), raw)

    def test_signed_and_unsafe_layouts_are_not_mutated(self):
        for raw, error in [(fixture(strong=True), 'SIGNED_OR_MIXED'), (fixture() + b'OVERLAY', 'OVERLAY')]:
            occurrence = self.scan(raw)['occurrences'][0]
            with self.assertRaisesRegex(ValueError, error):
                m.patch_assembly(raw, [{'location': occurrence['location'], 'source': occurrence['source'], 'target': 'TARGET'}])
        # Signing never excludes inventory.
        self.assertEqual(len(self.scan(fixture(strong=True))['occurrences']), 4)

    def test_invalid_il_token_and_branch_boundaries(self):
        with self.assertRaisesRegex(ValueError, 'LDSTR_BINDING'):
            self.scan(fixture(codes=[token(0x72, 0x70000002) + b'\x26\x2a']))
        result = self.scan(fixture(codes=[b'\x2b\x01' + token(0x72, 0x70000001) + b'\x26\x2a']))
        self.assertTrue(any(p['reason'] == 'managed_method_decode_unsupported' for p in result['pending']))
        result = self.scan(fixture(codes=[b'\xff\x2a']))
        self.assertTrue(result['pending'])
        self.assertEqual(result['occurrences'], [])

    def test_valid_native_integer_types_remain_readable_without_guessing_unity_storage(self):
        image = c.Image(fixture())
        for element in (0x16, 0x18, 0x19, 0x1c):
            with self.subTest(element=element):
                descriptor, end = image.signature(bytes([element]))
                self.assertEqual(descriptor, {'kind': 'unsupported', 'element_type': element})
                self.assertEqual(end, 1)
        descriptor, end = image.signature(b'\x1d\x18')
        self.assertEqual(descriptor, {'kind': 'array', 'element': {'kind': 'unsupported', 'element_type': 0x18}})
        self.assertEqual(end, 2)

    def test_malformed_pe_and_metadata_bounds(self):
        raw = fixture()
        for malformed in (b'', raw[:80], raw[:0x230]):
            with self.subTest(length=len(malformed)), self.assertRaises(ValueError):
                c.Image(malformed)
        image = c.Image(raw)
        malformed = bytearray(raw)
        header = image.stream_headers['#US'][0]
        struct.pack_into('<I', malformed, image.root + header + 4, len(raw) * 2)
        with self.assertRaisesRegex(ValueError, 'BOUNDS'):
            c.Image(bytes(malformed))

    def test_name_only_sink_is_not_evidence(self):
        raw = fixture().replace(b'UnityEngine.UI\0', b'NonDisplay.Api\0')
        result = self.scan(raw)
        self.assertTrue(all(o['classification'] == 'uncertain' for o in result['occurrences']))
        self.assertFalse(any(p['scope'] == 'display_dependency' for p in result['pending']))

    def test_unknown_transformation_on_sink_path_stays_pending(self):
        code = b'\x14' + token(0x72, 0x70000001) + b'\x8e' + token(0x6f, 0x0a000001) + b'\x2a'
        result = self.scan(fixture(codes=[code]))
        self.assertTrue(any(p['scope'] == 'display_dependency' for p in result['pending']))
        self.assertEqual(result['occurrences'][0]['classification'], 'display')

    def test_cross_assembly_sink_argument_without_local_input_stays_pending(self):
        caller = fixture(codes=[token(0x72, 0x70000001) + token(0x28, 0x0a000003) + b'\x2a'],
                         external_helper_assembly='Synthetic.Helper')
        helper = fixture(codes=[b'\x14\x02' + token(0x6f, 0x0a000001) + b'\x2a'],
                         string_argument=True, assembly_name='Synthetic.Helper')
        for method in ('primary', 'independent'):
            with self.subTest(method=method):
                a, b = self.scan(caller, method), self.scan(helper, method)
                self.assertEqual(len(a['occurrences']), 1)
                self.assertEqual(a['occurrences'][0]['classification'], 'uncertain')
                self.assertEqual(b['occurrences'], [])
                unbound = [p for p in b['pending'] if p['reason'] == 'managed_display_argument_input_unbound']
                self.assertEqual(len(unbound), 1)
                self.assertEqual(unbound[0]['dependency'], 'argument:100663297:0')
                self.assertEqual(unbound[0]['consumers'][0]['sink']['member'], 'set_text')

    def test_local_sink_argument_with_bound_literal_is_not_unbound(self):
        raw = fixture(codes=[b'\x14\x02' + token(0x6f, 0x0a000001) + b'\x2a',
                             token(0x72, 0x70000001) + token(0x28, 0x06000001) + b'\x2a'],
                      string_argument=True)
        for method in ('primary', 'independent'):
            result = self.scan(raw, method)
            self.assertEqual(result['occurrences'][0]['classification'], 'display')
            self.assertFalse(any(p['reason'] == 'managed_display_argument_input_unbound' for p in result['pending']))


    def test_embedded_resources_v1_v2_and_selected_rewrite(self):
        public_fixture = sys.modules['unity_managed_fixture']
        for version in (1, 2):
            raw = fixture(resources=public_fixture.resource_fixture(version))
            before = self.scan(raw)
            resource_rows = [o for o in before['occurrences'] if o['kind'] == 'managed_resource']
            self.assertEqual([o['location']['entry_index'] for o in resource_rows], [0, 1])
            self.assertEqual([o['source'] for o in resource_rows], ['SYNTHETIC RESOURCE', 'SYNTHETIC RESOURCE OTHER'])
            self.assertEqual(before['occurrences'], self.scan(raw, 'independent')['occurrences'])
            selected = resource_rows[0]
            changed = m.patch_assembly(raw, [{'location': selected['location'], 'source': selected['source'], 'target': 'SYNTHETIC RESOURCE TARGET ' * 10}])
            after = self.scan(changed)
            updated = [o for o in after['occurrences'] if o['kind'] == 'managed_resource']
            self.assertEqual(updated[0]['source'], 'SYNTHETIC RESOURCE TARGET ' * 10)
            self.assertEqual(updated[1]['source'], resource_rows[1]['source'])
            self.assertEqual(c.Image(raw).body(1)['code'], c.Image(changed).body(1)['code'])
            old_resource = sys.modules['unity_dotnet_resources'].inventory(c.Image(raw))[2][0x28000001]['raw']
            new_resource = sys.modules['unity_dotnet_resources'].inventory(c.Image(changed))[2][0x28000001]['raw']
            self.assertIn(struct.pack('<i', 42), new_resource)
            self.assertGreater(len(new_resource), len(old_resource))
            with self.assertRaisesRegex(ValueError, 'INDEX'):
                m.patch_assembly(raw, [{'location': {**selected['location'], 'entry_index': 99}, 'source': selected['source'], 'target': 'TARGET'}])

    def test_header_growth_preserves_rvas_and_physical_bindings(self):
        raw = sys.modules['unity_managed_fixture'].build_header_growth_fixture()
        before = self.scan(raw)
        row = before['occurrences'][0]
        changed = m.patch_assembly(raw, [{'location': row['location'], 'source': row['source'], 'target': 'SYNTHETIC HEADER TARGET'}])
        old, new = c.Image(raw), c.Image(changed)
        self.assertEqual(new.header_size, 1024)
        self.assertEqual(new.sections[0]['rva'], old.sections[0]['rva'])
        self.assertEqual(new.sections[0]['offset'], old.sections[0]['offset'] + 512)
        self.assertEqual(old.body(2)['code'], new.body(2)['code'])
        after = self.scan(changed)
        self.assertEqual(after['occurrences'][0]['source'], 'SYNTHETIC HEADER TARGET')
        self.assertEqual(after['occurrences'][1:], before['occurrences'][1:])


    def test_resource_lookup_values_not_keys_are_display_targets(self):
        resource_bytes = sys.modules['unity_managed_fixture'].resource_fixture()
        result = self.scan(fixture(resources=resource_bytes, resource_flow=True))
        literals = [o for o in result['occurrences'] if o['kind'] == 'managed_literal']
        self.assertEqual([o['classification'] for o in literals], ['non_display', 'non_display'])
        values = [o for o in result['occurrences'] if o['kind'] == 'managed_resource']
        self.assertEqual([o['classification'] for o in values], ['display', 'uncertain'])
        self.assertTrue(any(p['reason'] == 'resource_culture_resolution_unverified' for p in result['pending']))
        linked = self.scan(fixture(resources=resource_bytes, resource_flow=True, linked_resource=True))
        self.assertTrue(any(p['reason'] == 'linked_managed_resource_unresolved' for p in linked['pending']))

    def test_display_literal_shared_with_resource_identifier_is_not_writable(self):
        raw = fixture(resources=sys.modules['unity_managed_fixture'].resource_fixture(),
                      resource_flow=True, shared_resource_key=True)
        result = self.scan(raw)
        self.assertEqual(result['occurrences'], self.scan(raw, 'independent')['occurrences'])
        literals = [row for row in result['occurrences'] if row['kind'] == 'managed_literal']
        self.assertEqual([row['classification'] for row in literals], ['non_display', 'display'])
        key = literals[1]
        self.assertEqual(key['source'], 'a')
        self.assertTrue(key['display_projection_required'])
        self.assertTrue(any(p['reason'] == 'managed_shared_identifier_write_unsafe' for p in result['pending']))
        with self.assertRaisesRegex(ValueError, 'MANAGED_SHARED_IDENTIFIER_WRITE_UNSAFE'):
            m.patch_assembly(raw, [{'location': key['location'], 'source': key['source'], 'target': 'TRANSLATED'}])
        value = next(row for row in result['occurrences'] if row['kind'] == 'managed_resource')
        changed = m.patch_assembly(raw, [{'location': value['location'], 'source': value['source'], 'target': 'VALUE TARGET'}])
        after = self.scan(changed)
        self.assertEqual([row['source'] for row in after['occurrences'] if row['kind'] == 'managed_literal'],
                         [row['source'] for row in literals])
        self.assertEqual(next(row for row in after['occurrences'] if row['kind'] == 'managed_resource')['source'], 'VALUE TARGET')


    def test_native_iat_unwind_and_selected_reference_projection(self):
        native = sys.modules['unity_native_calls']
        raw = sys.modules['unity_managed_fixture'].build_native_fixture()
        before = native.scan_native_dependencies(raw, 'SyntheticNative.dll')
        self.assertEqual([o['source'] for o in before['occurrences']], ['SYNTHETIC NATIVE SOURCE', 'SYNTHETIC CAPTION'])
        self.assertTrue(all(not o['projection_required'] for o in before['occurrences']))
        self.assertEqual(before['occurrences'], native.scan_native_dependencies(raw, 'SyntheticNative.dll', method='independent')['occurrences'])
        row = before['occurrences'][0]
        changed = native.patch_native_literals(raw, [{'location': row['location'], 'source': row['source'], 'target': 'SYNTHETIC NATIVE TARGET ' * 20}])
        after = native.scan_native_dependencies(changed, 'SyntheticNative.dll')
        by_use = {o['location']['use_rva']: o for o in after['occurrences']}
        self.assertEqual(by_use[0x2080]['source'], 'SYNTHETIC NATIVE TARGET ' * 20)
        self.assertEqual(by_use[0x2087]['source'], 'SYNTHETIC CAPTION')
        self.assertEqual(native._cstring(sys.modules['unity_pe'].PE(changed), 0x4000, 'utf-16-le'), row['source'])
        self.assertFalse(after['proofs']['static_complete'])


    def test_native_projection_and_managed_code_guard(self):
        native = sys.modules['unity_native_calls']
        self.assertEqual(native.scan_native_dependencies(fixture(), 'Synthetic.dll')['occurrences'], [])
        raw = bytearray(sys.modules['unity_managed_fixture'].build_native_fixture())
        # A boundary-unsupported opcode keeps nominations but forbids rewriting.
        raw[0x280 + 20] = 0x62
        result = native.scan_native_dependencies(bytes(raw), 'SyntheticNative.dll')
        self.assertTrue(result['occurrences'])
        self.assertTrue(all(o['display_projection_required'] for o in result['occurrences']))
        row = result['occurrences'][0]
        with self.assertRaisesRegex(ValueError, 'PROJECTION_REQUIRED'):
            native.patch_native_literals(bytes(raw), [{'location': row['location'], 'source': row['source'], 'target': 'TARGET'}])

    def test_native_carried_display_iat_retains_only_bound_possible_scope(self):
        native = sys.modules['unity_native_calls']
        raw = sys.modules['unity_managed_fixture'].build_native_fixture(carried_import=True, unrelated_function=True)
        primary = native.scan_native_dependencies(raw, 'SyntheticNative.dll')
        independent = native.scan_native_dependencies(raw, 'SyntheticNative.dll', method='independent')
        self.assertEqual(primary['occurrences'], independent['occurrences'])
        self.assertEqual([row['location']['use_rva'] for row in primary['occurrences']], [0x2080, 0x2087])
        self.assertTrue(all(row['classification'] == 'uncertain' and row['display_projection_required']
                            for row in primary['occurrences']))
        self.assertTrue(any(edge['kind'] == 'native_possible_display_import_reference'
                            and edge['iat_rva'] == 0x40d0 for edge in primary['dependencies']))
        self.assertTrue(any(p['reason'] == 'native_indirect_call_unresolved' for p in primary['pending']))
        self.assertNotIn(str(0x2100), primary['proofs']['functions'])
        selected = primary['occurrences'][0]
        with self.assertRaisesRegex(ValueError, 'PROJECTION_REQUIRED'):
            native.patch_native_literals(raw, [{'location': selected['location'], 'source': selected['source'], 'target': 'TARGET'}])
        unrelated = raw.replace(b'MessageBoxW', b'NotDisplayW')
        self.assertEqual(native.scan_native_dependencies(unrelated, 'SyntheticNative.dll')['occurrences'], [])


if __name__ == '__main__':
    unittest.main()
