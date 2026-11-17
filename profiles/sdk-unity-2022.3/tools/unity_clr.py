"""First-party, bounded ECMA-335/PE reader shared by managed SDK scanners.

Only public binary-format machinery is shared. Each scanner constructs its own
image, table rows, method bodies and evidence from the immutable input bytes.
No dependency imports, runtime loading, or assembly execution occur here.
"""
import struct


class ManagedFormatError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ManagedFormatError(code)


def take(raw, offset, size):
    require(0 <= offset <= len(raw) and 0 <= size <= len(raw) - offset, 'CLR_BOUNDS')
    return raw[offset:offset + size]


def unpack(fmt, raw, offset):
    size = struct.calcsize(fmt)
    take(raw, offset, size)
    return struct.unpack_from(fmt, raw, offset)


def compressed(raw, offset):
    first = take(raw, offset, 1)[0]
    if first < 0x80:
        return first, offset + 1
    if first < 0xc0:
        return ((first & 0x3f) << 8) | take(raw, offset + 1, 1)[0], offset + 2
    require(first < 0xe0, 'CLR_COMPRESSED_INTEGER')
    rest = take(raw, offset + 1, 3)
    return ((first & 31) << 24) | int.from_bytes(rest, 'big'), offset + 4


def compress(value):
    require(0 <= value <= 0x1fffffff, 'CLR_COMPRESSED_INTEGER')
    if value < 0x80:
        return bytes([value])
    if value < 0x4000:
        return (value | 0x8000).to_bytes(2, 'big')
    return (value | 0xc0000000).to_bytes(4, 'big')


def align(value, alignment):
    require(alignment > 0 and alignment & (alignment - 1) == 0, 'PE_ALIGNMENT')
    return (value + alignment - 1) & -alignment


CODED = {
    'TypeDefOrRef': (2, [2, 1, 27]), 'HasConstant': (2, [4, 8, 23]),
    'HasCustomAttribute': (5, [6, 4, 1, 2, 8, 9, 10, 0, 14, 23, 20, 17, 26, 27, 32, 35, 38, 39, 40, 42, 44, 43]),
    'HasFieldMarshal': (1, [4, 8]), 'HasDeclSecurity': (2, [2, 6, 32]),
    'MemberRefParent': (3, [2, 1, 26, 6, 27]), 'HasSemantics': (1, [20, 23]),
    'MethodDefOrRef': (1, [6, 10]), 'MemberForwarded': (1, [4, 6]),
    'Implementation': (2, [38, 35, 39]), 'CustomAttributeType': (3, [None, None, 6, 10, None]),
    'ResolutionScope': (2, [0, 26, 35, 1]), 'TypeOrMethodDef': (1, [2, 6]),
}
SCHEMAS = {
    0: 'u2 s g g g', 1: 'ResolutionScope s s', 2: 'u4 s s TypeDefOrRef t4 t6',
    3: 't4', 4: 'u2 s b', 5: 't6', 6: 'u4 u2 u2 s b t8', 7: 't8',
    8: 'u2 u2 s', 9: 't2 TypeDefOrRef', 10: 'MemberRefParent s b',
    11: 'u2 HasConstant b', 12: 'HasCustomAttribute CustomAttributeType b',
    13: 'HasFieldMarshal b', 14: 'u2 HasDeclSecurity b', 15: 'u2 u4 t2',
    16: 'u4 t4', 17: 'b', 18: 't2 t20', 19: 't20', 20: 'u2 s TypeDefOrRef',
    21: 't2 t23', 22: 't23', 23: 'u2 s b', 24: 'u2 t6 HasSemantics',
    25: 't2 MethodDefOrRef MethodDefOrRef', 26: 's', 27: 'b',
    28: 'u2 MemberForwarded s t26', 29: 'u4 t4', 30: 'u4 u4', 31: 'u4',
    32: 'u4 u2 u2 u2 u2 u4 b s s', 33: 'u4', 34: 'u4 u4 u4',
    35: 'u2 u2 u2 u2 u4 b s s b', 36: 'u4 t35', 37: 'u4 u4 u4 t35',
    38: 'u4 s b', 39: 'u4 u4 s s Implementation', 40: 'u4 u4 s Implementation',
    41: 't2 t2', 42: 'u2 u2 TypeOrMethodDef s', 43: 'MethodDefOrRef b',
    44: 't42 TypeDefOrRef',
}

# Legal instructions, including reserved holes (which must NOT be zero-width).
ZERO = set(range(0x00, 0x0e)) | set(range(0x14, 0x1f)) | {0x25, 0x26, 0x2a}
ZERO |= set(range(0x46, 0x6f)) | {0x76, 0x7a, 0x8e} | set(range(0x82, 0x8c))
ZERO |= set(range(0x90, 0xa3)) | set(range(0xb3, 0xbb)) | {0xc3}
ZERO |= set(range(0xd1, 0xdd)) | {0xdc, 0xdf, 0xe0}
WIDTHS = {op: 0 for op in ZERO}
WIDTHS.update({op: 1 for op in set(range(0x0e, 0x14)) | {0x1f, 0xde} | set(range(0x2b, 0x38))})
WIDTHS.update({op: 4 for op in {0x20, 0x22, 0x27, 0x28, 0x29, 0x6f, 0x70, 0x71, 0x72, 0x73, 0x74, 0x75, 0x79,
                                         0x7b, 0x7c, 0x7d, 0x7e, 0x7f, 0x80, 0x81, 0x8c, 0x8d, 0x8f, 0xa3, 0xa4, 0xa5,
                                         0xc2, 0xc6, 0xd0, 0xdd} | set(range(0x38, 0x45))})
WIDTHS.update({0x21: 8, 0x23: 8})
WIDTHS.update({0xfe00 | op: 0 for op in {0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x0f, 0x11, 0x13, 0x14, 0x17, 0x18, 0x1a, 0x1d, 0x1e}})
WIDTHS.update({0xfe00 | op: 4 for op in {0x06, 0x07, 0x15, 0x16, 0x1c}})
WIDTHS.update({0xfe00 | op: 2 for op in range(0x09, 0x0f)})
WIDTHS.update({0xfe12: 1, 0xfe19: 1})


def instructions(code):
    """Decode every instruction and validate all branch destinations."""
    result = []
    p = 0
    while p < len(code):
        start = p
        op = code[p]
        p += 1
        if op == 0xfe:
            op = 0xfe00 | take(code, p, 1)[0]
            p += 1
        if op == 0x45:
            count = unpack('<I', code, p)[0]
            size = 4 + 4 * count
        else:
            require(op in WIDTHS, 'CLR_IL_OPCODE')
            size = WIDTHS[op]
        operand = take(code, p, size)
        p += size
        result.append({'offset': start, 'opcode': op, 'operand': operand, 'end': p})
    boundaries = {i['offset'] for i in result}
    for i in result:
        op, data, end = i['opcode'], i['operand'], i['end']
        targets = []
        if op in set(range(0x2b, 0x38)) | {0xde}:
            targets = [end + int.from_bytes(data, 'little', signed=True)]
        elif op in set(range(0x38, 0x45)) | {0xdd}:
            targets = [end + int.from_bytes(data, 'little', signed=True)]
        elif op == 0x45:
            targets = [end + x[0] for x in struct.iter_unpack('<i', data[4:])]
        require(all(t in boundaries for t in targets), 'CLR_IL_BRANCH_BOUNDARY')
        i['targets'] = targets
    return result


class Image:
    def __init__(self, raw):
        self.raw = raw
        self._type_stack = set()
        require(take(raw, 0, 2) == b'MZ', 'PE_DOS_SIGNATURE')
        self.pe = unpack('<I', raw, 60)[0]
        require(take(raw, self.pe, 4) == b'PE\0\0', 'PE_SIGNATURE')
        count = unpack('<H', raw, self.pe + 6)[0]
        optsize = unpack('<H', raw, self.pe + 20)[0]
        self.opt = self.pe + 24
        magic = unpack('<H', raw, self.opt)[0]
        require(magic in (0x10b, 0x20b), 'PE_OPTIONAL_HEADER')
        dd_offset = 96 if magic == 0x10b else 112
        require(optsize >= dd_offset + 15 * 8, 'PE_DIRECTORY_BOUNDS')
        self.dd = self.opt + dd_offset
        require(unpack('<I', raw, self.dd - 4)[0] >= 15, 'PE_CLI_DIRECTORY_MISSING')
        self.section_alignment, self.file_alignment = unpack('<II', raw, self.opt + 32)
        self.header_size = unpack('<I', raw, self.opt + 60)[0]
        self.section_table = self.opt + optsize
        take(raw, self.section_table, count * 40)
        self.sections = []
        for idx in range(count):
            off = self.section_table + idx * 40
            vs, va, rs, rp = unpack('<IIII', raw, off + 8)
            take(raw, rp, rs)
            self.sections.append({'header': off, 'virtual_size': vs, 'rva': va, 'size': rs, 'offset': rp})
        for idx, s in enumerate(self.sections):
            for other in self.sections[:idx]:
                require(not (s['offset'] < other['offset'] + other['size'] and other['offset'] < s['offset'] + s['size']), 'PE_SECTION_OVERLAP')
                require(not (s['rva'] < other['rva'] + max(other['virtual_size'], other['size']) and other['rva'] < s['rva'] + max(s['virtual_size'], s['size'])), 'PE_VIRTUAL_OVERLAP')
        cli_rva, cli_size = unpack('<II', raw, self.dd + 14 * 8)
        require(cli_size >= 72, 'CLR_HEADER_SIZE')
        self.cli = self.at(cli_rva, cli_size)
        require(unpack('<I', raw, self.cli)[0] >= 72, 'CLR_HEADER_SIZE')
        self.metadata_rva, self.metadata_size = unpack('<II', raw, self.cli + 8)
        self.root = self.at(self.metadata_rva, self.metadata_size)
        md = take(raw, self.root, self.metadata_size)
        require(take(md, 0, 4) == b'BSJB', 'CLR_METADATA_SIGNATURE')
        version_size = unpack('<I', md, 12)[0]
        p = align(16 + version_size, 4)
        take(md, 16, version_size)
        _, count = unpack('<HH', md, p)
        p += 4
        self.streams = {}
        self.stream_headers = {}
        for _ in range(count):
            header = p
            off, size = unpack('<II', md, p)
            p += 8
            end = md.find(b'\0', p, min(p + 32, len(md)))
            require(end >= p, 'CLR_STREAM_NAME')
            name = md[p:end].decode('ascii')
            p = align(end + 1, 4)
            require(name not in self.streams, 'CLR_DUPLICATE_STREAM')
            self.streams[name] = take(md, off, size)
            self.stream_headers[name] = (header, off, size)
        for name, (_, off, size) in self.stream_headers.items():
            require(off >= p and off % 4 == 0, 'CLR_STREAM_BOUNDARY')
            for other, (_, oo, os) in self.stream_headers.items():
                require(name == other or not (off < oo + os and oo < off + size), 'CLR_STREAM_OVERLAP')
        self.metadata_prefix = md[:p]
        require(('#~' in self.streams) != ('#-' in self.streams), 'CLR_TABLE_STREAM')
        self._tables(self.streams.get('#~', self.streams.get('#-')))
        self.assembly = self.string(self.row(32, 1)[7]) if self.rows.get(32) else ''
        self.method_owner, self.field_owner = {}, {}
        for rid, row in enumerate(self.rows.get(2, []), 1):
            nxt = self.rows[2][rid] if rid < len(self.rows[2]) else (0, 0, 0, 0, len(self.rows.get(4, [])) + 1, len(self.rows.get(6, [])) + 1)
            for col, table, owners in [(4, 4, self.field_owner), (5, 6, self.method_owner)]:
                require(1 <= row[col] <= nxt[col] <= len(self.rows.get(table, [])) + 1, 'CLR_MEMBER_RANGE')
                for member in range(row[col], nxt[col]):
                    owners[member] = rid
        self.nested = {r[0]: r[1] for r in self.rows.get(41, [])}
        require(not any(self.rows.get(t) for t in (3, 5, 7, 19, 22)), 'CLR_POINTER_TABLE_UNSUPPORTED')

    def at(self, rva, size=1):
        matches = [s for s in self.sections if s['rva'] <= rva and rva - s['rva'] + size <= s['size']]
        require(len(matches) == 1, 'PE_RVA_BOUNDS')
        return matches[0]['offset'] + rva - matches[0]['rva']

    def _tables(self, data):
        take(data, 0, 24)
        heaps = data[6]
        valid = unpack('<Q', data, 8)[0]
        p = 24
        counts = {}
        for t in range(64):
            if valid & (1 << t):
                counts[t] = unpack('<I', data, p)[0]
                p += 4
        self.rows = {}
        self.row_offsets = {}
        def width(kind):
            if kind in ('u2', 'u4'):
                return int(kind[1])
            if kind in ('s', 'g', 'b'):
                return 4 if heaps & {'s': 1, 'g': 2, 'b': 4}[kind] else 2
            if kind.startswith('t'):
                return 4 if counts.get(int(kind[1:]), 0) >= 65536 else 2
            bits, tables = CODED[kind]
            return 4 if max((counts.get(t, 0) for t in tables), default=0) >= 1 << (16 - bits) else 2
        for table, count in counts.items():
            require(table in SCHEMAS, 'CLR_METADATA_TABLE_UNSUPPORTED')
            widths = [width(k) for k in SCHEMAS[table].split()]
            size = sum(widths)
            take(data, p, size * count)
            fmt = '<' + ''.join('I' if w == 4 else 'H' for w in widths)
            self.rows[table] = [unpack(fmt, data, p + i * size) for i in range(count)]
            self.row_offsets[table] = [p + i * size for i in range(count)]
            p += size * count

    def row(self, table, rid):
        require(1 <= rid <= len(self.rows.get(table, [])), 'CLR_ROW_INDEX')
        return self.rows[table][rid - 1]

    def coded(self, kind, value):
        bits, tables = CODED[kind]
        tag, rid = value & ((1 << bits) - 1), value >> bits
        require(tag < len(tables) and tables[tag] is not None, 'CLR_CODED_INDEX')
        if rid:
            self.row(tables[tag], rid)
        return tables[tag], rid

    def string(self, offset):
        if not offset:
            return ''
        data = self.streams.get('#Strings', b'')
        require(0 < offset < len(data), 'CLR_STRING_INDEX')
        end = data.find(b'\0', offset)
        require(end >= 0, 'CLR_STRING_TERMINATOR')
        return data[offset:end].decode('utf-8')

    def blob(self, offset):
        if not offset:
            return b''
        data = self.streams.get('#Blob', b'')
        size, start = compressed(data, offset)
        return take(data, start, size)

    def user_strings(self):
        data = self.streams.get('#US', b'')
        if not data:
            return
        require(data[0] == 0, 'CLR_US_SENTINEL')
        p = 1
        while p < len(data):
            start = p
            length, p = compressed(data, p)
            if not length:
                continue  # stream alignment padding, not a string
            payload = take(data, p, length)
            require(length % 2 == 1 and payload[-1] in (0, 1), 'CLR_US_LAYOUT')
            yield start, payload[:-1].decode('utf-16-le', errors='surrogatepass')
            p += length

    def type_identity(self, table, rid, seen=()):
        if not rid:
            return None
        require((table, rid) not in seen, 'CLR_TYPE_CYCLE')
        require(len(seen) < 64, 'CLR_TYPE_DEPTH')
        seen = seen + ((table, rid),)
        row = self.row(table, rid)
        if table == 27:
            require((table, rid) not in self._type_stack and len(self._type_stack) < 64, 'CLR_TYPESPEC_CYCLE')
            self._type_stack.add((table, rid))
            try:
                return self.signature(self.blob(row[0]))[0]
            finally:
                self._type_stack.remove((table, rid))
        require(table in (1, 2), 'CLR_TYPE_REFERENCE')
        name = '.'.join(filter(None, (self.string(row[2]), self.string(row[1]))))
        assembly = self.assembly
        if table == 2 and rid in self.nested:
            parent = self.type_identity(2, self.nested[rid], seen)
            name = parent['name'] + '/' + self.string(row[1])
        if table == 1:
            scope, index = self.coded('ResolutionScope', row[0])
            if scope == 35 and index:
                assembly = self.string(self.row(35, index)[6])
            elif scope == 1 and index:
                parent = self.type_identity(1, index, seen)
                name, assembly = parent['name'] + '/' + self.string(row[1]), parent['assembly']
            elif scope == 26:
                assembly = ''  # netmodule resolution is not guessed
        return {'kind': 'named', 'name': name, 'assembly': assembly}

    def signature(self, data, pos=0, depth=0):
        require(depth < 64, 'CLR_SIGNATURE_DEPTH')
        et = take(data, pos, 1)[0]
        pos += 1
        primitives = {2: 'bool', 3: 'char', 4: 'sbyte', 5: 'byte', 6: 'int16', 7: 'uint16', 8: 'int32', 9: 'uint32', 10: 'int64', 11: 'uint64', 12: 'float32', 13: 'float64'}
        if et in primitives:
            return {'kind': 'primitive', 'name': primitives[et]}, pos
        if et in (1, 14):
            return {'kind': 'void' if et == 1 else 'string'}, pos
        if et in (17, 18):
            token, pos = compressed(data, pos)
            return self.type_identity(*self.coded('TypeDefOrRef', token)), pos
        if et in (0x1f, 0x20):
            _, pos = compressed(data, pos)
            return self.signature(data, pos, depth + 1)
        if et in (15, 16, 29):
            elem, pos = self.signature(data, pos, depth + 1)
            return {'kind': 'array' if et == 29 else 'unsupported', 'element': elem, **({'element_type': et} if et != 29 else {})}, pos
        if et == 21:
            base, pos = self.signature(data, pos, depth + 1)
            count, pos = compressed(data, pos)
            args = []
            for _ in range(count):
                arg, pos = self.signature(data, pos, depth + 1)
                args.append(arg)
            if base.get('name') == 'System.Collections.Generic.List`1' and len(args) == 1:
                return {'kind': 'list', 'element': args[0]}, pos
            return {'kind': 'unsupported', 'generic': base, 'arguments': args}, pos
        if et in (19, 30):
            index, pos = compressed(data, pos)
            return {'kind': 'unsupported', 'generic_parameter': index}, pos
        if et in (0x16, 0x18, 0x19, 0x1c):
            return {'kind': 'unsupported', 'element_type': et}, pos
        raise ManagedFormatError('CLR_SIGNATURE_UNSUPPORTED')

    def method_signature(self, token):
        table, rid = token >> 24, token & 0xffffff
        if table == 43:
            row = self.row(43, rid)
            t, r = self.coded('MethodDefOrRef', row[0])
            return self.method_signature(t << 24 | r)
        row = self.row(table, rid)
        require(table in (6, 10), 'CLR_METHOD_TOKEN')
        data = self.blob(row[4] if table == 6 else row[2])
        flags = take(data, 0, 1)[0]
        require(flags & 15 in (0, 5), 'CLR_CALL_CONVENTION_UNSUPPORTED')
        p = 1
        if flags & 0x10:
            _, p = compressed(data, p)
        count, p = compressed(data, p)
        result, p = self.signature(data, p)
        args = []
        for _ in range(count):
            if take(data, p, 1)[0] == 0x41:
                p += 1
            arg, p = self.signature(data, p)
            args.append(arg)
        require(p == len(data), 'CLR_METHOD_SIGNATURE_TRAILING')
        return {'instance': bool(flags & 0x20), 'arguments': args, 'return': result, 'vararg': flags & 15 == 5}

    def member(self, token):
        table, rid = token >> 24, token & 0xffffff
        row = self.row(table, rid)
        if table == 43:
            t, r = self.coded('MethodDefOrRef', row[0])
            return self.member(t << 24 | r)
        if table in (4, 6):
            owner = self.type_identity(2, (self.field_owner if table == 4 else self.method_owner)[rid])
            return owner, self.string(row[1] if table == 4 else row[3])
        require(table == 10, 'CLR_MEMBER_TOKEN')
        t, r = self.coded('MemberRefParent', row[0])
        return self.type_identity(t, r) if t in (1, 2, 27) else {'kind': 'unsupported'}, self.string(row[1])

    def body(self, rid):
        row = self.row(6, rid)
        if not row[0]:
            return None
        require(row[1] & 3 == 0, 'CLR_NATIVE_METHOD_UNSUPPORTED')
        start = self.at(row[0])
        first = self.raw[start]
        if first & 3 == 2:
            size, header, extra = first >> 2, 1, False
        else:
            require(first & 3 == 3, 'CLR_METHOD_HEADER')
            flags, _, size, _ = unpack('<HHII', self.raw, start)
            header = (flags >> 12) * 4
            require(header >= 12, 'CLR_METHOD_HEADER')
            extra = bool(flags & 8)
        self.at(row[0], header + size)
        code = take(self.raw, start + header, size)
        handlers = []
        if extra:
            p = align(start + header + size, 4)
            more = True
            while more:
                kind = take(self.raw, p, 1)[0]
                more = bool(kind & 0x80)
                require(kind & 0x3f == 1, 'CLR_METHOD_SECTION_UNSUPPORTED')
                fat = bool(kind & 0x40)
                length = int.from_bytes(take(self.raw, p + 1, 3), 'little') if fat else take(self.raw, p + 1, 1)[0]
                clause = 24 if fat else 12
                require(length >= 4 and (length - 4) % clause == 0, 'CLR_EXCEPTION_SECTION')
                section = take(self.raw, p + 4, length - 4)
                for off in range(0, len(section), clause):
                    values = unpack('<IIIIII' if fat else '<HHBHB I'.replace(' ', ''), section, off)
                    flags, ts, tl, hs, hl, value = values
                    require(ts + tl <= size and hs + hl <= size, 'CLR_EXCEPTION_BOUNDS')
                    handlers.append({'flags': flags, 'try_offset': ts, 'try_length': tl, 'handler_offset': hs, 'handler_length': hl, 'value': value})
                p = align(p + length, 4)
        return {'start': start + header, 'code': code, 'instructions': instructions(code), 'handlers': handlers}
