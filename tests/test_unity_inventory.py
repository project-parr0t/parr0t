from __future__ import annotations

import copy
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "profiles/sdk-unity-2022.3/tools"
sys.path.insert(0, str(TOOLS))

from unity_inventory_cache import ScanCache, file_digest, occurrence_id
from unity_native import _rebuild_tree, patch_native, scan_native
from unity_native_fixture import build_resource_fixture
from unity_managed import scan_assembly
from unity_managed_fixture import fixture, token, build_native_fixture, resource_fixture
from unity_native_calls import scan_native_dependencies


class InventoryCacheTests(unittest.TestCase):
    def test_duplicate_values_keep_physical_ids_but_heap_provenance_does_not_rename_them(self):
        row = {"file": "Synthetic_Data/Managed/Synthetic.dll", "kind": "managed_literal", "location": {"method_token": 0x06000001, "il_offset": 1, "string_token": 0x70000001}, "source": "SYNTHETIC SOURCE"}
        changed = copy.deepcopy(row)
        changed["location"]["string_token"] = 0x70000081
        changed["source"] = "SYNTHETIC TARGET"
        self.assertEqual(occurrence_id(row), occurrence_id(changed))
        changed["location"]["il_offset"] = 11
        self.assertNotEqual(occurrence_id(row), occurrence_id(changed))

    def test_native_relocation_preserves_use_identity_without_merging_distinct_uses(self):
        row = {"file": "SyntheticPlayer.dll", "kind": "native_literal", "location": {"rva": 0x4000, "use_rva": 0x2080, "encoding": "utf-16-le"}}
        moved = copy.deepcopy(row)
        moved["location"]["rva"] = 0x9000
        self.assertEqual(occurrence_id(row), occurrence_id(moved))
        moved["location"]["use_rva"] += 7
        self.assertNotEqual(occurrence_id(row), occurrence_id(moved))

    def test_table_entries_and_raw_payload_never_collapse(self):
        row = {"file": "Synthetic_Data/data.unity3d", "kind": "textasset", "location": {"container_entry": "synthetic.assets", "object_id": 2, "field_path": ["m_Script"]}}
        first = copy.deepcopy(row)
        first["location"].update(parser="json", parser_path=["rows", 0], entry_index=0)
        second = copy.deepcopy(first)
        second["location"].update(parser_path=["rows", 1], entry_index=1)
        self.assertEqual(len({occurrence_id(value) for value in [row, first, second]}), 3)
        root = copy.deepcopy(first)
        root["location"]["parser_path"] = []
        self.assertNotEqual(occurrence_id(root), occurrence_id(first))

    def test_lossless_shared_proof_storage_and_method_isolation(self):
        proof = {"instructions": ["SYNTHETIC STRUCTURAL EVIDENCE"] * 1500}
        occurrences = [{"kind": "managed_literal", "file": "Synthetic.dll", "location": {"method_token": 0x06000001, "il_offset": index}, "source": "SYNTHETIC DISPLAY", "classification": "display", "reason": "SYNTHETIC_UI_SINK", "proof": proof} for index in range(200)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ScanCache(root / "evidence.sqlite3") as cache:
                cache.store("primary", "Synthetic.dll", "source-v1", "scanner-v1", {"occurrences": occurrences})
                actual = list(cache.occurrences("primary"))
                self.assertEqual([(row["source"], row["location"]["il_offset"]) for row in actual], [("SYNTHETIC DISPLAY", index) for index in range(200)])
                self.assertEqual(list(cache.occurrences("independent")), [])
                self.assertTrue(cache.reusable("primary", "Synthetic.dll", "source-v1", "scanner-v1"))
                self.assertFalse(cache.reusable("primary", "Synthetic.dll", "source-v2", "scanner-v1"))
                self.assertFalse(cache.reusable("primary", "Synthetic.dll", "source-v1", "scanner-v2"))
                cache.inventory("primary", root / "inventory.json", "synthetic-implementation")
            emitted = json.loads((root / "inventory.json").read_text(encoding="utf-8"))
            self.assertEqual([row["source"] for row in emitted["segments"]], ["SYNTHETIC DISPLAY"] * 200)
            self.assertLess((root / "evidence.sqlite3").stat().st_size, len(json.dumps(occurrences).encode()) // 2)

    def test_raw_unclassified_is_accounting_not_a_display_gap_but_real_dependency_holds(self):
        base = {"kind": "managed_literal", "file": "Synthetic.dll", "location": {"method_token": 0x06000001, "il_offset": 1}, "source": "SYNTHETIC RAW", "classification": "uncertain", "reason": "UNCLASSIFIED", "proof": {}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ScanCache(root / "cache.sqlite3") as cache:
                cache.store("primary", "Synthetic.dll", "a", "b", {"occurrences": [base], "pending": []})
                cache.inventory("primary", root / "raw.json", "synthetic")
                raw = json.loads((root / "raw.json").read_text())
                self.assertEqual(raw["segments"], [])
                self.assertEqual(raw["coverage"]["display_scope"]["unclassified_count"], 1)
                possible = {**base, "display_dependency": True}
                cache.store("primary", "Synthetic.dll", "a", "b", {"occurrences": [possible], "pending": [{"reason": "UNRESOLVED_DISPLAY_REFERENCE", "affects_display": True}]})
                cache.inventory("primary", root / "possible.json", "synthetic")
                possible_document = json.loads((root / "possible.json").read_text())
                self.assertFalse(possible_document["coverage"]["declared_complete"])
                self.assertEqual(possible_document["segments"][0]["source"], "SYNTHETIC RAW")
                self.assertEqual(possible_document["segments"][0]["metadata"]["display_scope_evidence"]["kind"], "possible_display_dependency")

    def test_cross_assembly_unbound_sink_holds_consumer_coverage_without_losing_raw_literal(self):
        caller = fixture(codes=[token(0x72, 0x70000001) + token(0x28, 0x0a000003) + b'\x2a'],
                         external_helper_assembly='Synthetic.Helper')
        helper = fixture(codes=[b'\x14\x02' + token(0x6f, 0x0a000001) + b'\x2a'],
                         string_argument=True, assembly_name='Synthetic.Helper')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ScanCache(root / 'cache.sqlite3') as cache:
                for method in ('primary', 'independent'):
                    for name, raw in [('Synthetic.dll', caller), ('Synthetic.Helper.dll', helper)]:
                        path = root / name
                        path.write_bytes(raw)
                        cache.store(method, name, file_digest(path), 'synthetic', scan_assembly(path, name, method=method))
                    output = root / (method + '.json')
                    summary = cache.inventory(method, output, 'synthetic')
                    self.assertEqual(summary['raw_occurrence_count'], 1)
                    self.assertFalse(summary['static_complete'])
                    self.assertFalse(json.loads(output.read_text())['coverage']['declared_complete'])
                    self.assertTrue(any(row['reason'] == 'managed_display_argument_input_unbound'
                                        for row in cache.summary(method)['pending']))

    def test_native_scanner_incompleteness_survives_cache_for_relevant_display_scope(self):
        raw = build_native_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ScanCache(root / 'cache.sqlite3') as cache:
                for method in ('primary', 'independent'):
                    result = scan_native_dependencies(raw, 'SyntheticNative.dll', method=method)
                    self.assertFalse(result['proofs']['static_complete'])
                    self.assertEqual(result['pending'], [])
                    cache.store(method, 'SyntheticNative.dll', 'source', 'scanner', result)
                    self.assertTrue(cache.reusable(method, 'SyntheticNative.dll', 'source', 'scanner'))
                    summary = cache.inventory(method, root / (method + '.json'), 'synthetic')
                    self.assertEqual(summary['display_candidate_count'], 2)
                    self.assertFalse(summary['static_complete'])
                    self.assertFalse(json.loads((root / (method + '.json')).read_text())['coverage']['declared_complete'])
                    unrelated = scan_native_dependencies(raw.replace(b'MessageBoxW', b'NotDisplayW'),
                                                         'SyntheticNative.dll', method=method)
                    cache.store(method, 'SyntheticNative.dll', 'other', 'scanner', unrelated)
                    self.assertEqual(cache.summary(method)['pending'], [])

    def test_shared_resource_identifier_remains_display_candidate_but_requires_projection(self):
        raw = fixture(resources=resource_fixture(), resource_flow=True, shared_resource_key=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'Synthetic.dll'
            path.write_bytes(raw)
            with ScanCache(root / 'cache.sqlite3') as cache:
                for method in ('primary', 'independent'):
                    result = scan_assembly(path, path.name, method=method)
                    cache.store(method, path.name, file_digest(path), 'synthetic', result)
                    output = root / (method + '.json')
                    summary = cache.inventory(method, output, 'synthetic')
                    emitted = json.loads(output.read_text())
                    key = next(row for row in emitted['segments'] if row['source'] == 'a')
                    self.assertTrue(key['metadata']['display_projection_required'])
                    self.assertEqual(key['metadata']['display_scope_evidence']['kind'], 'positive_display_binding')
                    self.assertEqual(summary['raw_occurrence_count'], len(result['occurrences']))
                    self.assertFalse(emitted['coverage']['declared_complete'])




    def test_raw_utf16_units_are_lossless_but_invalid_display_unicode_is_rejected(self):
        row = {"kind": "managed_literal", "file": "Synthetic.dll", "location": {"method_token": 0x06000001, "il_offset": 1}, "source": "SYNTHETIC RAW \ud800", "classification": "uncertain", "reason": "RAW_CODE_UNIT", "proof": {}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ScanCache(root / "cache.sqlite3") as cache:
                cache.store("primary", "Synthetic.dll", "a", "b", {"occurrences": [row]})
                self.assertEqual(list(cache.occurrences("primary"))[0]["source"], row["source"])
                cache.inventory("primary", root / "inventory.json", "synthetic")
                self.assertEqual(json.loads((root / "inventory.json").read_text())["segments"], [])
                with self.assertRaises(UnicodeEncodeError):
                    cache.store("primary", "Synthetic.dll", "a", "b", {"occurrences": [{**row, "classification": "display"}]})
                self.assertEqual(list(cache.occurrences("primary"))[0]["source"], row["source"])


class NativeTextTests(unittest.TestCase):
    def test_resource_occurrences_patch_only_selected_slot_and_roundtrip_unicode(self):
        original = build_resource_fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "SyntheticPlayer.dll"
            path.write_bytes(original)
            primary = scan_native(path, path.name)
            independent = scan_native(path, path.name, method="independent")
            self.assertEqual([(row["source"], row["location"]) for row in primary["occurrences"]], [(row["source"], row["location"]) for row in independent["occurrences"]])
            rows = primary["occurrences"]
            rebuilt = patch_native(original, [{"location": rows[0]["location"], "source": rows[0]["source"], "target": "\ud569\uc131 \ub300\uc0c1"}])
            path.write_bytes(rebuilt)
            actual = scan_native(path, path.name)["occurrences"]
            self.assertEqual([row["source"] for row in actual], ["\ud569\uc131 \ub300\uc0c1", "SYNTHETIC NATIVE RESOURCE"] + [""] * 14)
            self.assertEqual([occurrence_id(row) for row in rows], [occurrence_id(row) for row in actual])
            with self.assertRaisesRegex(ValueError, "SOURCE_MISMATCH"):
                patch_native(original, [{"location": rows[0]["location"], "source": "WRONG SYNTHETIC", "target": "TARGET"}])

    def test_message_table_preserves_native_insert_and_control_tokens(self):
        text = "SYNTHETIC %1!s!%n"
        data = text.encode("utf-16-le") + b"\0\0"
        size = (4 + len(data) + 3) & ~3
        payload = struct.pack("<IIIIHH", 1, 1, 1, 16, size, 1) + data + bytes(size - 4 - len(data))
        raw = build_resource_fixture(resource_type=11, payload=payload)
        location = {"resource_path": [11, 1, 1033], "entry_index": 0}
        with self.assertRaisesRegex(ValueError, "CONTROL_MISMATCH"):
            patch_native(raw, [{"location": location, "source": text, "target": "SYNTHETIC BROKEN"}])
        rebuilt = patch_native(raw, [{"location": location, "source": text, "target": "\ud569\uc131 %1!s!%n"}])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Synthetic.dll"
            path.write_bytes(rebuilt)
            self.assertEqual(scan_native(path, path.name)["occurrences"][0]["source"], "\ud569\uc131 %1!s!%n")

    def test_app_info_preserves_company_bom_and_line_endings(self):
        raw = b"\xef\xbb\xbfSYNTHETIC COMPANY\r\nSYNTHETIC PRODUCT\r\nUNOWNED\r\n"
        rebuilt = patch_native(raw, [{"location": {"line_index": 1}, "source": "SYNTHETIC PRODUCT", "target": "\ud569\uc131 \uc81c\ubaa9"}], app_info=True)
        self.assertEqual(rebuilt, b"\xef\xbb\xbfSYNTHETIC COMPANY\r\n" + "\ud569\uc131 \uc81c\ubaa9".encode() + b"\r\nUNOWNED\r\n")
        with self.assertRaisesRegex(ValueError, "INVALID"):
            patch_native(raw, [{"location": {"line_index": 1}, "source": "SYNTHETIC PRODUCT", "target": "TARGET\nINJECTED"}], app_info=True)


if __name__ == "__main__":
    unittest.main()
