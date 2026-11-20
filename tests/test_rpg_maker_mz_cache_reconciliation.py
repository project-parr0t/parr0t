from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

from game_l10n.rpg_maker_mz.cache_reconciliation import reconcile

_FILE = "fixture.js"
_ROLES = ("primary", "independent")


def digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


def fixture(statements):
    text = "".join(item[0] for item in statements)
    file_record = {"text": text, "sha256": digest(text)}
    caches = (
        {"schema": "parr0t.primary-source-cache.v1", "files": {_FILE: deepcopy(file_record)}},
        {"schema": "parr0t.independent-structural-cache.v2", "files": [{"path": _FILE, **file_record}]},
    )
    witness = {"span": [0, len(text)], "sha256": digest(text)}
    rules, records, rows, raw = [], [], [], []
    offset = 0
    for statement, kind, member, literals in statements:
        if kind:
            start, end = offset, offset + len(statement)
            rules.append({"file": _FILE, "kind": kind, "member": member, "span": [start, end],
                          "sha256": digest(statement), "receiver_witness": witness})
        for literal in literals:
            start = offset + statement.index(literal)
            loc = {"file": _FILE, "path": [], "span": [start, start + len(literal)]}
            key, value = f"fixture-{start}", literal[1:-1]
            records.append({"comparison_key": key, "classification": "control_storage_with_projection",
                            "projection_ids": [], "source_sha256": digest(value)})
            rows.append({"id": key, "source": value, "metadata": {"inventory_comparison_key": key,
                                                                   "source_location": loc}})
            raw.append({"location": loc, "source_sha256": digest(value)})
        offset += len(statement)
    inventories = tuple({"method": role, "implementation_fingerprint": "fixture-" + role,
                         "segments": deepcopy(rows), "coverage": {"scope_correction": {"records": deepcopy(records)}}}
                        for role in _ROLES)
    ledgers = tuple({"occurrences": deepcopy(raw)} for _ in _ROLES)
    bindings = {f"{role}_{kind}": digest(f"{role}-{kind}") for role in _ROLES for kind in ("cache", "raw")}
    return caches, inventories, ledgers, rules, bindings


class CachedReconciliationTests(unittest.TestCase):
    def test_cli_proves_controls_but_preserves_display_and_locked_scopes(self):
        caches, inventories, ledgers, rules, _ = fixture([
            ('const canvas = document.createElement("canvas");\n', "selector", "document.createElement", ['"canvas"']),
            ('const ctx = canvas.getContext("2d");\n', "selector", "canvas.getContext", ['"2d"']),
            ('ctx.fillStyle = "rgb(" + palette[0] + ",0,0)";\n', "canvas_paint", "ctx.fillStyle", ['"rgb("', '",0,0)"']),
            ('media.type = major + "/" + minor;\n', "media_type", "media.type", ['"/"']),
            ('window.addEventListener("resize", listeners.onResize, true);\n', "selector", "window.addEventListener", ['"resize"']),
            ('ctx.fillText("canvas", 0, 0);\n', None, None, ['"canvas"']),
            ('document.createElement("button");\n', "selector", "document.createElement", ['"button"']),
        ])
        for doc in inventories:
            doc["coverage"]["scope_correction"]["records"][-1]["classification"] = "proven_non_display"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bindings = {}
            for role, cache, doc, ledger in zip(_ROLES, caches, inventories, ledgers, strict=True):
                for kind, value in (("cache", cache), ("inventory", doc), ("raw", ledger)):
                    name = f"{role}_{kind}"
                    text = json.dumps(value)
                    (root / f"{name}.json").write_text(text, encoding="utf-8")
                    bindings[name] = {"path": f"{name}.json", "sha256": digest(text)}
            text = json.dumps(rules)
            (root / "rules.json").write_text(text, encoding="utf-8")
            bindings["rules"] = {"path": "rules.json", "sha256": digest(text)}
            request = json.dumps({"schema": "parr0t.mz-cache-reconciliation-request.v1", "bindings": bindings})
            (root / "request.json").write_text(request, encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            argv = [sys.executable, "-B", "-m", "game_l10n.rpg_maker_mz.cache_reconciliation",
                    "--workspace", str(root), "--request", "request.json", "--request-sha256", digest(request)]
            process = subprocess.run([*argv, "--report", "report.json"], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            expected = [row["id"] for row in inventories[0]["segments"][:-2]]
            self.assertEqual([row["comparison_key"] for row in report["proposals"]], expected)
            self.assertEqual(report["unresolved"], {"outside_rules": 1})
            self.assertEqual(report["pending"], len(expected) + 1)
            for proposal in report["proposals"]:
                a, b = proposal["evidence"]
                self.assertNotEqual(a["evidence_sha256"], b["evidence_sha256"])
                self.assertEqual(a["location"], b["location"])
            for binding in bindings.values():
                self.assertEqual(sha256((root / binding["path"]).read_bytes()).hexdigest(), binding["sha256"])
            (root / "primary_cache.json").write_text("{}", encoding="utf-8")
            broken = subprocess.run([*argv, "--report", "broken.json"], cwd=root, env=env, capture_output=True, text=True)
            self.assertNotEqual(broken.returncode, 0)
            self.assertIn("bound input digest changed", broken.stderr)
            self.assertFalse((root / "broken.json").exists())

    def test_cross_method_misses_are_distinct_and_do_not_erase_supported_peer(self):
        args = fixture([(f'document.createElement("{value}");\n', "selector", "document.createElement", [f'"{value}"'])
                        for value in ("div", "button", "canvas", "select", "label", "input")])
        _, (primary, independent), (_, raw), _, _ = args
        primary["segments"][1]["metadata"]["source_location"]["path"] = ["fixture"]
        independent["segments"][1]["metadata"]["source_location"]["path"] = ["fixture"]
        independent["segments"][2]["source"] = "different"
        raw["occurrences"].append(deepcopy(raw["occurrences"][3]))
        independent["segments"][4]["metadata"]["source_location"]["span"][0] += 1
        independent["segments"].pop()
        report = reconcile(*args)
        self.assertEqual([row["comparison_key"] for row in report["proposals"]], [primary["segments"][0]["id"]])
        self.assertEqual(report["unresolved"], {
            "inventory_row_missing": 1, "location_disagrees": 1, "location_unsupported": 1,
            "raw_occurrence_not_unique": 1, "source_disagrees": 1,
        })

    def test_cache_schema_and_independent_witness_fail_closed(self):
        args = fixture([('document.createElement("button");\n', "selector", "document.createElement", ['"button"'])])
        primary, independent = args[0]
        independent["files"][0]["text"] = 'document.createElement("canvas");\n'
        report = reconcile(*args)
        self.assertEqual(report["proposals"], [])
        self.assertEqual(report["unresolved"], {"rule_not_proven": 1})
        independent["schema"] = primary["schema"]
        with self.assertRaisesRegex(ValueError, "unsupported independent cache schema"):
            reconcile(*args)

    def test_audited_span_does_not_prove_a_statement_inside_a_comment_or_call(self):
        args = fixture([
            ('/* ', None, None, []),
            ('document.createElement("button");', "selector", "document.createElement", ['"button"']),
            (' */\n', None, None, []),
            ('ctx.fillStyle = compute("display");\n', "canvas_paint", "ctx.fillStyle", ['"display"']),
        ])
        report = reconcile(*args)
        self.assertEqual(report["proposals"], [])
        self.assertEqual(report["unresolved"], {"rule_not_proven": 2})
