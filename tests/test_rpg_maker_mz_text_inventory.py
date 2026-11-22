from dataclasses import replace
from hashlib import sha256
import json
import unittest

from game_l10n.rpg_maker_mz.text_inventory import (
    DataKind,
    DisplayObservations,
    LocaleCatalogBinding,
    LocaleCatalogProof,
    LocaleInconclusive,
    LocaleUnavailable,
    NativeDataObservation,
    NativeRenderObservation,
    NewLocaleCatalog,
    ReceiverKind,
    ReceiverWitness,
    ScreenReview,
    SourceBinding,
    SpanBinding,
    TextInventoryError,
    materialize,
)


def digest(raw):
    return sha256(raw).hexdigest()


def bound(files, file):
    return SourceBinding(file, digest(files[file]))


def span(text, snippet, start=0):
    index = text.index(snippet, start)
    return SpanBinding(index, index + len(snippet), digest(snippet.encode()))


def command(code, *parameters, indent=0):
    return {"code": code, "indent": indent, "parameters": list(parameters)}


def display(files, observations):
    return materialize(read_source=files.__getitem__, probe_locale=unavailable, load_display=lambda: observations)


def unavailable():
    return LocaleUnavailable(digest(b"verified unavailable"))


def unexpected():
    raise AssertionError("display observations must not load")


def render(files, file, call, receiver, argument, kind):
    text = files[file].decode("utf-8-sig")
    call_span = span(text, call)
    return NativeRenderObservation(
        bound(files, file), ReceiverWitness(kind, span(text, receiver, call_span.start)),
        call_span, span(text, argument, call_span.start),
    )


class TextInventoryTests(unittest.TestCase):
    def document(self, inventory):
        return inventory.document(method="mature-fixture", implementation_fingerprint="fixture-v1")

    def rows(self, inventory):
        return {row["source"]: row for row in self.document(inventory)["segments"]}

    def assert_evidence(self, row, role, kind, basis):
        metadata = row["metadata"]
        evidence = metadata["display_scope_evidence"]
        self.assertEqual(metadata["storage_role"], role)
        self.assertEqual(metadata["display_binding_id"], row["id"])
        self.assertEqual(evidence["kind"], kind)
        self.assertEqual(evidence["binding_basis"], basis)
        self.assertEqual(evidence["source_sha256"], digest(row["source"].encode()))
        self.assertRegex(evidence["evidence_fingerprint"], r"^[0-9a-f]{64}$")

    def locale_fixture(self):
        files = {
            "engine/loader": b"resolveNativeCatalog(activeLocale);",
            "catalog/original": b'{ "objective": ["Find the gate"], "sound": "unlock", "blank": "" }',
            "catalog/destination": b'\xef\xbb\xbf{\r\n  "key.lookup" : {"body":"Old value"}, "sound" : "unlock", "offset" : -0, "blank": ""\r\n}\r\n',
            "data/Map001.json": b'{"displayName":"Do not extract me"}',
        }
        witness = span(files["engine/loader"].decode(), "resolveNativeCatalog(activeLocale)")
        proof = LocaleCatalogProof(bound(files, "engine/loader"), witness, (
            LocaleCatalogBinding(bound(files, "catalog/original"), bound(files, "catalog/destination"),
                                 ((("objective", 0), ("key.lookup", "body")), (("blank",), ("blank",)))),
        ), declared_complete=True)
        return files, proof

    def test_verified_locale_has_exclusive_lazy_route_and_patches_native_values_only(self):
        files, proof = self.locale_fixture()
        events = []

        def probe():
            events.append("probe")
            return proof

        def read(file):
            events.append(file)
            return files[file]

        inventory = materialize(read_source=read, probe_locale=probe, load_display=unexpected)
        self.assertEqual(events, ["probe", "engine/loader", "catalog/original", "catalog/destination"])
        doc = self.document(inventory)
        coverage = doc["coverage"]
        self.assertTrue(coverage["declared_complete"])
        evidence = coverage["text_source"].pop("evidence_fingerprint")
        self.assertRegex(evidence, r"^[0-9a-f]{64}$")
        self.assertEqual(coverage["text_source"], {
            "route": "locale_data", "locale_probe": "applicable", "screen_review": "not_applicable",
            "pending_display_count": 0,
        })
        scope_evidence = coverage["display_scope"].pop("evidence_fingerprint")
        self.assertRegex(scope_evidence, r"^[0-9a-f]{64}$")
        self.assertEqual(coverage["display_scope"], {
            "schema": "parr0t.display-scope.v1", "raw_occurrence_count": 1, "display_candidate_count": 1,
            "control_only_count": 0, "unclassified_count": 0,
        })
        rows = self.rows(inventory)
        self.assertEqual(set(rows), {"Find the gate"})
        row = rows["Find the gate"]
        self.assert_evidence(row, "locale_value", "locale_value", "locale_catalog")
        translated = '\ubb38\uc744 "\ucc3e\uc544\ub77c"\n'
        expected = files["catalog/destination"].replace(b'"Old value"', json.dumps(translated, ensure_ascii=False).encode())
        self.assertEqual(inventory.apply({row["id"]: translated}), {"catalog/destination": expected})
        self.assertEqual(inventory.apply({row["id"]: "Old value"}), {})
        combined = {**files, "catalog/destination": expected}
        catalog = proof.catalogs[0]
        reverse = replace(proof, catalogs=(LocaleCatalogBinding(bound(combined, "catalog/destination"), catalog.source,
                                                                tuple((dst, src) for src, dst in catalog.values)),))
        rewritten = materialize(read_source=combined.__getitem__, probe_locale=lambda: reverse, load_display=unexpected)
        self.assertEqual(set(self.rows(rewritten)), {translated})
        with self.assertRaisesRegex(TextInventoryError, "eligible target"):
            inventory.apply({"key.lookup": "unauthorized"})

    def test_builds_new_locale_without_reading_destination_or_scanning_display(self):
        files, proof = self.locale_fixture()
        source = "catalog/original"
        files[source] = br'{"objective":["Visit \u005cV[2]","Open %1"],"id":"objective"}'
        output = "catalog/new"
        paths = ((("objective", 0), ("objective", 0)), (("objective", 1), ("objective", 1)))
        proof = replace(proof, catalogs=(LocaleCatalogBinding(
            bound(files, source), NewLocaleCatalog(output), paths,
        ),))
        inventory = materialize(read_source=files.__getitem__, probe_locale=lambda: proof, load_display=unexpected)
        rows = self.rows(inventory)
        first = rows[r"Visit \V[2]"]["id"]
        second = rows["Open %1"]["id"]
        with self.assertRaisesRegex(TextInventoryError, "protected MZ"):
            inventory.apply({first: "\uac00\ub77c \\V[3]", second: "Open %1"})
        with self.assertRaisesRegex(TextInventoryError, "every bound value"):
            inventory.apply({first: "\uac00\ub77c \\V[2]"})
        value = "\uac00\ub77c \\V[2]"
        translated = inventory.apply({first: value, second: "Open %1"})
        expected = files[source].replace(br'"Visit \u005cV[2]"', json.dumps(value, ensure_ascii=False).encode())
        self.assertEqual(translated, {output: expected})
        self.assertEqual(inventory.apply({first: r"Visit \V[2]", second: "Open %1"}), {output: files[source]})

    def test_inconclusive_and_stale_or_invalid_locale_proof_never_touch_display(self):
        files, proof = self.locale_fixture()
        with self.assertRaisesRegex(TextInventoryError, "inconclusive"):
            materialize(read_source=lambda _: unexpected(), probe_locale=LocaleInconclusive, load_display=unexpected)
        with self.assertRaisesRegex(TextInventoryError, "requires bound probe evidence"):
            materialize(read_source=lambda _: unexpected(), probe_locale=lambda: LocaleUnavailable("no catalog"),
                        load_display=unexpected)
        catalog = proof.catalogs[0]
        for invalid, error in (
            (replace(proof, loader_evidence=replace(proof.loader_evidence, sha256="0" * 64)), "span"),
            (replace(proof, catalogs=(replace(catalog, target=replace(catalog.target, sha256="0" * 64)),)), "file"),
            (replace(proof, catalogs=(replace(catalog, values=((("objective",), ("key.lookup", "body")),)),)),
             "not a native JSON string value"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(TextInventoryError, error):
                materialize(read_source=files.__getitem__, probe_locale=lambda: invalid, load_display=unexpected)

    def test_native_map_event_slots_exclude_assets_names_branch_labels_comments_and_code(self):
        event = {"id": 1, "x": 2, "y": 4, "name": "Editor label", "note": "note", "pages": [{
            "conditions": {}, "list": [
                command(401, "Orphan line"),
                command(101, "Portrait_1", 0, 0, 2, "Speaker"),
                command(401, "Visit the gate"),
                command(401, ""),
                command(102, ["Enter", "Leave"], 1, 0, 2, 0),
                command(402, 0, "Branch label"),
                command(105, 2, False),
                command(405, "Credits line"),
                command(108, "Editor comment"),
                command(355, '$gameMessage.add("Script without evidence");'),
                command(357, "Plugin Name", "Command Name", "Editor comment", {"text": "not witnessed"}),
                command(0),
            ],
        }]}
        file = "engine/resource"
        root = {"displayName": "Old Farm", "parallaxName": "clouds", "events": [None, event]}
        files = {file: json.dumps(root, indent=2).encode(), "data/Map009.json": b"not examined"}
        order = []

        def probe():
            order.append("probe")
            return unavailable()

        def observe():
            order.append("display")
            return DisplayObservations(data=(NativeDataObservation(bound(files, file), DataKind.MAP),),
                                       screen_review=ScreenReview.USER_SKIPPED, pending_display_count=3,
                                       declared_complete=True, control_only_count=2, unclassified_count=1)

        def read(name):
            order.append(name)
            return files[name]

        inventory = materialize(read_source=read, probe_locale=probe, load_display=observe)
        self.assertEqual(order, ["probe", "display", file])
        rows = self.rows(inventory)
        self.assertEqual(set(rows), {"Old Farm", "Speaker", "Visit the gate", "Enter", "Leave", "Credits line"})
        for row in rows.values():
            self.assert_evidence(row, "display_value", "positive_display_binding", "format_field")
        coverage = self.document(inventory)["coverage"]
        self.assertFalse(coverage["declared_complete"])
        self.assertEqual(coverage["text_source"]["route"], "display_extraction")
        self.assertEqual(coverage["text_source"]["locale_probe"], "unavailable")
        self.assertEqual(coverage["text_source"]["screen_review"], "user_skipped")
        self.assertEqual(coverage["text_source"]["pending_display_count"], 3)
        self.assertEqual(coverage["text_source"]["evidence_fingerprint"], unavailable().evidence_fingerprint)
        self.assertEqual(coverage["display_scope"]["unclassified_count"], 1)
        self.assertEqual(coverage["display_scope"]["control_only_count"], 2)
        self.assertEqual(coverage["display_scope"]["raw_occurrence_count"], 9)
        self.assertEqual(coverage["exclusions"], [])
        replacements = {
            "Old Farm": "\ub18d\uc7a5", "Speaker": "\ud654\uc790",
            "Visit the gate": "\ubb38\uc73c\ub85c", "Enter": "\ub4e4\uc5b4\uac00\uae30",
        }
        translated = inventory.apply({rows[value]["id"]: new for value, new in replacements.items()})
        expected = files[file]
        for value, new in replacements.items():
            expected = expected.replace(json.dumps(value).encode(), json.dumps(new, ensure_ascii=False).encode())
        self.assertEqual(translated, {file: expected})
        rewritten = display(translated, DisplayObservations(data=(NativeDataObservation(bound(translated, file), DataKind.MAP),)))
        new_rows = self.rows(rewritten)
        self.assertEqual({value: new_rows[new]["id"] for value, new in replacements.items()},
                         {value: rows[value]["id"] for value in replacements})

    def test_native_common_and_troop_events_require_the_observed_structure(self):
        files = {
            "x": b'[null,{"id":1,"trigger":0,"switchId":1,"list":[{"code":102,"indent":0,"parameters":[["Common option"],-2,0,2,0]}]}]',
            "y": b'[null,{"id":1,"members":[],"pages":[{"conditions":{},"list":[{"code":102,"indent":0,"parameters":[["Battle option"],-2,0,2,0]}]}]}]',
        }
        common = NativeDataObservation(bound(files, "x"), DataKind.COMMON_EVENTS)
        troop = NativeDataObservation(bound(files, "y"), DataKind.TROOPS)
        inventory = display(files, DisplayObservations(data=(common, troop), declared_complete=True))
        self.assertEqual(set(self.rows(inventory)), {"Common option", "Battle option"})
        self.assertTrue(self.document(inventory)["coverage"]["declared_complete"])
        with self.assertRaisesRegex(TextInventoryError, "native MZ format"):
            display(files, DisplayObservations(data=(replace(troop, kind=DataKind.COMMON_EVENTS),)))
        duplicate = {"x": b'{"displayName":"A","displayName":"B","events":[null]}'}
        with self.assertRaisesRegex(TextInventoryError, "duplicate"):
            display(duplicate, DisplayObservations(data=(NativeDataObservation(bound(duplicate, "x"), DataKind.MAP),)))

    def test_typed_native_script_spans_round_trip_only_first_render_argument(self):
        file = "source/opaque"
        draw = r'''this.statusWindow.drawText('Line\n\uD83D\uDE00', x, 0, width, "center")'''
        add = '$gameMessage.add("Welcome")'
        text = f'// fixture\r\n{draw};\r\n{add};\r\nplugin.drawText("Unproven", 0, 0, 50);\r\n'
        files = {file: b"\xef\xbb\xbf" + text.encode()}
        drawing = render(files, file, draw, "this.statusWindow", r"'Line\n\uD83D\uDE00'", ReceiverKind.WINDOW_BASE)
        message = render(files, file, add, "$gameMessage", '"Welcome"', ReceiverKind.GAME_MESSAGE)
        inventory = display(files, DisplayObservations(scripts=(drawing, message)))
        rows = self.rows(inventory)
        self.assertEqual(set(rows), {"Line\n😀", "Welcome"})
        for row in rows.values():
            self.assert_evidence(row, "display_value", "positive_display_binding", "render_argument")
        literal = r"'\uac00\'\"\\\u2028'"
        value = "\uac00'\"\\\u2028"
        changed = inventory.apply({rows["Line\n😀"]["id"]: value})
        expected = text.replace(r"'Line\n\uD83D\uDE00'", literal)
        self.assertEqual(changed, {file: b"\xef\xbb\xbf" + expected.encode()})
        new_draw = draw.replace(r"'Line\n\uD83D\uDE00'", literal)
        new_witness = render(changed, file, new_draw, "this.statusWindow", literal, ReceiverKind.WINDOW_BASE)
        reopened = display(changed, DisplayObservations(scripts=(new_witness,)))
        self.assertEqual(set(self.rows(reopened)), {value})

    def test_script_rejects_unbound_receivers_spans_nonliteral_operands_and_lexical_decoys(self):
        file = "ticket"
        call = 'window.drawText("First", 0, y, width, "Not text")'
        files = {file: (call + ";").encode()}
        witness = render(files, file, call, "window", '"First"', ReceiverKind.WINDOW_BASE)
        for bad in (
            replace(witness, receiver=replace(witness.receiver, kind=ReceiverKind.GAME_MESSAGE)),
            replace(witness, argument=span(files[file].decode(), '"Not text"')),
            replace(witness, argument=replace(witness.argument, sha256="0" * 64)),
            replace(witness, receiver=replace(witness.receiver, expression=span(files[file].decode(), "drawText"))),
        ):
            with self.subTest(bad=bad), self.assertRaises(TextInventoryError):
                display(files, DisplayObservations(scripts=(bad,)))
        concatenated = call.replace('"First"', '"First" + suffix') + ";"
        for text in (f"/* {call} */", f"Logger.note('{call}');", concatenated,
                     f"const fallback = `{call}`;", f"window?.{call.split('.', 1)[1]};"):
            seen = call if call in text else text.rstrip(";")
            sources = {file: text.encode()}
            attempted = render(sources, file, seen, "window", '"First"', ReceiverKind.WINDOW_BASE)
            with self.subTest(text=text), self.assertRaises(TextInventoryError):
                display(sources, DisplayObservations(scripts=(attempted,)))
        with self.assertRaisesRegex(TextInventoryError, "does not match"):
            display({file: files[file] + b"\n"}, DisplayObservations(scripts=(witness,)))


if __name__ == "__main__":
    unittest.main()
