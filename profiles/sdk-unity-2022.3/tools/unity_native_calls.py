"""Bounded Windows x64 UI dependency evidence, not a general disassembler.

scan_native_dependencies(raw, relative_path, *, method='primary') returns
occurrences/dependencies/pending/proofs. Physical native_literal identity is
{rva,encoding,use_rva}; two LEA uses of one string remain separate occurrences.
Primary enumerates unwind functions; independent joins target-RVA use buckets
from a fresh PE parse. Shared bounded instruction decoder is explicitly disclosed.

Import/IAT, unwind boundaries and exact RIP operands establish possible display
paths. Byte-pattern candidates in undecodable functions are never patchable and
remain pending. Absence of a recognized API is NOT a non-display proof. This is
not universal native static completeness or runtime verification.
"""
from collections import defaultdict
import hashlib
import struct
from pathlib import PurePosixPath
from unity_clr import require, take, unpack
from unity_pe import PE, append_data, _checksum

# Windows x64 register argument indices: RCX=1,RDX=2,R8=8,R9=9.
# Only text-bearing arguments, never class names, resource IDs, flags or paths.
WINDOWS_SINKS = {
    'MessageBoxIndirectW': ([], False), 'MessageBoxIndirectA': ([], False),
    'TaskDialog': ([8, 9], False), 'TaskDialogIndirect': ([], False),
    'DialogBoxParamW': ([], False), 'DialogBoxParamA': ([], False),
    'CreateDialogParamW': ([], False), 'CreateDialogParamA': ([], False),
    'FormatMessageW': ([], False), 'FormatMessageA': ([], False),
    'MessageBoxW': ([2, 8], True), 'MessageBoxExW': ([2, 8], True),
    'SetWindowTextW': ([2], True), 'SetDlgItemTextW': ([8], True),
    'CreateWindowExW': ([8], True), 'DrawTextW': ([2], False),
    'DrawTextExW': ([2], False), 'TextOutW': ([9], False),
    'MessageBoxA': ([2, 8], False), 'SetWindowTextA': ([2], False),
    'SetDlgItemTextA': ([8], False), 'DrawTextA': ([2], False),
    'TextOutA': ([9], False), 'CreateWindowExA': ([8], False),
}


def _cstring(pe, rva, encoding, limit=16384):
    section = next((s for s in pe.sections if s['rva'] <= rva < s['rva'] + s['size']), None)
    require(section is not None, 'NATIVE_STRING_RVA')
    flags = unpack('<I', pe.raw, section['header'] + 36)[0]
    require(not flags & (0x20000000 | 0x80000000), 'NATIVE_STRING_NOT_READONLY_DATA')
    start = pe.at(rva)
    end = min(section['offset'] + section['size'], start + limit)
    width = 2 if encoding == 'utf-16-le' else 1
    require(width == 1 or rva % 2 == 0, 'NATIVE_STRING_ALIGNMENT')
    pos = start
    while pos + width <= end:
        if pe.raw[pos:pos + width] == bytes(width):
            return pe.raw[start:pos].decode(encoding, errors='surrogatepass')
        pos += width
    raise ValueError('NATIVE_STRING_TERMINATOR')


def _ascii(pe, rva):
    start = pe.at(rva)
    end = pe.raw.find(b'\0', start, min(start + 1024, len(pe.raw)))
    require(end >= start, 'NATIVE_IMPORT_STRING')
    return pe.raw[start:end].decode('ascii')


def _imports(pe):
    result = {}
    if pe.directory_count <= 1:
        return result
    rva, size = pe.directory(1)
    if not rva:
        return result
    start = pe.at(rva, size)
    require(size >= 20, 'NATIVE_IMPORT_BOUNDS')
    terminated = False
    for offset in range(start, start + size - 19, 20):
        original, stamp, forward, name_rva, iat = unpack('<IIIII', pe.raw, offset)
        if not any((original, stamp, forward, name_rva, iat)):
            terminated = True
            break
        library = _ascii(pe, name_rva).lower()
        thunk = original or iat
        index = 0
        while True:
            value = unpack('<Q', pe.raw, pe.at(thunk + index * 8, 8))[0]
            if not value:
                break
            name = None if value & (1 << 63) else _ascii(pe, value + 2)
            result[iat + index * 8] = {'library': library, 'member': name, 'ordinal': value & 65535 if name is None else None}
            index += 1
            require(index <= len(pe.raw) // 8, 'NATIVE_IMPORT_COUNT')
    require(terminated, 'NATIVE_IMPORT_TERMINATOR')
    return result


def _decode(code):
    """Conservative x64 boundary decoder; unknown/VEX forms stop, never guess."""
    result, p = [], 0
    while p < len(code):
        start, rex, prefix66, prefix67 = p, 0, False, False
        while p < len(code) and (code[p] in (0x66, 0x67, 0xf0, 0xf2, 0xf3, 0x2e, 0x3e, 0x26, 0x36, 0x64, 0x65) or 0x40 <= code[p] <= 0x4f):
            require(not rex or 0x40 <= code[p] <= 0x4f, 'NATIVE_PREFIX_ORDER_UNSUPPORTED')
            prefix66 |= code[p] == 0x66
            prefix67 |= code[p] == 0x67
            if 0x40 <= code[p] <= 0x4f:
                rex = code[p]
            p += 1
            require(p - start < 15, 'NATIVE_INSTRUCTION_LENGTH')
        op = take(code, p, 1)[0]
        p += 1
        modrm, displacement, displacement_pos, immediate = None, None, None, 0
        if op == 0x0f:
            second = take(code, p, 1)[0]
            p += 1
            op = 0x0f00 | second
            if 0x80 <= second <= 0x8f:
                immediate = 4
            elif second in (0x1f, 0xaf, 0xb6, 0xb7, 0xbe, 0xbf) or 0x40 <= second <= 0x4f or 0x90 <= second <= 0x9f or second in (0x10, 0x11, 0x28, 0x29, 0x2e, 0x2f, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5c, 0x5e, 0x6e, 0x6f, 0x7e, 0x7f, 0xef):
                modrm = take(code, p, 1)[0]
            else:
                raise ValueError('NATIVE_OPCODE_UNSUPPORTED')
        elif op in (0x90, 0x98, 0x99, 0x9c, 0x9d, 0xc3, 0xcc, 0xf4, 0xfc, 0xfd) or 0x50 <= op <= 0x5f:
            pass
        elif 0x70 <= op <= 0x7f or op in (0xeb, 0x6a):
            immediate = 1
        elif op in (0xe8, 0xe9, 0x68):
            immediate = 4
        elif op == 0xc2:
            immediate = 2
        elif 0xb8 <= op <= 0xbf:
            immediate = 8 if rex & 8 else 2 if prefix66 else 4
        elif op in (0x01, 0x03, 0x09, 0x0b, 0x21, 0x23, 0x29, 0x2b, 0x31, 0x33, 0x39, 0x3b, 0x63, 0x69, 0x6b, 0x80, 0x81, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89, 0x8a, 0x8b, 0x8d, 0x8f, 0xc0, 0xc1, 0xc6, 0xc7, 0xd0, 0xd1, 0xd2, 0xd3, 0xf6, 0xf7, 0xfe, 0xff):
            modrm = take(code, p, 1)[0]
            if op in (0x6b, 0x80, 0x83, 0xc0, 0xc1, 0xc6):
                immediate = 1
            elif op in (0x69, 0x81, 0xc7):
                immediate = 2 if prefix66 else 4
            elif op in (0xf6, 0xf7) and (modrm >> 3) & 7 == 0:
                immediate = 1 if op == 0xf6 else 2 if prefix66 else 4
        else:
            raise ValueError('NATIVE_OPCODE_UNSUPPORTED')
        if modrm is not None:
            require(op != 0x8d or modrm >> 6 != 3, 'NATIVE_INVALID_LEA')
            p += 1
            mode, rm = modrm >> 6, modrm & 7
            if mode != 3 and rm == 4:
                sib = take(code, p, 1)[0]
                p += 1
                if mode == 0 and sib & 7 == 5:
                    p += 4
            elif mode == 0 and rm == 5:
                displacement_pos = p
                displacement = unpack('<i', code, p)[0]
                p += 4
            if mode == 1:
                p += 1
            elif mode == 2:
                p += 4
        p += immediate
        take(code, start, p - start)
        require(p - start <= 15, 'NATIVE_INSTRUCTION_LENGTH')
        result.append({'offset': start, 'end': p, 'opcode': op, 'modrm': modrm, 'rex': rex,
                       'displacement': displacement, 'displacement_pos': displacement_pos, 'address_override': prefix67})
    return result


def _references(code, begin):
    """Candidate nominations only; decoded boundaries are a separate proof."""
    refs, calls, direct = [], [], []
    for p in range(len(code)):
        if p + 7 <= len(code) and code[p] in (0x48, 0x4c) and code[p + 1] == 0x8d and code[p + 2] & 0xc7 == 5:
            refs.append({'use_rva': begin + p, 'rva': begin + p + 7 + unpack('<i', code, p + 3)[0],
                         'register': ((code[p + 2] >> 3) & 7) + (8 if code[p] & 4 else 0)})
        if p + 6 <= len(code) and code[p:p + 2] in (b'\xff\x15', b'\xff\x25'):
            calls.append((begin + p, begin + p + 6 + unpack('<i', code, p + 2)[0]))
        if p + 5 <= len(code) and code[p] == 0xe8:
            direct.append((begin + p, begin + p + 5 + unpack('<i', code, p + 1)[0]))
    return refs, calls, direct


def _startup_functions(pe, functions, imports):
    entry = unpack('<I', pe.raw, pe.opt + 16)[0]
    # No TLS callback scope is claimed when a TLS directory is present.
    if pe.directory_count > 9 and any(pe.directory(9)):
        return None
    if not entry:
        return set()
    ordered = sorted(functions)
    from bisect import bisect_right
    def function_at(rva):
        pos = bisect_right(ordered, rva) - 1
        if pos >= 0 and rva < functions[ordered[pos]]['end']:
            return ordered[pos]

    root = function_at(entry)
    if root is None:
        return None
    seen, todo = {root}, [root]
    while todo:
        begin = todo.pop()
        function = functions[begin]
        if not function['complete']:
            return None
        for ins in function['decoded']:
            if (ins['opcode'] == 0xff and (ins['modrm'] >> 3) & 7 in (2, 4)
                    and (ins['modrm'] not in (0x15, 0x25) or ins['address_override']
                         or begin + ins['end'] + ins['displacement'] not in imports)):
                return None
            if ins['opcode'] not in (0xe8, 0xe9, 0xeb):
                continue
            end = ins['end']
            size = 1 if ins['opcode'] == 0xeb else 4
            start = pe.at(begin + end - size, size)
            destination = begin + end + int.from_bytes(pe.raw[start:start + size], 'little', signed=True)
            target = function_at(destination)
            if target is None:
                # Unknown thunk/leaf code is not proof of startup separation.
                return None
            if target not in seen:
                seen.add(target)
                todo.append(target)
    return seen


def scan_native_dependencies(raw: bytes, relative_path: str, *, method='primary') -> dict:
    require(method in ('primary', 'independent'), 'NATIVE_SCAN_METHOD')
    relative = PurePosixPath(relative_path.replace('\\', '/'))
    require(not relative.is_absolute() and '..' not in relative.parts, 'NATIVE_RELATIVE_PATH')
    pe = PE(raw)
    result = {'occurrences': [], 'dependencies': [], 'pending': [], 'proofs': {
        'sha256': hashlib.sha256(raw).hexdigest(), 'method': method, 'shared_decoder': 'first_party_bounded_x64',
        'enumeration': 'unwind_function_walk' if method == 'primary' else 'target_rva_use_join',
        'static_complete': False, 'runtime_verified': False}}
    if pe.directory_count > 14:
        cli_rva, cli_size = pe.directory(14)
        if cli_rva and cli_size >= 72 and unpack('<I', raw, pe.at(cli_rva, 72) + 16)[0] & 1:
            result['proofs']['scope'] = 'cli_il_only_no_native_code_scan'
            return result
    if unpack('<H', raw, pe.pe + 4)[0] != 0x8664 or unpack('<H', raw, pe.opt)[0] != 0x20b:
        result['pending'].append({'reason': 'native_architecture_unsupported', 'scope': 'native_dependencies'})
        return result
    imports = _imports(pe)
    if pe.directory_count > 13 and any(pe.directory(13)):
        result['pending'].append({'reason': 'native_delay_imports_unresolved', 'scope': 'native_dependencies'})
    known = {iat: rec for iat, rec in imports.items() if rec['library'] in ('user32.dll', 'comctl32.dll', 'kernel32.dll', 'kernelbase.dll', 'api-ms-win-ntuser-window-l1-1-0.dll') and rec['member'] in WINDOWS_SINKS}
    lookups = {iat for iat, rec in imports.items() if rec['library'] in ('kernel32.dll', 'kernelbase.dll') and rec['member'] == 'GetProcAddress'}
    api_identifiers = set()
    rva, size = pe.directory(3) if pe.directory_count > 3 else (0, 0)
    if not rva:
        result['pending'].append({'reason': 'native_unwind_boundaries_missing', 'scope': 'native_dependencies'})
        return result
    require(size % 12 == 0, 'NATIVE_UNWIND_TABLE')
    functions, sink_functions = {}, set()
    table = take(raw, pe.at(rva, size), size)
    for begin, end, unwind in struct.iter_unpack('<III', table):
        require(begin < end and unwind, 'NATIVE_UNWIND_ENTRY')
        code = take(raw, pe.at(begin, end - begin), end - begin)
        section = next(s for s in pe.sections if s['rva'] <= begin < s['rva'] + s['size'])
        require(unpack('<I', raw, section['header'] + 36)[0] & 0x20000000, 'NATIVE_UNWIND_NOT_CODE')
        info = take(raw, pe.at(unwind, 4), 4)
        require(info[0] & 7 in (1, 2), 'NATIVE_UNWIND_VERSION')
        take(raw, pe.at(unwind, 4 + info[2] * 2), 4 + info[2] * 2)
        require(begin not in functions, 'NATIVE_UNWIND_DUPLICATE')
        refs, calls, direct = _references(code, begin)
        try:
            decoded = _decode(code)
            complete = True
        except ValueError:
            decoded, complete = [], False
        functions[begin] = {'end': end, 'refs': refs, 'calls': calls, 'direct': direct, 'decoded': decoded, 'complete': complete}
        # A decoded RIP-relative pointer load binds a possible display import,
        # even when a later register-indirect call has no FF15/FF25 operand.
        # Do not claim argument/register propagation or a writable projection.
        for ins in decoded:
            if (ins['opcode'] == 0x8b and ins['rex'] & 8 and ins['modrm'] & 0xc7 == 5
                    and not ins['address_override']):
                iat = begin + ins['end'] + ins['displacement']
                if iat in known:
                    sink_functions.add(begin)
                    result['dependencies'].append({'kind': 'native_possible_display_import_reference',
                        'function_rva': begin, 'use_rva': begin + ins['offset'], 'iat_rva': iat,
                        'import': known[iat], 'display_dependency': True, 'operand_verified': True})
                    result['pending'].append({'reason': 'native_carried_display_import_unresolved',
                        'scope': 'display_dependency', 'function_rva': begin,
                        'use_rva': begin + ins['offset'], 'iat_rva': iat})
        for call, iat in calls:
            if iat in known:
                sink_functions.add(begin)
                result['dependencies'].append({'kind': 'native_display_import_call', 'function_rva': begin, 'call_rva': call, 'iat_rva': iat, 'import': known[iat]})
            if iat in lookups:
                boundaries = {begin + ins['offset'] for ins in decoded}
                matched = False
                for ref in refs:
                    if ref['register'] != 2 or ref['use_rva'] + 7 != call or call not in boundaries or ref['use_rva'] not in boundaries:
                        continue
                    try:
                        api = _cstring(pe, ref['rva'], 'utf-8')
                    except (ValueError, UnicodeError):
                        continue
                    if api in WINDOWS_SINKS:
                        matched = True
                        api_identifiers.add(ref['use_rva'])
                        sink_functions.add(begin)
                        result['dependencies'].append({'kind': 'native_dynamic_display_api_lookup', 'function_rva': begin, 'call_rva': call, 'use_rva': ref['use_rva'], 'member': api})
                        result['pending'].append({'reason': 'native_dynamic_display_invocation_unresolved', 'scope': 'display_dependency', 'function_rva': begin, 'call_rva': call})
                if not matched:
                    result['pending'].append({'reason': 'native_dynamic_lookup_unresolved', 'scope': 'native_dependencies', 'function_rva': begin, 'call_rva': call})
    # Backwards may-call closure; exact decoder boundaries are checked separately.
    connected = set(sink_functions)
    changed = True
    while changed:
        changed = False
        for begin, function in functions.items():
            if begin not in connected and any(target in connected for _, target in function['direct']):
                connected.add(begin); changed = True
    candidate_uses = []
    for begin in sorted(connected):
        function = functions[begin]
        boundary = {begin + ins['offset']: ins for ins in function['decoded']}
        bound = {}
        straight = True
        registers = {}
        refs = {ref['use_rva']: ref for ref in function['refs']}
        calls = dict(function['calls'])
        for use, ins in sorted(boundary.items()):
            if ins['opcode'] in (0xc3, 0xc2, 0xe9, 0xeb) or 0x70 <= ins['opcode'] <= 0x7f or 0x0f80 <= ins['opcode'] <= 0x0f8f:
                straight = False
            if straight and use in refs and ins['opcode'] == 0x8d and ins['end'] - ins['offset'] == 7 and not ins['address_override']:
                registers[refs[use]['register']] = refs[use]
            elif use in calls and calls[use] in known and ins['opcode'] == 0xff and ins['modrm'] in (0x15, 0x25):
                api = known[calls[use]]['member']
                slots, patchable = WINDOWS_SINKS[api]
                if not slots:
                    result['pending'].append({'reason': 'native_display_structure_or_output_unresolved', 'scope': 'display_dependency', 'call_rva': use})
                for register in slots:
                    if register in registers:
                        ref = registers[register]
                        bound[ref['use_rva']] = {'api': api, 'call_rva': use, 'patchable': patchable and api.endswith('W')}
                    else:
                        result['pending'].append({'reason': 'native_display_argument_unresolved', 'scope': 'display_dependency', 'call_rva': use, 'register': register})
                registers.clear()
            elif ins['opcode'] in (0x90, 0x0f1f):
                pass
            elif ins['opcode'] in (0x8b, 0x8a, 0x63):
                registers.pop(((ins['modrm'] >> 3) & 7) + (8 if ins['rex'] & 4 else 0), None)
            elif 0xb8 <= ins['opcode'] <= 0xbf:
                registers.pop(ins['opcode'] - 0xb8 + (8 if ins['rex'] & 1 else 0), None)
            else:
                registers.clear()
        for use, ins in boundary.items():
            if ins['opcode'] == 0xff and (ins['modrm'] >> 3) & 7 in (2, 4) and calls.get(use) not in known:
                result['pending'].append({'reason': 'native_indirect_call_unresolved', 'scope': 'display_dependency', 'function_rva': begin, 'call_rva': use})
        if not function['complete'] or any(call not in boundary for call, iat in function['calls'] if iat in known):
            result['pending'].append({'reason': 'native_instruction_boundaries_unverified', 'scope': 'display_dependency', 'function_rva': begin})
        for ref in function['refs']:
            candidate_uses.append((begin, ref, bound.get(ref['use_rva'])))
    if method == 'primary':
        uses = candidate_uses
    else:
        # Join raw physical use records by target before any text extraction or
        # occurrence/classification creation; never consume primary inventory.
        by_target = defaultdict(list)
        for begin, ref, proof in candidate_uses:
            by_target[ref['rva']].append((begin, ref, proof))
        uses = [use for target in sorted(by_target) for use in by_target[target]]
    nominations = []
    for begin, ref, proof in uses:
        if proof:
            encoding = 'utf-16-le' if proof['api'].endswith('W') else 'cp1252'
        else:
            try:
                sample = take(raw, pe.at(ref['rva'], 2), 2)
                encoding = 'utf-16-le' if ref['rva'] % 2 == 0 and sample[1] == 0 else 'utf-8'
            except ValueError:
                continue
        try:
            source = _cstring(pe, ref['rva'], encoding)
        except (ValueError, UnicodeError):
            continue
        occurrence = {'kind': 'native_literal', 'file': str(relative), 'location': {'rva': ref['rva'], 'encoding': encoding, 'use_rva': ref['use_rva']},
                      'source': source, 'classification': 'display' if proof else 'uncertain', 'display_dependency': True,
                      'reason': 'bound_native_display_argument' if proof else 'possible_native_display_dependency',
                      'projection_required': not (proof and proof['patchable']),
                      'proof': {'function_rva': begin, 'use_rva': ref['use_rva'], 'operand_verified': bool(proof), 'consumer': proof}}
        occurrence['display_projection_required'] = occurrence['projection_required']
        if proof and proof['api'].endswith('A'):
            occurrence['proof']['encoding_status'] = 'unverified_ansi_codepage_cp1252_interpretation'
            result['pending'].append({'reason': 'native_ansi_codepage_unverified', 'scope': 'display_dependency', 'location': occurrence['location']})
        if ref['use_rva'] in api_identifiers:
            occurrence['classification'] = 'non_display'
            occurrence['display_dependency'] = False
            occurrence['reason'] = 'structural_native_api_identifier'
        nominations.append(occurrence)
        if ref['use_rva'] not in api_identifiers and (not proof or not proof['patchable']):
            result['pending'].append({'reason': 'native_display_projection_required', 'scope': 'display_dependency', 'location': occurrence['location']})
    result['occurrences'] = sorted(nominations, key=lambda row: (row['location']['rva'], row['location']['encoding'], row['location']['use_rva']))
    result['proofs']['functions'] = {str(begin): {'end_rva': fn['end'], 'decoded': fn['complete']} for begin, fn in functions.items() if begin in connected}
    startup = _startup_functions(pe, functions, imports)
    result['proofs']['startup_functions'] = sorted(startup) if startup is not None else None
    return result


def patch_native_literals(raw, replacements):
    """Retarget selected, decoded RIP-relative UI argument uses; preserve others.

    Only exact null-terminated UTF-16 Windows UI arguments are patchable. Counted
    strings, ANSI/codepage ambiguity and possible/unverified paths require a
    separately approved projection, and raise NATIVE_PROJECTION_REQUIRED here.
    """
    if not replacements:
        return raw
    inventory = scan_native_dependencies(raw, 'synthetic-binding.dll')
    startup_functions = inventory['proofs'].get('startup_functions')
    startup = set(startup_functions) if startup_functions is not None else None
    rows = {(o['location']['rva'], o['location']['encoding'], o['location']['use_rva']): o for o in inventory['occurrences']}
    payload, writes, seen = bytearray(), [], set()
    for replacement in replacements:
        require(isinstance(replacement, dict) and isinstance(replacement.get('location'), dict), 'NATIVE_REPLACEMENT_BINDING')
        loc = replacement.get('location', {})
        key = (loc.get('rva'), loc.get('encoding'), loc.get('use_rva'))
        require(key in rows and key not in seen, 'NATIVE_REPLACEMENT_BINDING')
        seen.add(key)
        row = rows[key]
        require(not row['projection_required'], 'NATIVE_PROJECTION_REQUIRED')
        require(startup is not None and row['proof']['function_rva'] not in startup,
                'NATIVE_STARTUP_LITERAL_WRITE_FORBIDDEN')
        require(isinstance(replacement.get('source'), str) and row['source'] == replacement['source'], 'NATIVE_REPLACEMENT_SOURCE_MISMATCH')
        target = replacement.get('target')
        require(isinstance(target, str) and '\0' not in target, 'NATIVE_TARGET_TYPE')
        offset = len(payload)
        payload.extend(target.encode('utf-16-le', errors='surrogatepass') + b'\0\0')
        writes.append((key[2], offset))
    result, rva = append_data(raw, bytes(payload))
    pe = PE(result)
    result = bytearray(result)
    for use, offset in writes:
        displacement = rva + offset - (use + 7)
        require(-(1 << 31) <= displacement < 1 << 31, 'NATIVE_REFERENCE_RANGE')
        pos = pe.at(use, 7)
        require(result[pos] in (0x48, 0x4c) and result[pos + 1] == 0x8d and result[pos + 2] & 0xc7 == 5, 'NATIVE_REPLACEMENT_BINDING')
        struct.pack_into('<i', result, pos + 3, displacement)
    struct.pack_into('<I', result, pe.opt + 64, _checksum(result, pe.opt + 64))
    return bytes(result)
