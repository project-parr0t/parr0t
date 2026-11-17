"""Managed Unity inventory and selected-occurrence rewriting (stdlib only).

Public API:
    scan_assembly(path: Path, relative_path: str, *, method='primary') -> dict
    patch_assembly(raw: bytes, replacements: list[dict]) -> bytes
Replacements have source, target and location={method_token,il_offset,string_token?}
or location={resource_token,entry_index,resource_name?} for embedded .resources.
string_token is checked when supplied, but is not occurrence identity.

Independence is enumeration-level, not independently implemented format decoding:
primary walks method instructions; independent enumerates #US then joins uses
from separately parsed method bodies. Both construct fresh Image instances from
bytes and share only the public-format reader. Neither consumes another result.

Flow proofs are conservative may-flow, not a claim of runtime visibility. Exact
assembly/type/member/signature sink bindings are required. Unknown operations,
exception flow, virtual dispatch and external transformations on display paths
remain pending; raw strings alone are not completeness blockers.
"""
from collections import defaultdict, deque
from pathlib import Path, PurePosixPath
import hashlib
import struct

from unity_clr import Image, ManagedFormatError, align, compress, require, take, unpack
import unity_dotnet_resources as resource_format
import unity_pe


# Each entry binds an assembly, declaring type, method and accepted string slots.
# No wildcard member suffixes or title-specific names participate in decisions.
SINKS = {
    ('UnityEngine.UI', 'UnityEngine.UI.Text', 'set_text'): 'setter',
    ('Unity.TextMeshPro', 'TMPro.TMP_Text', 'set_text'): 'setter',
    ('Unity.TextMeshPro', 'TMPro.TMP_Text', 'SetText'): 'first_string',
    ('Unity.TextMeshPro', 'TMPro.TextMeshProUGUI', 'SetText'): 'first_string',
    ('Unity.TextMeshPro', 'TMPro.TextMeshPro', 'SetText'): 'first_string',
    ('Unity.TextMeshPro', 'TMPro.TextMeshProUGUI', 'set_text'): 'setter',
    ('Unity.TextMeshPro', 'TMPro.TextMeshPro', 'set_text'): 'setter',
    ('UnityEngine.UIElementsModule', 'UnityEngine.UIElements.TextElement', 'set_text'): 'setter',
    ('UnityEngine.IMGUIModule', 'UnityEngine.GUI', 'Label'): 'string_arguments',
    ('UnityEngine.IMGUIModule', 'UnityEngine.GUI', 'Box'): 'string_arguments',
    ('UnityEngine.IMGUIModule', 'UnityEngine.GUI', 'Button'): 'string_arguments',
    ('UnityEngine.IMGUIModule', 'UnityEngine.GUILayout', 'Label'): 'string_arguments',
    ('UnityEngine.IMGUIModule', 'UnityEngine.GUILayout', 'Box'): 'string_arguments',
    ('UnityEngine.IMGUIModule', 'UnityEngine.GUILayout', 'Button'): 'string_arguments',
}


def _node(kind, *parts):
    return ':'.join([kind] + [str(p) for p in parts])


def _types(image, pending):
    attrs = defaultdict(set)
    for parent, ctor, _ in image.rows.get(12, []):
        pt, pr = image.coded('HasCustomAttribute', parent)
        ct, cr = image.coded('CustomAttributeType', ctor)
        owner, _ = image.member(ct << 24 | cr)
        attrs[(pt, pr)].add((owner.get('assembly'), owner.get('name')))
    result, field_records = {}, {}
    for rid, row in enumerate(image.rows.get(2, []), 1):
        desc = image.type_identity(2, rid)
        if desc['name'] == '<Module>':
            continue
        base = image.type_identity(*image.coded('TypeDefOrRef', row[3])) if row[3] else None
        fields = []
        for fid, owner in image.field_owner.items():
            if owner != rid:
                continue
            flags, name, blob = image.row(4, fid)
            signature = image.blob(blob)
            try:
                require(signature[:1] == b'\x06', 'CLR_FIELD_SIGNATURE')
                ft, end = image.signature(signature, 1)
                require(end == len(signature), 'CLR_FIELD_SIGNATURE_TRAILING')
            except (ValueError, UnicodeError) as exc:
                ft = {'kind': 'unsupported', 'reason': str(exc)}
            fa = attrs[(4, fid)]
            unity_attr = lambda name: any(a in ('UnityEngine', 'UnityEngine.CoreModule') and n == 'UnityEngine.' + name for a, n in fa)
            serialized = not flags & (0x10 | 0x40 | 0x80 | 0x20) and ((flags & 7) == 6 or unity_attr('SerializeField') or unity_attr('SerializeReference'))
            if unity_attr('SerializeReference'):
                ft = {'kind': 'unsupported', 'reason': 'serialize_reference', 'declared': ft}
            record = {'name': image.string(name), 'type': ft, 'serialized': bool(serialized), 'static': bool(flags & 0x10), 'token': 0x04000000 | fid}
            fields.append(record)
            field_records[0x04000000 | fid] = record
            if serialized and _unsupported(ft):
                pending.append({'reason': 'managed_field_layout_unsupported', 'type_token': 0x02000000 | rid, 'field_token': 0x04000000 | fid, 'scope': 'serialized_layout', 'affects_display': False})
        record = {'name': desc['name'], 'assembly': image.assembly, 'base': base, 'fields': fields,
                  'enum': bool(base and base.get('name') == 'System.Enum' and base.get('assembly') in ('mscorlib', 'System.Runtime', 'System.Private.CoreLib')),
                  'serializable': bool(row[0] & 0x2000), 'token': 0x02000000 | rid}
        result[image.assembly + '::' + desc['name']] = record
    return result, field_records


def _unsupported(desc):
    return desc.get('kind') == 'unsupported' or ('element' in desc and _unsupported(desc['element']))


def _method_data(image, pending):
    bodies = {}
    for rid, row in enumerate(image.rows.get(6, []), 1):
        if not row[0]:
            continue
        try:
            bodies[0x06000000 | rid] = image.body(rid)
        except (ValueError, UnicodeError) as exc:
            pending.append({'reason': 'managed_method_decode_unsupported', 'method_token': 0x06000000 | rid, 'detail': str(exc), 'scope': 'method_decode'})
    return bodies


def _enumerate_primary(image, bodies):
    strings = dict(image.user_strings())
    for method, body in sorted(bodies.items()):
        for instruction in body['instructions']:
            if instruction['opcode'] == 0x72:
                token = int.from_bytes(instruction['operand'], 'little')
                require(token >> 24 == 0x70 and token & 0xffffff in strings, 'CLR_LDSTR_BINDING')
                yield method, instruction['offset'], token, strings[token & 0xffffff]


def _enumerate_independent(image, bodies):
    # Heap-driven membership and output, not a copy/relabel of primary inventory.
    uses = defaultdict(list)
    for method, body in bodies.items():
        for instruction in body['instructions']:
            if instruction['opcode'] == 0x72:
                token = int.from_bytes(instruction['operand'], 'little')
                require(token >> 24 == 0x70, 'CLR_LDSTR_BINDING')
                uses[token & 0xffffff].append((method, instruction['offset'], token))
    for offset, text in image.user_strings():
        for method, il, token in uses.pop(offset, []):
            yield method, il, token, text
    require(not uses, 'CLR_LDSTR_BINDING')


def _member_candidates(image, table, owner, name):
    attribute = '_method_member_index' if table == 6 else '_field_member_index'
    index = getattr(image, attribute, None)
    if index is None:
        index = defaultdict(list)
        for rid in range(1, len(image.rows.get(table, [])) + 1):
            token = table << 24 | rid
            declaring, member = image.member(token)
            index[(declaring.get('assembly'), declaring.get('name'), member)].append((declaring, token))
        setattr(image, attribute, index)
    return [token for declaring, token in index.get((owner.get('assembly'), owner.get('name'), name), ()) if declaring == owner]


def _local_method(image, token):
    table, rid = token >> 24, token & 0xffffff
    if table == 43:
        table, rid = image.coded('MethodDefOrRef', image.row(43, rid)[0])
        token = table << 24 | rid
    if table == 6:
        return token
    if table != 10:
        return None
    cache = getattr(image, '_local_method_cache', None)
    if cache is None:
        cache = image._local_method_cache = {}
    if token in cache:
        return cache[token]
    owner, name = image.member(token)
    if owner.get('assembly') != image.assembly:
        cache[token] = None
        return None
    signature = image.method_signature(token)
    matches = [candidate for candidate in _member_candidates(image, 6, owner, name)
               if image.method_signature(candidate) == signature]
    cache[token] = matches[0] if len(matches) == 1 else None
    return cache[token]


def _field_token(image, token):
    if token >> 24 == 4:
        image.row(4, token & 0xffffff)
        return token
    if token >> 24 != 10:
        return token
    cache = getattr(image, '_field_token_cache', None)
    if cache is None:
        cache = image._field_token_cache = {}
    if token in cache:
        return cache[token]
    owner, name = image.member(token)
    if owner.get('assembly') != image.assembly:
        cache[token] = token
        return token
    signature = image.blob(image.row(10, token & 0xffffff)[2])
    matches = [candidate for candidate in _member_candidates(image, 4, owner, name)
               if image.blob(image.row(4, candidate & 0xffffff)[2]) == signature]
    cache[token] = matches[0] if len(matches) == 1 else token
    return cache[token]


def _flow(image, bodies, occurrences, fields, resources, resource_occurrences):
    reverse = defaultdict(set)
    edges, edge_keys = [], set()
    sinks, unknown = {}, {}
    method_issues = defaultdict(set)
    method_links = defaultdict(set)
    method_fields = defaultdict(set)
    def edge(src, dst, kind, method, il):
        key = (src, dst, kind, method, il)
        if key not in edge_keys:
            edge_keys.add(key)
            edges.append({'from': src, 'to': dst, 'kind': kind, 'method_token': method, 'il_offset': il})
            reverse[dst].add(src)
    literal_nodes = {(o['location']['method_token'], o['location']['il_offset']): _node('literal', o['location']['method_token'], o['location']['il_offset']) for o in occurrences}
    literal_records = {literal_nodes[(o['location']['method_token'], o['location']['il_offset'])]: o for o in occurrences}
    managers, lookups, identifier_nodes = {}, {}, set()
    for method, body in bodies.items():
        ins = {i['offset']: i for i in body['instructions']}
        if not ins:
            continue
        if body['handlers']:
            method_issues[method].add('exception_control_flow')
        try:
            method_sig = image.method_signature(method)
        except ValueError:
            method_issues[method].add('method_signature_unsupported')
            continue
        states, queue = {}, deque()
        def enqueue(offset, stack, locals_):
            if offset not in ins:
                method_issues[method].add('control_flow_boundary')
                return
            current = states.get(offset)
            incoming = (tuple(frozenset(v) for v in stack), {k: frozenset(v) for k, v in locals_.items()})
            if current is None:
                states[offset] = incoming
                queue.append(offset)
            else:
                if len(current[0]) != len(incoming[0]):
                    method_issues[method].add('stack_merge_unsupported')
                    return
                merged = (tuple(a | b for a, b in zip(current[0], incoming[0])),
                          {k: current[1].get(k, frozenset()) | incoming[1].get(k, frozenset()) for k in current[1].keys() | incoming[1].keys()})
                if merged != current:
                    states[offset] = merged
                    queue.append(offset)
        enqueue(min(ins), [], {})
        # Handlers are roots too; unmodelled exceptional edges stay explicit.
        for handler in body['handlers']:
            node = _node('unknown', method, handler['handler_offset'], 'exception')
            unknown[node] = 'exception_control_flow'
            enqueue(handler['handler_offset'], [{node}] if handler['flags'] == 0 else [], {})
            if handler['flags'] & 1:
                enqueue(handler['value'], [{node}], {})
        while queue:
            offset = queue.popleft()
            stack = [set(v) for v in states[offset][0]]
            locals_ = dict(states[offset][1])
            i = ins[offset]
            op, data = i['opcode'], i['operand']
            operand = int.from_bytes(data, 'little')
            def pop():
                if stack:
                    return stack.pop()
                node = _node('unknown', method, offset, 'stack')
                unknown[node] = 'stack_value_unresolved'
                method_issues[method].add('stack_value_unresolved')
                return {node}
            def fresh(reason):
                node = _node('unknown', method, offset, reason)
                unknown[node] = reason
                return {node}
            terminal = False
            if op == 0x72:
                stack.append({literal_nodes[(method, offset)]})
            elif 0x02 <= op <= 0x05 or op in (0x0e, 0xfe09):
                arg = op - 2 if op <= 5 else operand
                stack.append({_node('argument', method, arg)})
            elif op in (0x0f, 0xfe0a):
                stack.append(fresh('argument_address'))
            elif op in (0x10, 0xfe0b):
                value = pop()
                for src in value:
                    edge(src, _node('argument', method, operand), 'argument_store', method, offset)
            elif 0x06 <= op <= 0x09 or op in (0x11, 0xfe0c):
                local = op - 6 if op <= 9 else operand
                stack.append(set(locals_.get(local, fresh('local_uninitialized'))))
            elif op in (0x12, 0xfe0d):
                stack.append(fresh('local_address'))
            elif 0x0a <= op <= 0x0d or op in (0x13, 0xfe0e):
                local = op - 0x0a if op <= 0x0d else operand
                locals_[local] = frozenset(pop())
            elif op == 0x25:
                value = pop()
                stack.extend([value, set(value)])
            elif op == 0x26:
                pop()
            elif op in (0x28, 0x6f, 0x73):
                try:
                    signature = image.method_signature(operand)
                    owner, name = image.member(operand)
                    args = [pop() for _ in signature['arguments']][::-1]
                    receiver = pop() if signature['instance'] and op != 0x73 else set()
                    target = _local_method(image, operand)
                    sink_mode = SINKS.get((owner.get('assembly'), owner.get('name'), name))
                    slots = [n for n, a in enumerate(signature['arguments']) if a.get('kind') == 'string']
                    if sink_mode == 'setter' and not (signature['instance'] and len(args) == 1 and slots == [0] and signature['return']['kind'] == 'void'):
                        sink_mode = None
                    if sink_mode == 'first_string':
                        if not signature['instance'] or not slots or slots[0] != 0 or signature['return']['kind'] != 'void':
                            sink_mode = None
                        slots = [0]
                    if sink_mode:
                        sink = _node('sink', method, offset)
                        sinks[sink] = {'method_token': method, 'il_offset': offset, 'sink': {'assembly': owner['assembly'], 'type': owner['name'], 'member': name}}
                        for slot in slots:
                            for src in args[slot]:
                                edge(src, sink, 'display_argument', method, offset)
                    inputs = receiver | set().union(*args) if args else receiver
                    resource_api = owner.get('assembly') in ('mscorlib', 'System.Resources.ResourceManager', 'System.Private.CoreLib') and owner.get('name') == 'System.Resources.ResourceManager'
                    manager_ctor = resource_api and name == '.ctor' and op == 0x73 and signature['arguments'] and signature['arguments'][0]['kind'] == 'string'
                    resource_lookup = resource_api and name == 'GetString' and signature['instance'] and args and signature['arguments'][0]['kind'] == 'string' and signature['return']['kind'] == 'string'
                    if target:
                        method_links[method].add(target)
                        values = ([receiver] if signature['instance'] else []) + args
                        for n, value in enumerate(values):
                            for src in value:
                                edge(src, _node('argument', target, n), 'call_argument', method, offset)
                        returned = {_node('return', target)}
                        if target not in bodies:
                            unknown[_node('return', target)] = 'managed_body_missing'
                            method_issues[method].add('managed_body_missing')
                        if op == 0x6f:
                            method_issues[method].add('virtual_dispatch_unresolved')
                    else:
                        call = _node('unknown', method, offset, 'external_call')
                        if not sink_mode and not manager_ctor and not resource_lookup:
                            unknown[call] = 'external_call_unresolved'
                            method_issues[method].add('external_call_unresolved')
                        if manager_ctor:
                            managers[call] = args[0]
                        elif resource_lookup:
                            lookups[call] = (receiver, args[0], method, offset)
                            unknown[call] = 'managed_resource_lookup_unresolved'
                        else:
                            for src in inputs:
                                edge(src, call, 'external_argument', method, offset)
                        returned = {call}
                    if signature['vararg']:
                        method_issues[method].add('vararg_call_unresolved')
                    if op == 0x73 or signature['return']['kind'] != 'void':
                        stack.append(returned)
                    edges_key = {'kind': 'call', 'method_token': method, 'il_offset': offset, 'target_token': operand, 'resolved_method_token': target, 'virtual': op == 0x6f}
                    key = ('call', method, offset)
                    if key not in edge_keys:
                        edge_keys.add(key)
                        edges.append(edges_key)
                except (ValueError, KeyError):
                    method_issues[method].add('call_signature_unresolved')
                    stack = [fresh('call_signature_unresolved')]
            elif op in (0x7b, 0x7c, 0x7e, 0x7f, 0x7d, 0x80):
                token = _field_token(image, operand)
                field = _node('field', token)
                method_fields[method].add(field)
                if op in (0x7d, 0x80):
                    value = pop()
                    if op == 0x7d:
                        pop()
                    for src in value:
                        edge(src, field, 'field_store', method, offset)
                else:
                    if op in (0x7b, 0x7c):
                        pop()
                    stack.append({field} if op in (0x7b, 0x7e) else fresh('field_address'))
                key = ('field', method, offset)
                if key not in edge_keys:
                    edge_keys.add(key)
                    edges.append({'kind': 'field_access', 'method_token': method, 'il_offset': offset, 'field_token': token, 'write': op in (0x7d, 0x80)})
            elif op == 0x2a:
                if method_sig['return']['kind'] != 'void':
                    for src in pop():
                        edge(src, _node('return', method), 'method_return', method, offset)
                terminal = True
            elif op in (0x2b, 0x38, 0xdd, 0xde):
                if op in (0xdd, 0xde):
                    stack = []
            elif op in (0x2c, 0x2d, 0x39, 0x3a, 0x45):
                pop()
            elif 0x2e <= op <= 0x37 or 0x3b <= op <= 0x44:
                pop(); pop()
            elif op in (0x7a, 0xfe1a):
                if op == 0x7a:
                    pop()
                terminal = True
            elif op in (0xdc, 0xfe11, 0x27):
                method_issues[method].add('control_transfer_unresolved')
                terminal = True
            elif op in (0x00, 0x01, 0xfe12, 0xfe13, 0xfe14, 0xfe16, 0xfe19, 0xfe1e):
                pass
            elif op == 0x14 or 0x15 <= op <= 0x23:
                stack.append(set())
            elif op in (0x74, 0x75, 0x8c, 0xa5) or 0x67 <= op <= 0x6e or op in (0x76, 0xc3) or 0xb3 <= op <= 0xba or 0xd1 <= op <= 0xd5 or op in (0xe0,):
                stack.append(pop())
            elif 0x58 <= op <= 0x64 or 0xd6 <= op <= 0xdb or 0xfe01 <= op <= 0xfe05:
                value = pop() | pop()
                node = next(iter(fresh('computed_value_unresolved')))
                for src in value:
                    edge(src, node, 'computed_value', method, offset)
                stack.append({node})
            elif op in (0x65, 0x66):
                stack.append(pop())
            else:
                # Preserve incoming origins conservatively, never silently shift
                # a stack and claim a later sink has been completely analyzed.
                value = set().union(*stack) if stack else set()
                node = next(iter(fresh('instruction_semantics_unsupported')))
                for src in value:
                    edge(src, node, 'unsupported_instruction', method, offset)
                stack = [{node}]
                method_issues[method].add('instruction_semantics_unsupported')
            if not terminal:
                for target in i['targets']:
                    enqueue(target, stack, locals_)
                if op not in (0x2b, 0x38, 0xdd, 0xde) and i['end'] < len(body['code']):
                    enqueue(i['end'], stack, locals_)
    # A separate backwards walk per sink binds field consumers precisely.
    reached, consumers = set(), defaultdict(list)
    def ancestors(roots):
        seen, todo = set(roots), list(roots)
        while todo:
            for src in reverse[todo.pop()]:
                if src not in seen:
                    seen.add(src); todo.append(src)
        return seen
    for call, (receiver, key_values, method, offset) in lookups.items():
        key_nodes = ancestors(key_values) & literal_records.keys()
        manager_sources = set().union(*(managers[n] for n in ancestors(receiver) if n in managers))
        base_nodes = ancestors(manager_sources) & literal_records.keys()
        identifier_nodes.update(key_nodes | base_nodes)
        keys = {literal_records[n]['source'] for n in key_nodes}
        bases = {literal_records[n]['source'] for n in base_nodes}
        matched = False
        if len(keys) == 1 and len(bases) == 1:
            for token, record in resources.items():
                if record['name'] != next(iter(bases)) + '.resources':
                    continue
                if record['linked']:
                    unknown[call] = 'linked_managed_resource_unresolved'
                    continue
                for occurrence in resource_occurrences:
                    loc = occurrence['location']
                    if loc['resource_token'] == token and occurrence['entry_name'] in keys:
                        resource_node = _node('resource', token, loc['entry_index'])
                        edge(resource_node, call, 'resource_lookup_value', method, offset)
                        matched = True
        if matched:
            unknown.pop(call, None)
            method_issues[method].add('resource_culture_resolution_unverified')
    for sink, proof in sinks.items():
        seen, todo = {sink}, [sink]
        while todo:
            node = todo.pop()
            consumers[node].append(proof)
            for src in reverse[node]:
                if src not in seen:
                    seen.add(src); todo.append(src)
        reached.update(seen)
    for method, used_fields in method_fields.items():
        if used_fields & reached:
            for reason in sorted(method_issues[method]):
                node = _node('unknown', method, 'field_flow', reason)
                unknown[node] = reason
                reached.add(node)
                for field in used_fields & reached:
                    consumers[node].extend(consumers[field])
    for node in sorted(reached):
        if node.startswith('field:') and int(node.split(':')[1]) >> 24 != 4:
            unknown[node] = 'external_field_unresolved'
        elif node.startswith('argument:') and not reverse[node]:
            _, method, slot = node.split(':')
            signature = image.method_signature(int(method))
            index = int(slot) - int(signature['instance'])
            if 0 <= index < len(signature['arguments']) and signature['arguments'][index]['kind'] == 'string':
                unknown[node] = 'managed_display_argument_input_unbound'
    connected_methods = {p['method_token'] for p in sinks.values()}
    changed = True
    while changed:
        changed = False
        for method, targets in method_links.items():
            if method not in connected_methods and targets & connected_methods:
                connected_methods.add(method); changed = True
    pending = []
    for node in sorted(reached & unknown.keys()):
        pending.append({'reason': unknown[node], 'dependency': node, 'scope': 'display_dependency', 'consumers': consumers[node]})
    for method in sorted(connected_methods):
        for reason in sorted(method_issues[method]):
            pending.append({'reason': reason, 'method_token': method, 'scope': 'display_dependency'})
    for token, field in fields.items():
        node = _node('field', token)
        if node in reached:
            field['classification'] = 'display'
            field['display_consumers'] = consumers[node]
            field['proof'] = {'kind': 'managed_field_flow', 'field_token': token}
    for occurrence in resource_occurrences:
        loc = occurrence['location']
        node = _node('resource', loc['resource_token'], loc['entry_index'])
        if node in reached:
            occurrence['classification'] = 'display'
            occurrence['reason'] = 'structural_resource_display_flow'
            occurrence['proof']['flow_node'] = node
    for occurrence in occurrences:
        loc = occurrence['location']
        node = literal_nodes[(loc['method_token'], loc['il_offset'])]
        if node in reached:
            occurrence['classification'] = 'display'
            occurrence['reason'] = 'structural_display_argument_flow'
            if node in identifier_nodes:
                occurrence['display_projection_required'] = True
                pending.append({'reason': 'managed_shared_identifier_write_unsafe',
                                'scope': 'display_dependency', 'dependency': node,
                                'location': dict(loc), 'consumers': consumers[node]})
        elif node in identifier_nodes:
            occurrence['classification'] = 'non_display'
            occurrence['reason'] = 'structural_resource_identifier'
        else:
            occurrence['classification'] = 'uncertain'
            occurrence['reason'] = 'no_proven_display_consumer'
        occurrence['proof'] = {'method_ref': _node('method', loc['method_token']), 'flow_node': node}
    return edges, pending, sinks


def scan_assembly(path: Path, relative_path: str, *, method: str = 'primary') -> dict:
    require(method in ('primary', 'independent'), 'MANAGED_SCAN_METHOD')
    relative = PurePosixPath(relative_path.replace('\\', '/'))
    require(not relative.is_absolute() and '..' not in relative.parts, 'MANAGED_RELATIVE_PATH')
    raw = Path(path).read_bytes()
    image = Image(raw)
    pending = []
    if not image.assembly:
        pending.append({'reason': 'managed_assembly_identity_missing', 'scope': 'managed_layout'})
    types, fields = _types(image, pending)
    bodies = _method_data(image, pending)
    resource_occurrences, resource_pending, resources, _ = resource_format.inventory(image, independent=method == 'independent')
    pending.extend(resource_pending)
    iterator = _enumerate_primary if method == 'primary' else _enumerate_independent
    occurrences = [{'kind': 'managed_literal', 'file': str(relative),
                    'location': {'method_token': mid, 'il_offset': off, 'string_token': token}, 'source': text}
                   for mid, off, token, text in iterator(image, bodies)]
    occurrences.sort(key=lambda o: (o['location']['method_token'], o['location']['il_offset']))
    dependencies, flow_pending, sinks = _flow(image, bodies, occurrences, fields, resources, resource_occurrences)
    pending.extend(flow_pending)
    for occurrence in resource_occurrences:
        occurrence['file'] = str(relative)
    occurrences.extend(resource_occurrences)
    proofs = {'file': {'kind': 'managed_image', 'sha256': hashlib.sha256(raw).hexdigest(), 'method': method,
                       'enumeration': 'method_instruction_walk' if method == 'primary' else 'user_string_heap_use_join',
                       'shared_decoder': 'first_party_ecma335', 'runtime_verified': False},
              'methods': {_node('method', token): {'method_token': token, 'code_sha256': hashlib.sha256(body['code']).hexdigest(),
                                                   'instruction_count': len(body['instructions']), 'exception_regions': body['handlers']}
                          for token, body in bodies.items()}, 'display_sinks': list(sinks.values())}
    proofs['resources'] = {'resource:' + str(token): {'resource_token': token, 'name': record['name'], 'linked': record['linked'],
                                                    'entry_count': len(record.get('parsed', {}).get('entries', []))}
                           for token, record in resources.items()}
    return {'occurrences': occurrences, 'types': types, 'dependencies': dependencies, 'pending': pending, 'proofs': proofs}


def _us_entry(text):
    require(isinstance(text, str), 'MANAGED_TARGET_TYPE')
    data = text.encode('utf-16-le', errors='surrogatepass')
    # II.24.2.4: special-character flag considers UTF-16 code units.
    units = [u[0] for u in struct.iter_unpack('<H', data)]
    special = any(u >= 0x100 or 1 <= u <= 8 or 0x0e <= u <= 0x1f or u in (0x27, 0x2d, 0x7f) for u in units)
    return compress(len(data) + 1) + data + bytes([int(special)])




def patch_assembly(raw: bytes, replacements: list[dict]) -> bytes:
    """Append selected #US or embedded .resources values without hooks.

    Unsigned IL-only PE32/PE32+ images with ordinary alignment are supported,
    including safe section-header growth. Original bodies/identifiers remain
    unchanged except selected ldstr operands; resource value offsets and
    ManifestResource offsets change only for selected embedded string entries.
    Signed, mixed-mode, ReadyToRun, overlays, pointer tables and unsafe layouts
    are explicit errors. Unselected shared-token/value uses remain unchanged.
    """
    require(isinstance(raw, bytes) and isinstance(replacements, list), 'MANAGED_PATCH_ARGUMENTS')
    if not replacements:
        return raw
    image = Image(raw)
    flags = unpack('<I', raw, image.cli + 16)[0]
    require(flags & 1 and not flags & (8 | 16), 'MANAGED_PATCH_SIGNED_OR_MIXED')
    require(not any(unpack('<II', raw, image.dd + 4 * 8)), 'MANAGED_PATCH_AUTHENTICODE')
    for offset in (32, 40, 48, 56, 64):
        require(not any(unpack('<II', raw, image.cli + offset)), 'MANAGED_PATCH_UNSAFE_CLI_DIRECTORY')
    if image.rows.get(32):
        row = image.row(32, 1)
        require(not row[5] & 1 and not image.blob(row[6]), 'MANAGED_PATCH_STRONG_NAME')
    require(set(image.streams) <= {'#~', '#-', '#Strings', '#US', '#GUID', '#Blob'}, 'MANAGED_PATCH_METADATA_STREAM_UNSUPPORTED')
    strings = dict(image.user_strings())
    heap = bytearray(image.streams.get('#US', b''))
    resource_replacements = defaultdict(list)
    for replacement in replacements:
        require(isinstance(replacement, dict) and isinstance(replacement.get('location'), dict), 'MANAGED_REPLACEMENT_LOCATION')
        if 'resource_token' in replacement['location']:
            resource_replacements[replacement['location']['resource_token']].append(replacement)
    resource_occurrences, _, resources, original_resource_data = resource_format.inventory(image)
    resource_data = bytearray(original_resource_data)
    table_name = '#~' if '#~' in image.streams else '#-'
    table_data = bytearray(image.streams[table_name])
    for resource_token, group in resource_replacements.items():
        require(type(resource_token) is int and resource_token >> 24 == 40 and resource_token in resources, 'RESOURCE_REPLACEMENT_TOKEN')
        record = resources[resource_token]
        require(not record['linked'] and 'parsed' in record, 'RESOURCE_REPLACEMENT_UNSUPPORTED')
        for replacement in group:
            loc = replacement['location']
            require('resource_name' not in loc or loc['resource_name'] == record['name'], 'RESOURCE_REPLACEMENT_NAME')
        rebuilt = resource_format.rewrite(record['raw'], group)
        resource_data.extend(bytes((-len(resource_data)) % 8))
        offset = len(resource_data)
        require(offset <= 0xffffffff and len(rebuilt) <= 0xffffffff, 'RESOURCE_INTEGER_LIMIT')
        resource_data.extend(struct.pack('<I', len(rebuilt)) + rebuilt)
        row_offset = image.row_offsets[40][(resource_token & 0xffffff) - 1]
        struct.pack_into('<I', table_data, row_offset, offset)
    writes, selected, new_tokens = [], set(), {}
    bodies = {0x06000000 | rid: image.body(rid) for rid, row in enumerate(image.rows.get(6, []), 1) if row[0]}
    spans = sorted((body['start'], body['start'] + len(body['code'])) for body in bodies.values())
    require(all(left[1] <= right[0] for left, right in zip(spans, spans[1:])), 'MANAGED_PATCH_ALIASED_BODY')
    unsafe_identifiers = set()
    if any('resource_token' not in row['location'] for row in replacements):
        literals = [{'location': {'method_token': mid, 'il_offset': off}, 'source': text}
                    for mid, off, _, text in _enumerate_primary(image, bodies)]
        _flow(image, bodies, literals, {}, resources, resource_occurrences)
        unsafe_identifiers = {(row['location']['method_token'], row['location']['il_offset'])
                              for row in literals if row.get('display_projection_required')}
    for replacement in replacements:
        require(isinstance(replacement, dict), 'MANAGED_REPLACEMENT_SHAPE')
        loc = replacement.get('location')
        require(isinstance(loc, dict), 'MANAGED_REPLACEMENT_LOCATION')
        if 'resource_token' in loc:
            continue
        method, il = loc.get('method_token'), loc.get('il_offset')
        require(type(method) is int and method >> 24 == 6 and type(il) is int and il >= 0, 'MANAGED_REPLACEMENT_LOCATION')
        require((method, il) not in selected, 'MANAGED_REPLACEMENT_DUPLICATE')
        selected.add((method, il))
        if method not in bodies:
            bodies[method] = image.body(method & 0xffffff)
        body = bodies[method]
        require(body is not None, 'MANAGED_REPLACEMENT_NO_BODY')
        candidates = [i for i in body['instructions'] if i['offset'] == il]
        require(len(candidates) == 1 and candidates[0]['opcode'] == 0x72, 'MANAGED_REPLACEMENT_NOT_LDSTR')
        instruction = candidates[0]
        old = int.from_bytes(instruction['operand'], 'little')
        require(old >> 24 == 0x70 and old & 0xffffff in strings, 'CLR_LDSTR_BINDING')
        require('string_token' not in loc or loc['string_token'] == old, 'MANAGED_REPLACEMENT_TOKEN_MISMATCH')
        require(isinstance(replacement.get('source'), str) and strings[old & 0xffffff] == replacement['source'], 'MANAGED_REPLACEMENT_SOURCE_MISMATCH')
        target = replacement.get('target')
        require(isinstance(target, str), 'MANAGED_TARGET_TYPE')
        require(target == replacement['source'] or (method, il) not in unsafe_identifiers,
                'MANAGED_SHARED_IDENTIFIER_WRITE_UNSAFE')
        if target not in new_tokens:
            require(len(heap) <= 0xffffff, 'MANAGED_PATCH_US_TOKEN_LIMIT')
            new_tokens[target] = 0x70000000 | len(heap)
            heap.extend(_us_entry(target))
        operand_offset = body['start'] + il + 1
        require(operand_offset >= image.header_size and not (operand_offset < image.root + image.metadata_size and image.root < operand_offset + 4)
                and not (operand_offset < image.cli + 72 and image.cli < operand_offset + 4), 'MANAGED_PATCH_OVERLAPPING_BODY')
        writes.append((operand_offset, new_tokens[target]))
    # Rebuild stream directory with identical names/ordering and original bytes.
    metadata = bytearray(image.metadata_prefix)
    for name, (header, _, _) in image.stream_headers.items():
        metadata.extend(b'\0' * (align(len(metadata), 4) - len(metadata)))
        data = bytes(heap) if name == '#US' else bytes(table_data) if name == table_name else image.streams[name]
        struct.pack_into('<II', metadata, header, len(metadata), len(data))
        metadata.extend(data)
    resource_section_offset = align(len(metadata), 8)
    payload = bytes(metadata)
    if resource_replacements:
        payload += bytes(resource_section_offset - len(payload)) + bytes(resource_data)
    result = bytearray(raw)
    for offset, token in writes:
        struct.pack_into('<I', result, offset, token)
    appended, rva = unity_pe.append_data(bytes(result), payload)
    relocated = unity_pe.PE(appended)
    cli_rva, _ = relocated.directory(14)
    cli_offset = relocated.at(cli_rva, 72)
    result = bytearray(appended)
    struct.pack_into('<II', result, cli_offset + 8, rva, len(metadata))
    if resource_replacements:
        struct.pack_into('<II', result, cli_offset + 24, rva + resource_section_offset, len(resource_data))
    struct.pack_into('<I', result, relocated.opt + 64, unity_pe._checksum(result, relocated.opt + 64))
    return bytes(result)
