"""Bounded x64 resource-to-UI evidence; resource membership is not visibility.

Only decoded straight-line LoadStringW/FormatMessageW output buffers reaching
an exact Unicode UI argument are bound. Imports, IDs, module identity and buffer
identity are required. This is may-display evidence, not runtime reachability.
"""
import struct

from unity_clr import require, take, unpack
from unity_native_calls import WINDOWS_SINKS, _cstring, _decode, _imports, _references
from unity_pe import PE

_LOADERS = {'LoadStringW': ('user32.dll',),
            'FormatMessageW': ('kernel32.dll', 'kernelbase.dll')}
_UNSUPPORTED_LOADERS = {'LoadStringA': ('user32.dll',),
                        'FormatMessageA': ('kernel32.dll', 'kernelbase.dll')}
_UI_LIBRARIES = {'user32.dll', 'comctl32.dll', 'api-ms-win-ntuser-window-l1-1-0.dll'}


def scan_resource_consumers(raw, relative_path, *, method='primary'):
    require(method in ('primary', 'independent'), 'NATIVE_SCAN_METHOD')
    pe = PE(raw)
    result = {'bindings': [], 'pending': [], 'dependencies': []}
    if unpack('<H', raw, pe.pe + 4)[0] != 0x8664 or unpack('<H', raw, pe.opt)[0] != 0x20b:
        return result  # The native dependency scanner retains unsupported scope.
    imports = _imports(pe)
    loaders = {iat: rec['member'] for iat, rec in imports.items()
               if rec['member'] in _LOADERS and rec['library'] in _LOADERS[rec['member']]}
    unsupported = {iat: rec['member'] for iat, rec in imports.items()
                   if rec['member'] in _UNSUPPORTED_LOADERS
                   and rec['library'] in _UNSUPPORTED_LOADERS[rec['member']]}
    if not loaders and not unsupported:
        return result
    sinks = {iat: rec['member'] for iat, rec in imports.items()
             if rec['library'] in _UI_LIBRARIES and rec['member'] in WINDOWS_SINKS
             and rec['member'] not in ('FormatMessageW', 'FormatMessageA')}
    modules = {iat: rec['member'] for iat, rec in imports.items()
               if rec['library'] in ('kernel32.dll', 'kernelbase.dll')
               and rec['member'] in ('GetModuleHandleW', 'GetModuleHandleA')}
    is_executable = not unpack('<H', raw, pe.pe + 22)[0] & 0x2000
    module_name = relative_path.replace('\\', '/').rsplit('/', 1)[-1].casefold()
    rva, size = pe.directory(3) if pe.directory_count > 3 else (0, 0)
    if not rva:
        # Import membership alone nominates no values. An unresolved loader and
        # UI import cannot establish a resource ID, module or buffer connection.
        if sinks:
            result['pending'].append({'reason': 'native_resource_consumer_boundaries_missing',
                                      'scope': 'display_dependency', 'file': relative_path})
        return result
    require(size % 12 == 0, 'NATIVE_UNWIND_TABLE')
    functions = list(struct.iter_unpack('<III', take(raw, pe.at(rva, size), size)))
    nominations, edges, connected = {}, {}, set()
    for begin, end, _ in functions:
        require(begin < end, 'NATIVE_UNWIND_ENTRY')
        _, calls, direct = _references(take(raw, pe.at(begin, end - begin), end - begin), begin)
        nominations[begin] = [(call, iat) for call, iat in calls if iat in loaders or iat in unsupported]
        edges[begin] = {target for _, target in direct}
        if any(iat in sinks for _, iat in calls):
            connected.add(begin)
    # Graph adjacency nominates unresolved scope only; it never proves a value
    # flows between functions or promotes any resource-table entries.
    changed = True
    while changed:
        changed = False
        for begin, targets in edges.items():
            if begin in connected:
                additions = (targets & edges.keys()) - connected
                if additions:
                    connected.update(additions)
                    changed = True
            elif targets & connected:
                connected.add(begin)
                changed = True
    if method == 'independent':
        functions.reverse()
    for begin, end, unwind in functions:
        require(begin < end and unwind, 'NATIVE_UNWIND_ENTRY')
        section = next(s for s in pe.sections if s['rva'] <= begin < s['rva'] + s['size'])
        require(unpack('<I', raw, section['header'] + 36)[0] & 0x20000000, 'NATIVE_UNWIND_NOT_CODE')
        info = take(raw, pe.at(unwind, 4), 4)
        require(info[0] & 7 in (1, 2), 'NATIVE_UNWIND_VERSION')
        take(raw, pe.at(unwind, 4 + info[2] * 2), 4 + info[2] * 2)
        code = take(raw, pe.at(begin, end - begin), end - begin)
        relevant_calls = nominations[begin]
        if not relevant_calls or begin not in connected:
            continue
        try:
            instructions = _decode(code)
        except ValueError:
            for call, iat in relevant_calls:
                result['pending'].append({'reason': 'native_resource_consumer_decode_unresolved',
                                          'scope': 'display_dependency', 'function_rva': begin,
                                          'load_rva': call, 'loader': imports[iat]['member']})
            continue
        registers, stack, buffers = {}, {}, {}
        resource_loads, resolved_loads = [], set()
        for ins in instructions:
            op, modrm, rex = ins['opcode'], ins['modrm'], ins['rex']
            use = begin + ins['offset']
            instruction = code[ins['offset']:ins['end']]
            if instruction[0] in (0x66, 0x67, 0xf0, 0xf2, 0xf3, 0x2e, 0x3e, 0x26, 0x36, 0x64, 0x65):
                registers.clear()
                stack.clear()
                buffers.clear()
                continue
            if op in (0xc3, 0xc2):
                break
            if op in (0xe9, 0xeb) or 0x70 <= op <= 0x7f or 0x0f80 <= op <= 0x0f8f:
                break  # Never infer flow through a branch or merge.
            if op in (0x90, 0x0f1f):
                continue
            if 0xb8 <= op <= 0xbf and not instruction.startswith(b'\x66'):
                register = op - 0xb8 + (8 if rex & 1 else 0)
                width = 8 if rex & 8 else 4
                registers[register] = ('integer', int.from_bytes(instruction[-width:], 'little'))
                continue
            if op in (0x31, 0x33) and modrm >> 6 == 3:
                left = ((modrm >> 3) & 7) + (8 if rex & 4 else 0)
                right = (modrm & 7) + (8 if rex & 1 else 0)
                destination = left if op == 0x33 else right
                registers.pop(destination, None)
                if left == right:
                    registers[destination] = ('integer', 0)
                continue
            if op == 0x8d and rex & 8 and modrm & 0xc7 == 5 and not ins['address_override']:
                target = begin + ins['end'] + ins['displacement']
                registers[((modrm >> 3) & 7) + (8 if rex & 4 else 0)] = ('pointer', target)
                continue
            if op in (0x89, 0x8b) and modrm >> 6 == 3 and rex & 8:
                reg = ((modrm >> 3) & 7) + (8 if rex & 4 else 0)
                rm = (modrm & 7) + (8 if rex & 1 else 0)
                destination, source = (rm, reg) if op == 0x89 else (reg, rm)
                if source not in registers:
                    registers.pop(destination, None)
                if source in registers:
                    registers[destination] = registers[source]
                continue
            # The fifth FormatMessageW argument is an exact outgoing RSP+0x20
            # slot, not an arbitrary memory reference or a guessed stack frame.
            if (op == 0x89 and rex & 8 and not rex & 1 and modrm & 0xc7 == 0x44
                    and not ins['address_override'] and instruction[-2:] == b'\x24\x20'):
                register = ((modrm >> 3) & 7) + (8 if rex & 4 else 0)
                stack[0x20] = registers.get(register)
                continue
            if op == 0xff and modrm in (0x15, 0x25) and not ins['address_override']:
                iat = begin + ins['end'] + ins['displacement']
                returned_module = False
                if iat in modules:
                    argument = registers.get(1)
                    returned_module = argument == ('integer', 0) and is_executable
                    if argument and argument[0] == 'pointer':
                        try:
                            encoding = 'utf-16-le' if modules[iat].endswith('W') else 'ascii'
                            returned_module = _cstring(pe, argument[1], encoding).casefold() == module_name
                        except (ValueError, UnicodeError):
                            pass
                elif iat in loaders:
                    api = loaders[iat]
                    module = registers.get(1 if api == 'LoadStringW' else 2)
                    local = bool(module and module[0] == 'module') or (api == 'FormatMessageW' and module == ('integer', 0) and is_executable)
                    identifier = registers.get(2 if api == 'LoadStringW' else 8)
                    output = registers.get(8) if api == 'LoadStringW' else stack.get(0x20)
                    flags = registers.get(1)
                    capacity = registers.get(9) if api == 'LoadStringW' else None
                    valid = local and identifier and identifier[0] == 'integer'
                    if api == 'LoadStringW':
                        valid = valid and capacity and capacity[0] == 'integer' and 0 < capacity[1] <= 0x7fffffff
                    else:
                        valid = valid and flags and flags[0] == 'integer' and flags[1] & 0x800 and not flags[1] & (0x100 | 0x400)
                    origin = {'resource_type': 6 if api == 'LoadStringW' else 11,
                              'resource_id': identifier[1] if identifier and identifier[0] == 'integer' else None,
                              'loader': api, 'load_rva': use, 'function_rva': begin,
                              'module_verified': bool(local), 'output_verified': False,
                              'module_call_rva': module[1] if module and module[0] == 'module' else None,
                              'system_fallback': bool(api == 'FormatMessageW' and flags and flags[0] == 'integer' and flags[1] & 0x1000)}
                    resource_loads.append(origin)
                    buffers.clear()  # Aliasing/reuse never leaves stale loader evidence.
                    if valid and output and output[0] == 'pointer':
                        section = next((s for s in pe.sections if s['rva'] <= output[1]
                                        and output[1] + 2 <= s['rva'] + s['size']), None)
                        if section is not None and unpack('<I', raw, section['header'] + 36)[0] & 0xa0000000 == 0x80000000:
                            origin['output_verified'] = True
                            buffers[output] = origin
                elif iat in sinks:
                    for register in WINDOWS_SINKS[sinks[iat]][0]:
                        origin = buffers.get(registers.get(register))
                        if origin and sinks[iat].endswith('W'):
                            binding = {**origin, 'consumer': sinks[iat], 'call_rva': use,
                                       'register': register, 'buffer_rva': registers[register][1]}
                            result['bindings'].append(binding)
                            result['dependencies'].append({'kind': 'native_resource_display_binding',
                                                           'display_dependency': True, **binding})
                            resolved_loads.add(origin['load_rva'])
                            if origin['system_fallback']:
                                result['pending'].append({'reason': 'native_system_message_fallback_unresolved',
                                                          'scope': 'display_dependency', 'call_rva': use})
                    buffers.clear()
                else:
                    buffers.clear()
                # Windows x64 calls clobber all volatile registers. Nonvolatile
                # register values are retained only through understood effects.
                registers = {reg: value for reg, value in registers.items() if reg in (3, 5, 6, 7, 12, 13, 14, 15)}
                if returned_module:
                    registers[0] = ('module', use)
                stack.clear()
                if modrm == 0x25:
                    break
                continue
            # Unknown effects may overwrite buffers or aliases. Clear, do not
            # extrapolate dataflow from mere same-function membership.
            registers.clear()
            stack.clear()
            buffers.clear()
        origins = {origin['load_rva']: origin for origin in resource_loads}
        for call, iat in relevant_calls:
            if call not in resolved_loads:
                result['pending'].append({'reason': 'native_resource_loader_unsupported' if iat in unsupported else 'native_resource_display_flow_unresolved',
                                          'scope': 'display_dependency', 'function_rva': begin,
                                          'load_rva': call, 'loader': imports[iat]['member'],
                                          **origins.get(call, {})})
    result['bindings'].sort(key=lambda row: (row['resource_type'], row['resource_id'], row['load_rva'], row['call_rva']))
    return result


def reconcile_resource_dependencies(resources, native):
    """Discharge only overlapping gaps covered by exact resource-use proofs.

    Unrelated registers, callsites and the native scanner's static-completeness
    limitation remain intact. No raw resource values participate in this join.
    """
    bindings = [edge for edge in resources['dependencies']
                if edge.get('kind') == 'native_resource_display_binding'
                and edge.get('module_verified') is True
                and edge.get('output_verified') is True]
    loader_calls = {(edge['function_rva'], edge['load_rva']) for edge in bindings}
    module_calls = {(edge['function_rva'], edge['module_call_rva']) for edge in bindings
                    if edge.get('module_call_rva') is not None}
    resolved_calls = loader_calls | module_calls
    format_calls = {edge['load_rva'] for edge in bindings if edge['loader'] == 'FormatMessageW'}
    arguments = {(edge['call_rva'], edge['register']) for edge in bindings}
    pending = []
    for gap in native['pending']:
        reason = gap.get('reason')
        if (reason == 'native_indirect_call_unresolved'
                and (gap.get('function_rva'), gap.get('call_rva')) in resolved_calls):
            continue
        if reason == 'native_display_structure_or_output_unresolved' and gap.get('call_rva') in format_calls:
            continue
        if reason == 'native_display_argument_unresolved' and (gap.get('call_rva'), gap.get('register')) in arguments:
            continue
        pending.append(gap)
    return {**native, 'pending': pending}
