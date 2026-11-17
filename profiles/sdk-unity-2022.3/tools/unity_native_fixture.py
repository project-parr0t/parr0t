"""Unmistakably synthetic Windows resource bytes for extractor preparation."""
import struct

from unity_managed_fixture import build_fixture
from unity_native import _rebuild_tree
from unity_pe import append_data, set_directory

SOURCE_RESOURCE = 'SYNTHETIC NATIVE RESOURCE'


def build_resource_fixture(*, resource_type=6, payload=None, codepage=1200):
    if payload is None:
        strings = [SOURCE_RESOURCE, SOURCE_RESOURCE] + [''] * 14
        payload = b''.join(struct.pack('<H', len(value)) + value.encode('utf-16-le') for value in strings)
    def branch(key, child):
        header = bytearray(16)
        struct.pack_into('<H', header, 14, 1)
        return {'header': bytes(header), 'entries': [(key, child)]}
    leaf = {'path': [resource_type, 1, 1033], 'data': payload, 'codepage': codepage, 'reserved': 0}
    tree = branch(resource_type, branch(1, branch(1033, leaf)))
    rebuilt, rva = append_data(build_fixture(), _rebuild_tree(tree, 0))
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
