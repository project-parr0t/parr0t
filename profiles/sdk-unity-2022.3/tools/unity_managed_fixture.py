"""Title-free real PE/CLI bytes for SDK preparation and regression tests.

build_fixture() includes four physical ldstr uses at (MethodDef token, IL offset):
(0x06000001,1), (0x06000001,11), (0x06000003,4), (0x06000003,11).
Sources: SYNTHETIC SOURCE for the first three, SYNTHETIC OTHER for the fourth.
The first, third and fourth flow into the exact UnityEngine.UI.Text.set_text
sink; the second is popped and stays uncertain. Method 2 reads a serialized
field into that sink. Embedded resource 0x28000001 includes two strings plus an
integer: source SYNTHETIC RESOURCE at entry 0, SYNTHETIC RESOURCE OTHER at entry
1; both remain uncertain without an actual ResourceManager display consumer.
"""
import struct
import unity_clr as c
import unity_managed as m
from unity_dotnet_resources import put_seven
def token(op, value):
    return bytes([op]) + struct.pack('<I', value)


def fixture(*, codes=None, strong=False, pe64=False, unsupported_field=False, resources=None, resource_flow=False, linked_resource=False,
            string_argument=False, external_helper_assembly=None, shared_resource_key=False, assembly_name='Synthetic'):
    strings = bytearray(b'\0')
    blobs = bytearray(b'\0')
    def s(value):
        offset = len(strings)
        strings.extend(value.encode('utf-8') + b'\0')
        return offset
    def b(value):
        offset = len(blobs)
        blobs.extend(c.compress(len(value)) + value)
        return offset
    us = b'\0' + m._us_entry('SYNTHETIC SOURCE') + m._us_entry('SYNTHETIC OTHER')
    second = 1 + len(m._us_entry('SYNTHETIC SOURCE'))
    if resource_flow:
        base_offset = len(us)
        us += m._us_entry('Synthetic')
        key_offset = len(us)
        us += m._us_entry('a')
        key_flow = token(0x72, 0x70000000 | key_offset)
        if shared_resource_key:
            key_flow += b'\x25' + token(0x80, 0x04000004)
        code = b'\x14' + token(0x72, 0x70000000 | base_offset) + b'\x14' + token(0x73, 0x0a000003)
        code += key_flow + token(0x6f, 0x0a000004) + token(0x6f, 0x0a000001)
        if shared_resource_key:
            code += b'\x14' + token(0x7e, 0x04000004) + token(0x6f, 0x0a000001)
        codes = [code + b'\x2a']
    if codes is None:
        codes = [b'\x14' + token(0x72, 0x70000001) + token(0x6f, 0x0a000001) + token(0x72, 0x70000001) + b'\x26\x2a',
                 b'\x14\x14' + token(0x7b, 0x04000001) + token(0x6f, 0x0a000001) + b'\x2a',
                 b'\x14\x02\x2c\x07' + token(0x72, 0x70000001) + b'\x2b\x05' + token(0x72, 0x70000000 | second) + token(0x6f, 0x0a000001) + b'\x2a']
    body_offsets = [0x280 + i * 0x80 for i in range(len(codes))]
    rows = {
        0: [(0, s('Synthetic.dll'), 1, 0, 0)],
        1: [(6, s('Text'), s('UnityEngine.UI')), (10, s('MonoBehaviour'), s('UnityEngine')), (10, s('SerializeField'), s('UnityEngine'))],
        2: [(0, s('<Module>'), 0, 0, 1, 1), (0x2001, s('Panel'), s('Synthetic'), 9, 1, 1)],
        4: [(6, s('caption'), b(b'\x06\x0e')), (1, s('hidden'), b(b'\x06\x0e')), (6, s('numbers'), b(b'\x06\x1d\x08')), (0x16, s('shared'), b(b'\x06\x0e'))],
        6: [(0x2000 + off - 0x200, 0, 0x16, s('Method' + str(i)), b(b'\x00\x01\x01\x02' if i == 2 else b'\x00\x00\x01'), 1) for i, off in enumerate(body_offsets)],
        10: [(9, s('set_text'), b(b'\x20\x01\x01\x0e')), (25, s('.ctor'), b(b'\x20\x00\x01'))],
        12: [(65, 19, b(b'\x01\x00\x00\x00'))],
        32: [(0x8004, 1, 0, 0, 0, int(strong), b(b'KEY') if strong else 0, s(assembly_name), 0)],
        35: [(1, 0, 0, 0, 0, 0, s('UnityEngine.UI'), 0, 0), (1, 0, 0, 0, 0, 0, s('UnityEngine.CoreModule'), 0, 0)],
    }
    if string_argument:
        row = rows[6][0]
        rows[6][0] = (*row[:4], b(b'\x00\x01\x01\x0e'), row[5])
    if external_helper_assembly:
        rows[35].append((1, 0, 0, 0, 0, 0, s(external_helper_assembly), 0, 0))
        rows[1].append((14, s('Panel'), s('Synthetic')))
        rows[10].append((33, s('Method0'), b(b'\x00\x01\x01\x0e')))
    if resource_flow:
        rows[35].append((1, 0, 0, 0, 0, 0, s('mscorlib'), 0, 0))
        rows[1].extend([(14, s('ResourceManager'), s('System.Resources')), (14, s('Assembly'), s('System.Reflection'))])
        rows[10].extend([(33, s('.ctor'), b(b'\x20\x02\x01\x0e\x12\x15')), (33, s('GetString'), b(b'\x20\x01\x0e\x0e'))])
    if unsupported_field:
        rows[4].append((6, s('opaque'), b(b'\x06\x1c')))
    if resources is not None:
        rows[40] = [(0, 1, s('Synthetic.resources'), 0)]
        if linked_resource:
            rows[38] = [(0, s('Synthetic.linked'), 0)]
            rows[40][0] = (0, 1, rows[40][0][2], 4)
    formats = {0: '<HHHHH', 1: '<HHH', 2: '<IHHHHH', 4: '<HHH', 6: '<IHHHHH', 10: '<HHH', 12: '<HHH', 32: '<IHHHHIHHH', 35: '<HHHHIHHHH', 38: '<IHH', 40: '<IIHH'}
    valid = sum(1 << table for table in rows)
    tables = bytearray(struct.pack('<IBBBBQQ', 0, 2, 0, 0, 1, valid, 0))
    for table in sorted(rows):
        tables.extend(struct.pack('<I', len(rows[table])))
    for table in sorted(rows):
        for row in rows[table]:
            tables.extend(struct.pack(formats[table], *row))
    streams = [('#~', tables), ('#Strings', strings), ('#US', us), ('#Blob', blobs), ('#GUID', bytes(16))]
    prefix = bytearray(b'BSJB' + struct.pack('<HHII', 1, 1, 0, 12) + b'v4.0.30319\0\0')
    prefix.extend(struct.pack('<HH', 0, len(streams)))
    headers = []
    for name, data in streams:
        headers.append(len(prefix))
        name_bytes = name.encode() + b'\0'
        prefix.extend(bytes(8) + name_bytes + bytes((-len(name_bytes)) % 4))
    metadata = prefix
    for header, (_, data) in zip(headers, streams):
        metadata.extend(bytes((-len(metadata)) % 4))
        struct.pack_into('<II', metadata, header, len(metadata), len(data))
        metadata.extend(data)
    md_offset = 0x600
    resource_offset = c.align(md_offset + len(metadata), 4)
    total = c.align(resource_offset + (4 + len(resources) if resources is not None else 0), 0x200)
    raw = bytearray(total)
    raw[:2] = b'MZ'
    struct.pack_into('<I', raw, 60, 0x80)
    raw[0x80:0x84] = b'PE\0\0'
    optsize = 240 if pe64 else 224
    struct.pack_into('<HHIIIHH', raw, 0x84, 0x8664 if pe64 else 0x14c, 1, 0, 0, 0, optsize, 0x2102)
    opt = 0x98
    struct.pack_into('<H', raw, opt, 0x20b if pe64 else 0x10b)
    struct.pack_into('<I', raw, opt + 8, total - 0x200)
    struct.pack_into('<II', raw, opt + 32, 0x2000, 0x200)
    struct.pack_into('<II', raw, opt + 56, 0x4000, 0x200)
    dd = opt + (112 if pe64 else 96)
    struct.pack_into('<I', raw, dd - 4, 16)
    struct.pack_into('<II', raw, dd + 14 * 8, 0x2000, 72)
    sh = opt + optsize
    struct.pack_into('<8sIIIIIIHHI', raw, sh, b'.text\0\0\0', total - 0x200, 0x2000, total - 0x200, 0x200, 0, 0, 0, 0, 0x60000020)
    struct.pack_into('<IHHIIII', raw, 0x200, 72, 2, 5, 0x2000 + md_offset - 0x200, len(metadata), 9 if strong else 1, 0)
    for offset, code in zip(body_offsets, codes):
        if len(code) < 64:
            raw[offset:offset + 1 + len(code)] = bytes([(len(code) << 2) | 2]) + code
        else:
            raw[offset:offset + 12 + len(code)] = struct.pack('<HHII', 0x3003, 8, len(code), 0) + code
    raw[md_offset:md_offset + len(metadata)] = metadata
    if resources is not None:
        struct.pack_into('<II', raw, 0x200 + 24, 0x2000 + resource_offset - 0x200, 4 + len(resources))
        struct.pack_into('<I', raw, resource_offset, len(resources))
        raw[resource_offset + 4:resource_offset + 4 + len(resources)] = resources
    return bytes(raw)


def resource_fixture(version=2):
    """Actual .resources table: two string entries and one preserved integer."""
    def text(value, encoding='utf-8'):
        data = value.encode(encoding)
        return put_seven(len(data)) + data
    header = text('System.Resources.ResourceReader, mscorlib') + text('System.Resources.RuntimeResourceSet')
    raw = bytearray(struct.pack('<III', 0xbeefcace, 1, len(header)) + header)
    types = ['System.String, mscorlib', 'System.Int32, mscorlib'] if version == 1 else []
    raw.extend(struct.pack('<III', version, 3, len(types)))
    for name in types:
        raw.extend(text(name))
    raw.extend(bytes((-len(raw)) % 8))
    # FastResourceComparer hashes for a, c, b, sorted as signed Int32.
    raw.extend(struct.pack('<iii', 177604, 177606, 177607))
    names, values, positions = bytearray(), bytearray(), []
    for key, source in [('a', 'SYNTHETIC RESOURCE'), ('c', 'SYNTHETIC RESOURCE OTHER'), ('b', None)]:
        positions.append(len(names))
        names.extend(text(key, 'utf-16-le') + struct.pack('<i', len(values)))
        if source is None:
            values.extend(put_seven(1 if version == 1 else 8) + struct.pack('<i', 42))
        else:
            values.extend(put_seven(0 if version == 1 else 1) + text(source))
    raw.extend(struct.pack('<iii', *positions))
    raw.extend(struct.pack('<i', len(raw) + 4 + len(names)))
    return bytes(raw + names + values)


def build_fixture():
    return fixture(resources=resource_fixture())


def build_header_growth_fixture():
    """PE32+ with three section headers exactly filling its 512-byte headers."""
    raw = bytearray(fixture(pe64=True, resources=resource_fixture()))
    struct.pack_into('<H', raw, 0x86, 3)
    section_table = 0x98 + 240
    for index, rva in [(1, 0x4000), (2, 0x6000)]:
        struct.pack_into('<8sIIIIIIHHI', raw, section_table + index * 40, b'.empty\0\0', 0x1000, rva, 0, 0, 0, 0, 0, 0, 0x40000040)
    struct.pack_into('<I', raw, 0x98 + 56, 0x8000)
    return bytes(raw)


def build_native_fixture(*, carried_import=False, unrelated_function=False):
    """PE64/IAT/unwind fixture: two exact MessageBoxW UTF-16 arguments.

    Source at RVA 0x4000 = SYNTHETIC NATIVE SOURCE, use RVA 0x2080.
    Caption at RVA 0x4100 = SYNTHETIC CAPTION, use RVA 0x2087.
    Neither module nor its imports are ever loaded or executed.
    """
    raw = bytearray(fixture(pe64=True))
    original_size = len(raw)
    raw.extend(bytes(1024))
    dd = 0x98 + 112
    struct.pack_into('<II', raw, dd + 14 * 8, 0, 0)
    struct.pack_into('<II', raw, dd + 1 * 8, 0x4060, 40)
    struct.pack_into('<II', raw, dd + 3 * 8, 0x6000, 12)
    struct.pack_into('<H', raw, 0x86, 3)
    struct.pack_into('<I', raw, 0x98 + 56, 0x8000)
    for index, name, rva, pointer in [(1, b'.rdata\0\0', 0x4000, original_size), (2, b'.pdata\0\0', 0x6000, original_size + 512)]:
        struct.pack_into('<8sIIIIIIHHI', raw, 0x98 + 240 + index * 40, name, 512, rva, 512, pointer, 0, 0, 0, 0, 0x40000040)
    def data(rva, value):
        offset = original_size + rva - 0x4000
        raw[offset:offset + len(value)] = value
    data(0x4000, 'SYNTHETIC NATIVE SOURCE\0'.encode('utf-16-le'))
    data(0x4100, 'SYNTHETIC CAPTION\0'.encode('utf-16-le'))
    data(0x4060, struct.pack('<IIIII', 0x40c0, 0, 0, 0x40a0, 0x40d0))
    data(0x40a0, b'user32.dll\0')
    data(0x40c0, struct.pack('<QQ', 0x40e0, 0))
    data(0x40d0, struct.pack('<QQ', 0x40e0, 0))
    data(0x40e0, b'\0\0MessageBoxW\0')
    code = b'\x48\x8d\x15' + struct.pack('<i', 0x4000 - 0x2087)
    code += b'\x4c\x8d\x05' + struct.pack('<i', 0x4100 - 0x208e)
    if carried_import:
        code += b'\x48\x8b\x05' + struct.pack('<i', 0x40d0 - 0x2095) + b'\xff\xd0\xc3'
    else:
        code += b'\xff\x15' + struct.pack('<i', 0x40d0 - 0x2094) + b'\xc3'
    raw[0x280:0x280 + len(code)] = code
    struct.pack_into('<III', raw, original_size + 512, 0x2080, 0x2080 + len(code), 0x6020)
    raw[original_size + 512 + 32:original_size + 512 + 36] = b'\x01\x00\x00\x00'
    if unrelated_function:
        other = b'\x48\x8d\x15' + struct.pack('<i', 0x4000 - 0x2107) + b'\xc3'
        raw[0x300:0x300 + len(other)] = other
        struct.pack_into('<II', raw, dd + 3 * 8, 0x6000, 24)
        struct.pack_into('<III', raw, original_size + 512 + 12, 0x2100, 0x2100 + len(other), 0x6020)
    return bytes(raw)
