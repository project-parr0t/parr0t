from __future__ import annotations

import contextlib
import copy
import io
import json
import struct
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from game_l10n.coverage import COMPLETION_SCHEMA
from game_l10n.formats import write_results
from game_l10n.models import TranslationResult
from game_l10n.tooling import cached_tool_state
from game_l10n.unity6000 import assets, discovery, fonts, managed, package, tables, workflow
from game_l10n.unity6000.contracts import AdapterError, digest, get_leaf, leaves, output_path, relative_path, sha, target_id, validate_text
from tests.unity6000_fixtures import clr_fixture, serialized_fixture, synthetic_font

try:
    import UnityPy
    import dnfile
    import fontTools
    DEPS = True
except ImportError:
    DEPS = False


class PureAdapterTests(unittest.TestCase):
    def test_path_aliases_and_traversal(self):
        for value in ("../x", "/x", "C:/x", "x\\y", "x//y", "x/./y", "x/CON.txt", "x/end.", "x/a?b", "x/a|b"):
            with self.subTest(value=value), self.assertRaises(AdapterError):
                relative_path(value)
        self.assertEqual(relative_path("synthetic/sub/file.bin"), "synthetic/sub/file.bin")

    def test_output_guards(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder); root = base / "source"; root.mkdir(); work = base / "work"; work.mkdir()
            for destination in (root / "new", base / "escape", work):
                with self.assertRaises(AdapterError):
                    output_path(work, destination, [root])
            with self.assertRaises(AdapterError):
                output_path(base, root / "new", [root])

    def test_tokens_and_empty_targets(self):
        source = r"<b>{{literal}} {name} %02d %% $value\n\C[2]</b>" + "\x1e"
        validate_text(source, source.replace("{name}", "{name}"))
        validate_text("{a} {b}", "{b} {a}")
        validate_text("{0} {1:N0}", "{1:N0} {0}")
        for target in ("{0} {1}", "{1:N0}"):
            with self.assertRaises(AdapterError):
                validate_text("{0} {1:N0}", target)
        for target in (source.replace(r"\n", ""), source.replace("%%", "%"), source.replace("$value", "$other"), source.replace("<b>", "<i>"), source.replace("\x1e", ""), ""):
            with self.subTest(target=target), self.assertRaises(AdapterError):
                validate_text(source, target)
        with self.assertRaises(AdapterError):
            validate_text("<b><i>x</i></b>", "<i><b>y</b></i>")

    def test_traversal_and_protected_fields(self):
        tree = {"entries": [{"m_Text": "Synthetic"}], "m_MethodName": "Callback"}
        self.assertEqual(list(leaves(tree))[0], (["entries", 0, "m_Text"], "Synthetic"))
        changed = assets.apply_tree(tree, [{"field": ["entries", 0, "m_Text"], "source": "Synthetic", "target": "Example"}], "MonoBehaviour")
        self.assertEqual(get_leaf(changed, ["entries", 0, "m_Text"]), "Example")
        self.assertEqual(tree["entries"][0]["m_Text"], "Synthetic")
        with self.assertRaises(AdapterError):
            assets.apply_tree(tree, [{"field": ["m_MethodName"], "source": "Callback", "target": "Changed"}], "MonoBehaviour")

    def test_table_order_control_codes_and_endings(self):
        source = 'id\t"Synthetic\t{name}"\tkeep\r\nnext\tvalue\x1e\tkeep\nlast\tend'
        changes = [{"field": ["m_Script", "cell", 0, 1], "source": "Synthetic\t{name}", "target": "Example\t{name}"}, {"field": ["m_Script", "cell", 1, 1], "source": "value\x1e", "target": "changed\x1e"}]
        result = tables.rewrite(source, changes)
        self.assertEqual(tables.cell(result, 0, 0), "id")
        self.assertEqual(tables.cell(result, 0, 1), "Example\t{name}")
        self.assertTrue(result.endswith("last\tend"))
        self.assertEqual([r[1] for r in tables.rows(result)], ["\r\n", "\n", ""])
        with self.assertRaises(AdapterError):
            tables.rewrite(source, [*changes, changes[0]])

    def test_table_rejects_multiline_and_source_mismatch(self):
        for source, changes in [('a\t"unterminated\nnext"', []), ("a\tb", [{"field": ["m_Script", "cell", 0, 1], "source": "wrong", "target": "new"}]), ("a\tb", [{"field": ["m_Script", "cell", 0, 1], "source": "b", "target": "new\nline"}])]:
            with self.assertRaises(AdapterError):
                tables.rewrite(source, changes)

    def test_ui_text_and_image_census(self):
        def item(pid, cls, go, sf="one", file_id=0):
            return {"path_id": pid, "class": cls, "serialized_file": sf, "tree": {"m_GameObject": {"m_FileID": file_id, "m_PathID": go}}}
        report = discovery.census([item(1, "Button", 10), item(2, "TextMeshProUGUI", 10), item(3, "Image", 10), item(4, "Button", 20), item(5, "Image", 20)])
        self.assertEqual(report["controls"][0]["text_components"], [2])
        self.assertEqual(report["controls"][1]["label_state"], "no_serialized_text_component")
        self.assertFalse(report["image_only_controls_are_text_covered"])
        report = discovery.census([item(1, "Button", 10), item(2, "TextMeshProUGUI", 10, "two"), item(3, "Image", 10, file_id=1)])
        self.assertEqual(report["controls"][0]["text_components"], [])
        self.assertEqual(report["controls"][0]["image_components"], [])
        self.assertEqual(report["unresolved_relationships"], 1)

    def test_ui_external_and_missing_parent_unresolved(self):
        objects = [{"path_id": 1, "class": "Transform", "tree": {"m_GameObject": fonts.ptr(10), "m_Father": {"m_FileID": 1, "m_PathID": 2}}}, {"path_id": 3, "class": "Transform", "tree": {"m_GameObject": fonts.ptr(20), "m_Father": fonts.ptr(99)}}]
        self.assertEqual(discovery.census(objects)["unresolved_relationships"], 2)

    def test_entry_gate_blocks_before_source_context(self):
        from game_l10n.errors import ConfigurationError
        from game_l10n.unity6000.cli import main
        with patch("game_l10n.unity6000.cli.require_control_plane_ready"), patch("game_l10n.unity6000.cli.require_control_plane_entry", side_effect=ConfigurationError("E_CONTROL_PLANE_ENTRY_REQUIRED: synthetic")), patch("game_l10n.unity6000.cli.context") as source_context, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["discover", "--workspace", "synthetic", "--output", "discovery.json"]), 2)
        source_context.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["issue_code"], "E_CONTROL_PLANE_ENTRY_REQUIRED")

    def test_delta_boundary_cases(self):
        for before, after in ((b"", b"new"), (b"old", b""), (b"prefix-old-tail", b"prefix-new-tail"), (b"abc", b"abcd")):
            p, s, old, new = package.delta(before, after)
            rebuilt = before[:p] + new + (before[-s:] if s else b"")
            restored = rebuilt[:p] + old + (rebuilt[-s:] if s else b"")
            self.assertEqual((rebuilt, restored), (after, before))


@unittest.skipUnless(DEPS, "Install profiles/unity-6000.0-requirements.txt")
class FormatAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name); self.root = self.base / "source"; self.root.mkdir()
        self.work = self.base / "work"; self.work.mkdir()
        self.data = self.root / "Synthetic_Data"; self.data.mkdir(); (self.data / "Managed").mkdir()
        (self.data / "Managed" / "UnityEngine.CoreModule.dll").write_bytes(b"synthetic-layout-marker")
        (self.data / "globalgamemanagers").write_bytes(serialized_fixture([(1, 49, {"m_Name": "Synthetic", "m_Script": "Version marker"})]))
        self.relative = "Synthetic_Data/synthetic.assets"
        self.source = "Synthetic {name}"
        self.blob = serialized_fixture([(10, 49, {"m_Name": "Synthetic", "m_Script": self.source}), (11, 49, {"m_Name": "Unowned", "m_Script": "Unchanged"})], name=self.relative)
        (self.root / self.relative).write_bytes(self.blob)

    def inventory(self):
        return discovery.discover(self.root, assets.Schemas())

    def inputs(self):
        inventory = self.inventory()
        selection = {"schema": "parr0t.unity6000.selection.v1", "inventory_fingerprint": inventory["fingerprint"], "entries": [{"id": r["id"], "classification": "confirmed_text" if r["file"] == self.relative and r["path_id"] == 10 else "nontext"} for r in inventory["records"]]}
        chosen = workflow.selected_records(inventory, selection)
        results = self.work / "results.json"
        write_results(results, [TranslationResult(segment_id=r["id"], source=r["source"], text="Example {name}", status="reviewed") for r in chosen])
        return inventory, selection, results

    def test_discover_and_serialized_roundtrip(self):
        inventory, selection, results = self.inputs()
        outputs = workflow.payload(self.root, inventory, selection, results, assets.Schemas())
        self.assertEqual((self.root / self.relative).read_bytes(), self.blob)
        parsed = {o.path_id: o for o in assets.load(outputs[self.relative][1], self.relative).objects}
        original = {o.path_id: o for o in assets.load(self.blob, self.relative).objects}
        self.assertEqual(parsed[10].read_typetree()["m_Script"], "Example {name}")
        self.assertEqual(parsed[11].get_raw_data(), original[11].get_raw_data())

    def test_stale_source_and_inventory_rejected(self):
        inventory, selection, results = self.inputs()
        broken = copy.deepcopy(inventory); broken["fingerprint"] = "0" * 64
        with self.assertRaises(AdapterError):
            discovery.validate_inventory(broken, self.root, assets.Schemas())
        (self.root / self.relative).write_bytes(self.blob + b"x")
        with self.assertRaisesRegex(AdapterError, "SOURCE_STALE"):
            workflow.payload(self.root, inventory, selection, results, assets.Schemas())

    def test_backend_and_version_rejected(self):
        (self.root / "GameAssembly.dll").write_bytes(b"synthetic")
        with self.assertRaisesRegex(AdapterError, "BACKEND_UNSUPPORTED"):
            discovery.installation(self.root)
        (self.root / "GameAssembly.dll").unlink()
        (self.data / "globalgamemanagers").write_bytes(serialized_fixture([(1, 49, {"m_Name": "Synthetic", "m_Script": "x"})], version="6000.1.1f1"))
        with self.assertRaisesRegex(AdapterError, "ENGINE_VERSION_UNSUPPORTED"):
            discovery.installation(self.root)

    def test_candidate_selection_complete_and_unresolved(self):
        inventory, selection, _ = self.inputs()
        partial = copy.deepcopy(selection); partial["entries"].pop()
        with self.assertRaises(AdapterError):
            workflow.selected_records(inventory, partial)
        partial = copy.deepcopy(selection); partial["entries"][0]["classification"] = "unresolved"
        with self.assertRaisesRegex(AdapterError, "CANDIDATES_UNRESOLVED"):
            workflow.selected_records(inventory, partial)

    def test_source_immutability_writer_guard(self):
        inventory, selection, results = self.inputs()
        with self.assertRaisesRegex(AdapterError, "OUTPUT_SOURCE_OVERLAP"):
            workflow.prepare(self.root, inventory, selection, results, assets.Schemas(), [], self.root / "new", workspace=self.base)
        self.assertFalse((self.root / "new").exists())

    def test_serialized_table_cell_roundtrip(self):
        source = 'id\tSynthetic {name}\r\nkeep\tunchanged\n'
        blob = serialized_fixture([(10, 49, {"m_Name": "Synthetic", "m_Script": source})], name=self.relative)
        result = assets.patch(blob, self.relative, [{"serialized_file": Path(self.relative).name, "path_id": 10, "field": ["m_Script", "cell", 0, 1], "source": "Synthetic {name}", "target": "Example {name}"}], assets.Schemas())
        obj = next(iter(assets.load(result, self.relative).objects))
        self.assertEqual(obj.read_typetree()["m_Script"], source.replace("Synthetic", "Example"))

    def test_dependency_resolution_is_bound_and_unambiguous(self):
        env = assets.load(self.blob, self.relative, self.root)
        dependency = env.find_file("globalgamemanagers")
        self.assertTrue(dependency.is_dependency)
        self.assertEqual(len(env.objects), 2)
        with self.assertRaises(AdapterError):
            env.find_file("../escape")
        with self.assertRaises(AdapterError):
            assets.load(self.blob, self.relative).find_file("globalgamemanagers")

    def test_malformed_specs_are_categorical(self):
        for function, args in ((fonts.validate_overlay, (None, {}, {})), (assets.apply_tree, ({}, [None], "MonoBehaviour")), (workflow.selected_records, ({"records": []}, {"schema": "parr0t.unity6000.selection.v1", "entries": [None]}))):
            with self.assertRaises(AdapterError):
                function(*args)

    def test_schema_cache_isolation_and_stale_binding(self):
        assembly = self.data / "Managed" / "Synthetic.dll"; assembly.write_bytes(b"synthetic-assembly")
        node = [{k: v for k, v in item.items() if k != "m_Children"} for item in next(iter(assets.load(self.blob, self.relative).objects)).serialized_type.node.to_dict_list()]
        cache = {"schema": assets.CACHE_SCHEMA, "engine": "unity", "series": "6000.0", "generator": "synthetic-v1", "assemblies": {"Synthetic.dll": {"path": "Synthetic_Data/Managed/Synthetic.dll", "sha256": sha(assembly.read_bytes())}}, "nodes": {"Synthetic.dll|Synthetic.Component": node}}
        cache["unity_version"] = "6000.0.1f1"
        path = self.work / "schemas.json"; path.write_text(json.dumps(cache))
        first = assets.Schemas(path, self.root)
        self.assertEqual(len(first.nodes), 1)
        cache["series"] = "6000.1"; path.write_text(json.dumps(cache))
        with self.assertRaisesRegex(AdapterError, "CACHE_IDENTITY"):
            assets.Schemas(path, self.root)
        cache["series"] = "6000.0"; path.write_text(json.dumps(cache)); assembly.write_bytes(b"stale")
        with self.assertRaisesRegex(AdapterError, "ASSEMBLY_STALE"):
            assets.Schemas(path, self.root)
        self.assertEqual(len(assets.Schemas().nodes), 0)

    def test_managed_targeted_append_and_shared_original(self):
        data, source = clr_fixture()
        changes = [{"method_token": 0x06000001, "il_offset": 1, "source": source, "target": "Example {name}"}]
        after = managed.patch(data, changes)
        self.assertGreater(len(after), len(data))
        records = managed.discover(after)
        self.assertEqual([r["source"] for r in records], ["Example {name}", source])
        self.assertEqual(data, clr_fixture()[0])
        for changed in ({**changes[0], "il_offset": 2}, {**changes[0], "method_token": 1}, {**changes[0], "source": "stale"}):
            with self.assertRaises(AdapterError):
                managed.patch(data, [changed])

    def test_managed_signed_overlay_bss_and_malformed_rejected(self):
        data, source = clr_fixture()
        changes = [{"method_token": 0x06000001, "il_offset": 1, "source": source, "target": "Example {name}"}]
        signed = bytearray(data); struct.pack_into("<I", signed, 0x200 + 16, 9)
        bss = bytearray(data); struct.pack_into("<I", bss, 0x178 + 8, 0x700)
        auth = bytearray(data); struct.pack_into("<II", auth, 0x98 + 96 + 4 * 8, 0x800, 16)
        for blob in (bytes(signed), bytes(bss), bytes(auth), data + b"overlay", b"malformed"):
            with self.subTest(size=len(blob)), self.assertRaises(AdapterError):
                managed.patch(blob, changes)

    def test_user_string_binary_lengths_and_utf16_flag(self):
        self.assertEqual(managed.compressed_uint(127), b"\x7f")
        self.assertEqual(managed.compressed_uint(128), b"\x80\x80")
        self.assertEqual(managed.user_string("A"), b"\x03A\0\0")
        self.assertEqual(managed.user_string("\U00010000")[-1], 1)

    def test_prepared_delta_pack_roundtrip_and_tamper(self):
        inventory, selection, results = self.inputs()
        prepared = self.work / "prepared"
        workflow.prepare(self.root, inventory, selection, results, assets.Schemas(), [], prepared, workspace=self.work)
        receipt, outputs = workflow.verify_prepared(self.root, prepared, results, assets.Schemas())
        archive = self.work / "patch.zip"
        package.pack(outputs, archive, result_sha256=sha(results.read_bytes()), inventory_fingerprint=inventory["fingerprint"], workspace=self.work, source_root=self.root)
        self.assertEqual(package.verify(archive, self.root)["restore"], "verified")
        second = self.work / "second.zip"
        package.pack(outputs, second, result_sha256=sha(results.read_bytes()), inventory_fingerprint=inventory["fingerprint"], workspace=self.work, source_root=self.root)
        self.assertEqual(archive.read_bytes(), second.read_bytes())
        (prepared / "payload/0.bin").write_bytes(b"tampered")
        with self.assertRaisesRegex(AdapterError, "PAYLOAD_CHANGED"):
            workflow.verify_prepared(self.root, prepared, results, assets.Schemas())
        results.write_text('{"results": []}')
        with self.assertRaisesRegex(AdapterError, "BINDING_STALE"):
            workflow.verify_prepared(self.root, prepared, results, assets.Schemas())

    def test_archive_traversal_duplicate_unowned_and_corrupt_delta(self):
        inventory, selection, results = self.inputs()
        outputs = workflow.payload(self.root, inventory, selection, results, assets.Schemas())
        good = self.work / "good.zip"
        package.pack(outputs, good, result_sha256=sha(results.read_bytes()), inventory_fingerprint=inventory["fingerprint"], workspace=self.work, source_root=self.root)
        for index, (extra, content) in enumerate((("../escape", b"x"), ("manifest.json", b"{}"), ("unowned", b"x"), ("delta/0.after", b"corrupt"))):
            bad = self.work / f"bad{index}.zip"
            bad.write_bytes(good.read_bytes())
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(bad, "a") as archive:
                    archive.writestr(extra, content)
            with self.assertRaises(AdapterError):
                package.verify(bad, self.root)
        bad = self.work / "hash-corrupt.zip"
        with zipfile.ZipFile(good) as source, zipfile.ZipFile(bad, "x") as target:
            for name in source.namelist():
                target.writestr(name, b"corrupt" if name == "delta/0.after" else source.read(name))
        with self.assertRaisesRegex(AdapterError, "DELTA_ROUNDTRIP_INVALID"):
            package.verify(bad, self.root)

    def test_cli_apply_pack_core_binding(self):
        from game_l10n.unity6000.cli import main
        inventory, selection, results = self.inputs()
        (self.work / "inventory.json").write_text(json.dumps(inventory)); (self.work / "selection.json").write_text(json.dumps(selection))
        target = {"engine": "unity", "version": "6000.0", "branch": "sdk-unity-6000.0"}
        def run(*args):
            with patch("game_l10n.unity6000.cli.load_config", return_value={"game_roots": [str(self.root)]}), patch("game_l10n.unity6000.cli.require_sdk_activation", return_value={"target": target}), patch("game_l10n.unity6000.cli.require_control_plane_ready"), patch("game_l10n.unity6000.cli.require_control_plane_entry"), contextlib.redirect_stdout(io.StringIO()):
                return main([*args, "--workspace", str(self.work)])
        self.assertEqual(run("apply", "--inventory", "inventory.json", "--selection", "selection.json", "--results", "results.json", "--output", "prepared"), 0)
        self.assertEqual(run("pack", "--prepared", "prepared", "--results", "results.json", "--output", "unbound.zip"), 2)
        self.assertEqual(cached_tool_state(self.work, "pack")["status"], "missing")
        completion = self.work / ".game-l10n/completion-attestation.json"; completion.parent.mkdir(parents=True, exist_ok=True)
        completion.write_text(json.dumps({"schema": COMPLETION_SCHEMA, "target_completion": True, "static_completion": True, "result_artifact": {"path": str(results.resolve()), "sha256": sha(results.read_bytes()), "size": results.stat().st_size}}))
        self.assertEqual(run("pack", "--prepared", "prepared", "--results", "results.json", "--output", "bound.zip"), 0)
        self.assertEqual(cached_tool_state(self.work, "pack")["status"], "valid")
        self.assertEqual((self.root / self.relative).read_bytes(), self.blob)

    def font_objects(self):
        font = synthetic_font()
        trees = {
            1: {"m_Name": "Original", "m_FontData": list(font), "m_Texture": fonts.ptr(0)},
            2: {"m_Name": "OriginalTMP", "m_Script": fonts.ptr(91), "m_SourceFontFile": fonts.ptr(1), "m_Material": fonts.ptr(3), "m_AtlasTextures": [fonts.ptr(4)], "m_AtlasPopulationMode": 1, "m_GlyphTable": [], "m_CharacterTable": []},
            3: {"m_Name": "OriginalMaterial", "m_Shader": fonts.ptr(90), "m_SavedProperties": {"m_TexEnvs": [("_MainTex", {"m_Texture": fonts.ptr(4)})]}},
            4: {"m_Name": "OriginalAtlas", "m_StreamData": {"path": "", "offset": 0, "size": 0}},
            5: {"m_Name": "Settings", "m_Script": fonts.ptr(91), "m_fallbackFontAssets": [fonts.ptr(2)]},
            90: {"m_Name": "SyntheticShader"}, 91: {"m_Name": "SyntheticScript"},
        }
        classes = {1: "Font", 2: "MonoBehaviour", 3: "Material", 4: "Texture2D", 5: "MonoBehaviour", 90: "Shader", 91: "MonoScript"}
        spec = {"schema": "parr0t.unity6000.tmp-overlay.v1", "file": self.relative, "serialized_file": self.relative, "ids": {"native": 101, "tmp": 102, "material": 103, "atlas": 104}, "templates": {"native": 1, "tmp": 2, "material": 3, "atlas": 4}, "settings_id": 5, "font_sha256": sha(font), "required_codepoints": [65], "trees": {role: copy.deepcopy(trees[pid]) for role, pid in {"native": 1, "tmp": 2, "material": 3, "atlas": 4}.items()}}
        spec["serialized_file"] = Path(self.relative).name
        spec["trees"]["tmp"].update(m_SourceFontFile=fonts.ptr(101), m_Material=fonts.ptr(103), m_AtlasTextures=[fonts.ptr(104)])
        spec["trees"]["material"]["m_SavedProperties"]["m_TexEnvs"][0][1]["m_Texture"] = fonts.ptr(104)
        return trees, classes, spec

    def test_font_additive_refs_and_original_preservation(self):
        trees, classes, spec = self.font_objects()
        from UnityPy.enums import ClassIDType
        blob = serialized_fixture([(pid, int(ClassIDType[classes[pid]]), tree) for pid, tree in trees.items()], name=self.relative)
        # JSON transports map pairs as lists; the adapter restores the template shape.
        result = assets.patch(blob, self.relative, [], assets.Schemas(), json.loads(json.dumps(spec)))
        original = {o.path_id: o for o in assets.load(blob, self.relative).objects}
        rebuilt = {o.path_id: o for o in assets.load(result, self.relative).objects}
        self.assertEqual(set(rebuilt), set(original) | {101, 102, 103, 104})
        for pid in original:
            if pid != 5:
                self.assertEqual(original[pid].get_raw_data(), rebuilt[pid].get_raw_data())
        self.assertEqual(rebuilt[5].read_typetree()["m_fallbackFontAssets"], [fonts.ptr(2), fonts.ptr(102)])

    def test_font_bad_templates_refs_and_coverage(self):
        trees, classes, spec = self.font_objects()
        fonts.validate_overlay(spec, trees, classes)
        cases = []
        for role, field, value in (("tmp", "m_Script", fonts.ptr(90)), ("material", "m_Shader", fonts.ptr(1)), ("tmp", "m_Material", fonts.ptr(999)), ("atlas", "m_StreamData", {"path": "synthetic/external", "size": 1, "offset": 0})):
            changed = copy.deepcopy(spec); changed["trees"][role][field] = value; cases.append(changed)
        changed = copy.deepcopy(spec); changed["ids"]["native"] = 1; cases.append(changed)
        changed = copy.deepcopy(spec); changed["required_codepoints"] = [66]; cases.append(changed)
        changed = copy.deepcopy(spec); changed["font_sha256"] = "0" * 64; cases.append(changed)
        for changed in cases:
            with self.assertRaises(AdapterError):
                fonts.validate_overlay(changed, trees, classes)


if __name__ == "__main__":
    unittest.main()
