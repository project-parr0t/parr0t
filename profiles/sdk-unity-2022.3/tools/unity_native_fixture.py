"""Unmistakably synthetic Windows resource bytes for extractor preparation."""
import struct

from unity_managed_fixture import build_fixture
from unity_native import _rebuild_tree
from unity_pe import append_data, set_directory

SOURCE_RESOURCE = 'SYNTHETIC NATIVE RESOURCE'


def build_resource_fixture(*, resource_type=6, payload=None, codepage=1200, base=None):
    if payload is None:
        strings = [SOURCE_RESOURCE, SOURCE_RESOURCE] + [''] * 14
        payload = b''.join(struct.pack('<H', len(value)) + value.encode('utf-16-le') for value in strings)
    def branch(key, child):
        header = bytearray(16)
        struct.pack_into('<H', header, 14, 1)
        return {'header': bytes(header), 'entries': [(key, child)]}
    leaf = {'path': [resource_type, 1, 1033], 'data': payload, 'codepage': codepage, 'reserved': 0}
    tree = branch(resource_type, branch(1, branch(1033, leaf)))
    rebuilt, rva = append_data(build_fixture() if base is None else base, _rebuild_tree(tree, 0))
    payload = _rebuild_tree(tree, rva)
    pe = struct.unpack_from('<I', rebuilt, 0x3c)[0]
    table = pe + 24 + struct.unpack_from('<H', rebuilt, pe + 20)[0]
    for index in range(struct.unpack_from('<H', rebuilt, pe + 6)[0]):
        size, address, disk_size, disk = struct.unpack_from('<IIII', rebuilt, table + index * 40 + 8)
        if address <= rva < address + max(size, disk_size):
            offset = disk + rva - address
            result = bytearray(rebuilt)
            result[offset:offset + len(payload)] = payload
            return set_directory(bytes(result), 2, rva, len(payload))
    raise ValueError('SYNTHETIC_RESOURCE_SECTION_MISSING')


def build_resource_consumer_fixture(*, message=False, identifier=0, display=True,
                                   wrong_buffer=False, dll=False, branch=False,
                                   overwrite=False, unknown_id=False,
                                   system_fallback=False, missing_resource=False,
                                   branch_before=False, interprocedural=False,
                                   ansi=False, extra_display_argument=False):
    """Real synthetic x64 imports/unwind/resource consumers; never executable here."""
    from unity_managed_fixture import build_native_fixture
    from unity_pe import PE
    raw, rva = append_data(build_native_fixture(), bytes(1024))
    image = PE(raw)
    result = bytearray(raw)
    characteristics = struct.unpack_from('<H', result, image.pe + 22)[0]
    struct.pack_into('<H', result, image.pe + 22, characteristics | 0x2000 if dll else characteristics & ~0x2000)
    section = next(s for s in image.sections if s['rva'] == rva)
    struct.pack_into('<I', result, section['header'] + 36, 0xc0000040)
    def data(offset, value):
        start = image.at(rva + offset, len(value))
        result[start:start + len(value)] = value
    loader = 'FormatMessageW' if message else 'LoadStringA' if ansi else 'LoadStringW'
    data(0x80, b'user32.dll\0')
    data(0xa0, b'kernel32.dll\0')
    consumer = 'MessageBoxW' if extra_display_argument else 'SetWindowTextW'
    for offset, name in [(0xc0, loader), (0xe0, consumer if display else 'NotDisplayW'), (0x100, 'GetModuleHandleW')]:
        data(offset, b'\0\0' + name.encode('ascii') + b'\0')
    # Each descriptor has an independent terminated INT and IAT.
    for index, (name, library) in enumerate([(0xc0, 0xa0 if message else 0x80), (0xe0, 0x80), (0x100, 0xa0)]):
        thunk, iat = rva + 0x140 + index * 16, rva + 0x180 + index * 16
        data(index * 20, struct.pack('<IIIII', thunk, 0, 0, rva + library, iat))
        data(0x140 + index * 16, struct.pack('<QQ', rva + name, 0))
        data(0x180 + index * 16, struct.pack('<QQ', rva + name, 0))
    code = bytearray(b'\x48\x83\xec\x38')
    code_rva = 0x2180 if interprocedural else 0x2080
    def call(index):
        code.extend(b'\xff\x15' + struct.pack('<i', rva + 0x180 + index * 16 - (code_rva + len(code) + 6)))
    def lea(register, pointer):
        prefix, operand = {2: (0x48, 0x15), 8: (0x4c, 0x05), 0: (0x48, 0x05)}[register]
        code.extend(bytes([prefix, 0x8d, operand]) + struct.pack('<i', pointer - (code_rva + len(code) + 7)))
    if branch_before:
        code.extend(b'\x75\x00')
    if not message:
        code.extend(b'\x31\xc9')
        call(2)
        code.extend(b'\x48\x89\xc1')
        if unknown_id:
            code.extend(b'\x89\xda')  # Unknown EBX, never guess an ID.
        else:
            code.extend(b'\xba' + struct.pack('<I', identifier))
        lea(8, rva + 0x240)
        code.extend(b'\x41\xb9\x20\0\0\0')
    else:
        flags = 0x800 | (0x1000 if system_fallback else 0)
        code.extend(b'\xb9' + struct.pack('<I', flags) + b'\x31\xd2')
        code.extend(b'\x41\xb8' + struct.pack('<I', identifier))
        code.extend(b'\x45\x31\xc9')
        lea(0, rva + 0x240)
        code.extend(b'\x48\x89\x44\x24\x20')
    call(0)
    helper = None
    if interprocedural:
        helper = bytes(code) + b'\x48\x83\xc4\x38\xc3'
        code_rva = 0x2080
        code = bytearray(b'\x48\x83\xec\x38\xe8' + struct.pack('<i', 0x2180 - 0x2089))
    if branch:
        code.extend(b'\x75\x00')
    if overwrite:
        code.extend(b'\x48\x89\x18')  # Unknown memory write invalidates buffer evidence.
    lea(2, rva + (0x300 if wrong_buffer else 0x240))
    call(1)
    code.extend(b'\x48\x83\xc4\x38\xc3')
    start = image.at(0x2080, len(code))
    result[start:start + len(code)] = code
    pdata = image.at(0x6000, 12)
    struct.pack_into('<III', result, pdata, 0x2080, 0x2080 + len(code), 0x6020)
    if helper is not None:
        start = image.at(0x2180, len(helper))
        result[start:start + len(helper)] = helper
        struct.pack_into('<III', result, pdata + 12, 0x2180, 0x2180 + len(helper), 0x6020)
        directory = image.dd + 3 * 8
        struct.pack_into('<II', result, directory, 0x6000, 24)
    raw = set_directory(bytes(result), 1, rva, 80)
    if missing_resource:
        return raw
    if message:
        values = ['SYNTHETIC SYSTEM %1!s!%n', 'SYNTHETIC INTERNAL MESSAGE']
        payload = bytearray(struct.pack('<IIII', 1, 0, 1, 16))
        for value in values:
            text = value.encode('utf-16-le') + b'\0\0'
            size = (4 + len(text) + 3) & ~3
            payload.extend(struct.pack('<HH', size, 1) + text + bytes(size - 4 - len(text)))
        return build_resource_fixture(base=raw, resource_type=11, payload=bytes(payload))
    return build_resource_fixture(base=raw)
