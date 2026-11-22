import unittest

from game_l10n.rpg_maker_mz.cache_reconciliation import _NAME_RE, _code_tokens
from game_l10n.rpg_maker_mz.consumer_usage import ConsumerUsage


def classify(source, expected, *, reasons=False, key_types=False, diagnostic=None):
    tokens = [("name" if _NAME_RE.fullmatch(value) else "string" if value.startswith(('"', "'")) else "punct",
               value, start, end) for value, start, end in _code_tokens(source, 0, len(source))]
    usage = ConsumerUsage(tokens)
    indices = {value[1:-1]: i for i, (kind, value, *_span) in enumerate(tokens) if kind == "string"}
    if diagnostic is not None:
        get = getattr(usage, diagnostic)
        return {value: get(indices[value]) for value in expected}
    if key_types:
        return {value: (usage.previously_unsupported_key_type(indices[value]),
                        usage.classify_with_reason(indices[value], cohort)[2])
                for value, cohort in expected.items()}
    method = usage.classify_with_reason if reasons else usage.classify
    return {value: method(indices[value], cohort) for value, cohort in expected.items()}


class ConsumerUsageTests(unittest.TestCase):
    def test_identifies_object_values_and_actual_member_reads_instead_of_using_equal_text(self):
        source = '''const config = {"fixture_key": "lookup", title: "caption", opaque: "unknown"};
                    table[config.fixture_key]; view.drawText(config["title"]);'''
        cases = dict.fromkeys(("fixture_key", "lookup", "caption", "unknown"), "colon_neighbor")
        self.assertEqual(classify(source, cases), {
            "fixture_key": ("object_key", "unbound"),
            "lookup": ("object_value_other_field_shape", "index_read"),
            "caption": ("object_value_display_field_shape", "display_call_shape"),
            "unknown": ("object_value_other_field_shape", "no_direct_read"),
        })

    def test_distinguishes_conditionals_case_labels_and_non_assignment_operators(self):
        source = '''const value = condition ? "first" : "second";
                    switch (value) { case "fixture-case": break; }
                    if (value === "same") { view.message = "maybe"; }'''
        cases = {**dict.fromkeys(("first", "second", "fixture-case"), "colon_neighbor"),
                 **dict.fromkeys(("same", "maybe"), "equals_prefix")}
        self.assertEqual(classify(source, cases), {
            "first": ("conditional_arm", "unbound"), "second": ("conditional_arm", "unbound"),
            "fixture-case": ("switch_case_label", "unbound"), "same": ("compound_or_comparison", "unbound"),
            "maybe": ("member_assignment_other_field_shape", "unbound"),
        })

    def test_preserves_ambiguity_after_overwrite_and_ignores_shadowed_reads(self):
        source = '''let key = "original"; key = "replacement"; table[key];
                    const config = {}; config["title"] = "label"; view.drawText(config.title);
                    const thing = "outer"; { const thing = "inner"; view.setText(thing); }'''
        cases = dict.fromkeys(("original", "replacement", "label", "outer", "inner"), "equals_prefix")
        self.assertEqual(classify(source, cases), {
            "original": ("local_initializer", "ambiguous_write"),
            "replacement": ("identifier_assignment", "ambiguous_write"),
            "label": ("indexed_assignment_display_field_shape", "indexed_lookup_display_call_shape"),
            "outer": ("local_initializer", "no_direct_read"),
            "inner": ("local_initializer", "display_call_shape"),
        })

    def test_follows_inline_and_named_objects_to_the_correct_parameter_read(self):
        source = '''function accept(unused, item) { view.drawText(item.title); table[item.key]; }
                    const config = {title: "label", unused: "not used"};
                    accept(0, config);
                    accept(0, {key: "lookup"});
                    receiver.accept(0, {title: "other receiver"});
                    accept(...args, {title: "unbound slot"});'''
        cases = dict.fromkeys(("label", "not used", "lookup", "other receiver", "unbound slot"), "colon_neighbor")
        self.assertEqual(classify(source, cases), {
            "label": ("object_value_display_field_shape", "via_function_display_call_shape"),
            "not used": ("object_value_other_field_shape", "no_direct_read"),
            "lookup": ("object_value_other_field_shape", "via_function_index_read"),
            "other receiver": ("object_value_display_field_shape", "unbound"),
            "unbound slot": ("object_value_display_field_shape", "unbound"),
        })

    def test_rejects_shadowed_callee_and_modified_parameter(self):
        source = '''function render(arg) { arg.title += suffix; view.drawText(arg.title); }
                    render({title: "modified"});
                    { const render = other; render({title: "shadowed"}); }'''
        self.assertEqual(classify(source, dict.fromkeys(("modified", "shadowed"), "colon_neighbor")), {
            "modified": ("object_value_display_field_shape", "unbound"),
            "shadowed": ("object_value_display_field_shape", "unbound"),
        })

    def test_indexes_require_a_later_read_of_the_same_receiver_and_stable_key(self):
        source = '''function render(cache, row) {
                      cache.labels[row] = "rendered"; view.drawText(cache.labels[row]);
                    }
                    const table = {}; const other = {}; const key = choose();
                    table[12] = "call value"; handle(table["12"]);
                    table[key] = "unknown collision"; table[other.key] = "dynamic";
                    other[1] = "no later read"; handle(table[1]);'''
        cases = dict.fromkeys(("rendered", "call value", "unknown collision", "dynamic", "no later read"), "equals_prefix")
        self.assertEqual(classify(source, cases), {
            "rendered": ("indexed_assignment", "indexed_lookup_display_call_shape"),
            "call value": ("indexed_assignment", "ambiguous_write"),
            "unknown collision": ("indexed_assignment", "ambiguous_write"),
            "dynamic": ("indexed_assignment", "unbound"),
            "no later read": ("indexed_assignment", "unbound"),
        })
        self.assertEqual(classify('const table = {}; table[12] = "call"; handle(table["12"]);',
                                  {"call": "equals_prefix"}),
                         {"call": ("indexed_assignment", "indexed_lookup_other_call_read")})
        self.assertEqual(classify('const table = {}; handle(table["key"]); table["key"] = "later";',
                                  {"later": "equals_prefix"}),
                         {"later": ("indexed_assignment_other_field_shape", "no_direct_read")})

    def test_argument_rejections_distinguish_callee_and_parameter_read(self):
        source = '''function use(item) { view.drawText(item.title); }
                    use({title: "known", note: "unread"});
                    view.use({title: "member"});
                    missing({title: "outside"});
                    const separate = {title: "not passed"};'''
        observed = classify(source, dict.fromkeys(
            ("known", "unread", "member", "outside", "not passed"), "colon_neighbor"), reasons=True)
        self.assertEqual({value: row[2] for value, row in observed.items()}, {
            "known": "matched", "unread": "no_parameter_property_read",
            "member": "member_or_optional_call", "outside": "no_unique_supported_local_callee",
            "not passed": "no_direct_argument",
        })

    def test_index_rejections_distinguish_binding_key_and_read_order(self):
        source = '''external[0] = "receiver";
                    const box = {}; let key = choose(); box[key] = "unstable";
                    const table = {};
                    read(table[0]); table[0] = "before";
                    table[1] = "absent";
                    table[2] = "after"; read(table[2]);'''
        observed = classify(source, dict.fromkeys(
            ("receiver", "unstable", "before", "absent", "after"), "equals_prefix"), reasons=True)
        self.assertEqual({value: row[2] for value, row in observed.items()}, {
            "receiver": "receiver_not_unique_local", "unstable": "key_not_stable_local",
            "before": "no_later_read", "absent": "no_same_address_read", "after": "matched",
        })

    def test_folds_pure_literals_but_keeps_dynamic_index_keys_categorical(self):
        source = '''const table = {};
                    table["prefix" + (2 + 3)] = "folded"; read(table.prefix5);
                    const members = {}; members[config.slot] = "member";
                    const calls = {}; calls[choose()] = "call";
                    const nested = {}; nested[slots[0]] = "nested";
                    const operators = {}; operators[slot + 1] = "operator";'''
        cases = dict.fromkeys(("folded", "member", "call", "nested", "operator"), "equals_prefix")
        self.assertEqual(classify(source, cases, key_types=True), {
            "folded": ("constant_expression", "matched"),
            "member": ("member_access", "unsupported_key_expression"),
            "call": ("call_expression", "unsupported_key_expression"),
            "nested": ("nested_subscript", "unsupported_key_expression"),
            "operator": ("operator_expression", "unsupported_key_expression"),
        })

    def test_folded_key_does_not_bypass_competing_writes_or_rounding(self):
        source = '''const table = {};
                    table[2 + 3] = "folded"; table[dynamic.id] = "dynamic"; read(table[5]);
                    const other = {}; other[999999999999999 + 999999999999999 + 999999999999999
                    + 999999999999999 + 999999999999999 + 999999999999999 + 999999999999999
                    + 999999999999999 + 999999999999999 + 999999999999999] = "too wide";'''
        cases = dict.fromkeys(("folded", "dynamic", "too wide"), "equals_prefix")
        self.assertEqual(classify(source, cases, key_types=True), {
            "folded": ("constant_expression", "competing_index_writes"),
            "dynamic": ("member_access", "unsupported_key_expression"),
            "too wide": ("operator_expression", "unsupported_key_expression"),
        })

    def test_records_operator_and_operand_shapes_without_copying_operands(self):
        source = '''const table = {}; const index = 1;
                    table[index * 10 + source.offset] = "precedence";
                    table[index ?? generate()] = "coalesce";
                    table[enabled ? quiet ? option.a : 10 : "key"] = "conditional";
                    table[index - -2] = "unary right";
                    table[+index] = "unary root";'''
        expected = {
            "precedence": ("+", "expression", "member_access"),
            "coalesce": ("??", "local_name", "call"),
            "conditional": ("?:", "other_name", "expression", "string_literal"),
            "unary right": ("-", "local_name", "expression"),
            "unary root": ("prefix_+", "none", "local_name"),
        }
        self.assertEqual(classify(source, expected, diagnostic="previously_unsupported_key_operator"), expected)

    def test_traces_exact_nested_property_slots_and_distinguishes_unbound_destinations(self):
        source = '''function accept(arg) { table[arg[1].slot]; }
                    const fixture = { group: [{slot: "other index"}, {slot: "active"}] };
                    view.drawText(fixture["group"][1].slot);
                    accept([{}, {slot: "argument"}]);
                    function make() { return [{slot: "returned"}]; }
                    destination.value = { slot: "assigned" };
                    const named = [{}, {slot: "forwarded"}]; accept(named);
                    let changed = {group: {slot: "changed"}};
                    changed[groupKey] ??= replacement; view.drawText(changed.group.slot);
                    const opaque = {slot: "obscured", [dynamic]: value}; read(opaque.slot);'''
        expected = {
            "other index": ("array_element", "local_initializer", "no_later_path_read", None),
            "active": ("array_element", "local_initializer", "display_call_shape", None),
            "argument": ("array_element", "bound_call_argument", "index_read", None),
            "returned": ("array_element", "return_value", "unbound", None),
            "assigned": ("direct_object", "member_assignment", "unbound", None),
            "forwarded": ("array_element", "local_initializer", "via_function_index_read", None),
            "changed": ("nested_property", "local_initializer", "property_path_reassigned", None),
            "obscured": ("direct_object", "ambiguous_property", "unbound", "later_dynamic_key"),
        }
        self.assertEqual(classify(source, expected, diagnostic="object_value_usage"), expected)

    def test_compares_later_literal_members_to_target_key_in_source_order(self):
        source = r'''const readable = {[dynamic]: before, slot: "live", ["other" + 1]: value,
                                        12: other, fallback, get rest() {}, async *load() {}};
                     view.drawText(readable.slot);
                     const duplicate = {slot: "earlier", slot: "latest"}; read(duplicate.slot);
                     const computed = {slot: "computed", ["s\u{006c}ot"]: other};
                     const quoted = {slot: "quoted", "s\x6cot": other};
                     const shorthand = {slot: "shorthand", slot}; read(shorthand.slot);
                     const getter = {slot: "getter", get ["slot"]() {}};
                     const later = {slot: "dynamic", [choose()]: other};
                     const ambiguous = {slot: "ambiguous", "\163lot": value};
                     const inherited = {__proto__: "prototype"};
                     const nested = {group: {slot: "ancestor"}, ['gr' + 'oup']: replacement};
                     view.drawText(nested.group.slot);'''
        direct_read = ("direct_object", "local_initializer")
        blocked = ("direct_object", "ambiguous_property", "unbound")
        expected = {
            "live": (*direct_read, "display_call_shape", "no_later_key_conflict"),
            "earlier": (*blocked, "later_same_key"),
            "latest": (*direct_read, "other_call_read", "no_later_key_conflict"),
            "computed": (*blocked, "later_same_key"),
            "quoted": (*blocked, "later_same_key"),
            "shorthand": (*blocked, "later_same_key"),
            "getter": (*blocked, "later_same_key"),
            "dynamic": (*blocked, "later_dynamic_key"),
            "ambiguous": (*blocked, "later_unresolved_member"),
            "prototype": (*blocked, "prototype_setter_unresolved"),
            "ancestor": ("nested_property", "ambiguous_property", "unbound", "later_same_key"),
        }
        self.assertEqual(classify(source, expected, diagnostic="object_value_usage"), expected)

    def test_follows_projected_aliases_and_positional_wrapper_chains(self):
        source = '''const sink = arg => { view.drawText(arg[0].slot); };
                    const receiver = function (arg) { sink(arg); };
                    function wrapper(ignored, value) { receiver(value); }
                    const fixture = {group: [{slot: "forwarded"}, {slot: "other slot"}], spare};
                    const first = fixture.group;
                    let second = first;
                    wrapper(ignored, second);
                    const config = {group: {slot: "alias only"}, other};
                    const alias = config.group; view.drawText(alias.slot);
                    function end(arg) { use(arg.slot); }
                    function start(arg) { end(arg); }
                    const local = {slot: "function only", other}; start(local);'''
        outcomes = {
            "forwarded": "via_alias_and_function_display_call_shape",
            "other slot": "no_exact_forwarded_read",
            "alias only": "via_alias_display_call_shape",
            "function only": "via_function_other_call_read",
        }
        traces = classify(source, outcomes, diagnostic="local_initializer_forwarding")
        self.assertEqual({key: result[0] for key, result in traces.items()}, outcomes)
        self.assertTrue({"alias_forward", "function_forward"} <= set(traces["forwarded"][1]))

    def test_follows_lexical_blocks_iifes_and_directly_called_closures(self):
        source = '''function render() { view.drawText(data.group.slot); }
                    const data = {group: {slot: "closure", other}, spare};
                    render();
                    const branch = {slot: "block", other};
                    if (ready) { view.drawText(branch.slot); }
                    const wrapped = {group: {slot: "iife", other}, spare};
                    (function () { const alias = wrapped.group; view.drawText(alias.slot); })();
                    function sink(arg) { view.drawText(arg.slot); }
                    const chain = {group: {slot: "chain", other}, spare};
                    const run = () => { const alias = chain.group; sink(alias); };
                    run();'''
        expected = {
            "closure": "via_nested_scope_display_call_shape",
            "block": "via_nested_scope_display_call_shape",
            "iife": "via_nested_scope_and_alias_display_call_shape",
            "chain": "via_nested_scope_alias_and_function_display_call_shape",
        }
        traces = classify(source, expected, diagnostic="local_initializer_forwarding")
        self.assertEqual({key: trace[0] for key, trace in traces.items()}, expected)

    def test_nested_capture_needs_an_unambiguous_activation_after_initialization(self):
        source = '''render(); const early = {slot: "early", other};
                    function render() { view.drawText(early.slot); }
                    const dormant = {slot: "dormant", other};
                    external(function () { view.drawText(dormant.slot); })();
                    const data = {slot: "parameter shadow", other};
                    if (ready) { const render = data => view.drawText(data.slot); }
                    const hidden = {slot: "destructured", other};
                    if (ready) { const {hidden} = foreign; view.drawText(hidden.slot); }
                    const outer = {slot: "block var", other};
                    function local() { if (ready) { var outer; } view.drawText(outer.slot); }
                    local();
                    const written = {slot: "nested write", other};
                    function mutate() { written.slot = other; view.drawText(written.slot); }
                    mutate();'''
        expected = dict.fromkeys(("early", "dormant", "parameter shadow", "destructured", "block var"),
                                 "no_exact_forwarded_read")
        expected["nested write"] = "forwarded_property_reassigned"
        traces = classify(source, expected, diagnostic="local_initializer_forwarding")
        self.assertEqual({key: trace[0] for key, trace in traces.items()}, expected)
        self.assertIn("nested_activation_unresolved", traces["dormant"][1])
        for case in ("parameter shadow", "destructured", "block var"):
            self.assertIn("nested_binding_ambiguous", traces[case][1])

    def test_leaves_mutated_ambiguous_and_dynamic_reference_paths_unconfirmed(self):
        source = '''const changed = {slot: "mutated", other};
                    const alias = changed; alias[dynamic] = something; view.drawText(alias.slot);
                    const source = {slot: "reassigned", other};
                    let unstable = source; unstable = spare; view.drawText(unstable.slot);
                    function cycle(arg) { cycle(arg); }
                    const loop = {slot: "cyclic", other}; cycle(loop);
                    const data = {slot: "dynamic read", other}; external(data[unknown]);
                    function typed(arg) { view.drawText(arg.slot); }
                    function wrapper(typed, arg) { typed(arg); }
                    const shadow = {slot: "shadowed callee", other}; wrapper(opaque, shadow);'''
        outcomes = {
            "mutated": "forwarded_property_reassigned",
            "reassigned": "binding_not_single_assignment",
            "cyclic": "no_exact_forwarded_read",
            "dynamic read": "no_exact_forwarded_read",
            "shadowed callee": "no_exact_forwarded_read",
        }
        traces = classify(source, outcomes, diagnostic="local_initializer_forwarding")
        self.assertEqual({key: result[0] for key, result in traces.items()}, outcomes)
        self.assertIn("dynamic_or_optional_access", traces["dynamic read"][1])
        self.assertIn("callee_shadowed_or_reassigned", traces["shadowed callee"][1])
