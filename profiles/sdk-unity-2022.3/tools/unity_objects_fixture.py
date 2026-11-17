"""Public synthetic Unity bytes for the pre-inventory extractor validation gate."""
import struct
def string(value, padding=b'\0'):
    encoded = value.encode('utf-8')
    return struct.pack('<i', len(encoded)) + encoded + padding * ((-len(encoded)) % 4)


def node(level, ty, name, size=-1, flags=0, meta=0):
    return dict(level=level, type=ty, name=name, size=size, flags=flags, meta=meta)


def typetree(nodes):
    strings, offsets = bytearray(), {}
    for n in nodes:
        for key in ('type', 'name'):
            text = n[key]
            if text not in offsets:
                offsets[text] = len(strings)
                strings.extend(text.encode('utf-8') + b'\0')
    records = b''.join(struct.pack('<HBBIIiiiQ', 1, n['level'], n['flags'],
        offsets[n['type']], offsets[n['name']], n['size'], i, n['meta'], 0)
        for i, n in enumerate(nodes))
    return struct.pack('<ii', len(nodes), len(strings)) + records + strings


def serialized(types_, payloads, *, trees=True, externals=()):
    """payloads: (path ID, type index, exact object bytes)."""
    metadata = bytearray(b'2022.3.99f1\0' + struct.pack('<iBi', 19, trees, len(types_)))
    for cid, nodes in types_:
        metadata.extend(struct.pack('<iBh', cid, 0, -1))
        if cid == 114:
            metadata.extend(bytes(16))
        metadata.extend(bytes(16))
        if trees:
            metadata.extend(typetree(nodes) + struct.pack('<i', 0))
    metadata.extend(struct.pack('<i', len(payloads)))
    data = bytearray()
    for pid, tid, payload in payloads:
        metadata.extend(bytes((-(48 + len(metadata))) % 4))
        metadata.extend(struct.pack('<qqIi', pid, len(data), len(payload), tid))
        data.extend(payload)
        data.extend(b'G' * ((-len(data)) % 8))
    metadata.extend(struct.pack('<ii', 0, len(externals)))
    for external in externals:
        metadata.extend(b'\0' + bytes(16) + struct.pack('<i', 0) + external.encode() + b'\0')
    metadata.extend(struct.pack('<i', 0) + b'\0')
    offset = (48 + len(metadata) + 15) & ~15
    header = bytearray(48)
    struct.pack_into('>IIII', header, 0, 0, 0, 22, 0)
    struct.pack_into('>IQQQ', header, 20, len(metadata), offset + len(data), offset, 0)
    return bytes(header + metadata + b'M' * (offset - 48 - len(metadata)) + data)


def text_tree(cid=102):
    return [node(0, 'TextMesh' if cid == 102 else 'Synthetic', 'Base'),
            node(1, 'string', 'm_Text'), node(2, 'Array', 'Array', flags=1),
            node(3, 'int', 'size', 4), node(3, 'char', 'data', 1)]


def nested_tree():
    return [node(0, 'Synthetic', 'Base'),
            node(1, 'string', 'first'), node(2, 'Array', 'Array', flags=1),
            node(3, 'int', 'size', 4), node(3, 'char', 'data', 1),
            node(1, 'vector', 'items'), node(2, 'Array', 'Array', flags=1),
            node(3, 'int', 'size', 4), node(3, 'SyntheticItem', 'data'),
            node(4, 'string', 'caption'), node(5, 'Array', 'Array', flags=1),
            node(6, 'int', 'size', 4), node(6, 'char', 'data', 1),
            node(1, 'PPtr<TextAsset>', 'resource', 12),
            node(2, 'int', 'm_FileID', 4), node(2, 'SInt64', 'm_PathID', 8)]


def unityfs(entries, *, flags=0x40, version=7):
    payload, info = bytearray(), bytearray(bytes(16))
    positions = []
    for name, value, kind in entries:
        positions.append((name, len(payload), len(value), kind))
        payload.extend(value)
    info.extend(struct.pack('>iIIH', 1, len(payload), len(payload), 0))
    info.extend(struct.pack('>i', len(entries)))
    for name, offset, size, kind in positions:
        info.extend(struct.pack('>qqI', offset, size, kind) + name.encode() + b'\0')
    header = bytearray(b'UnityFS\0' + struct.pack('>I', version) + b'5.x.x\0' + b'2022.3.99f1\0')
    size_at = len(header)
    header.extend(struct.pack('>QIII', 0, len(info), len(info), flags))
    if version >= 7:
        header.extend(bytes((-len(header)) % 16))
    if flags & 0x80:
        if flags & 0x200:
            header.extend(bytes((-len(header)) % 16))
        header.extend(payload); header.extend(info)
    else:
        header.extend(info)
        if flags & 0x200:
            header.extend(bytes((-len(header)) % 16))
        header.extend(payload)
    struct.pack_into('>Q', header, size_at, len(header))
    return bytes(header)


CONTAINER_ENTRY = 'synthetic.assets'
EXPECTED_STRINGS = ['SOURCE_TEXT', 'SOURCE_TEXT', '', 'LAST_TEXT',
                    'SYNTHETIC_TABLE', '{"rows":["TABLE_TEXT","TABLE_TEXT",""]}',
                    'TABLE_TEXT', 'TABLE_TEXT', '']
EXPECTED_LOCATIONS = [
    {'object_id': 1, 'field_path': ['m_Text']},
    {'object_id': 1, 'field_path': ['items', 0, 'caption']},
    {'object_id': 1, 'field_path': ['items', 1, 'caption']},
    {'object_id': 1, 'field_path': ['items', 2, 'caption']},
    {'object_id': 2, 'field_path': ['m_Name']},
    {'object_id': 2, 'field_path': ['m_Script']},
    {'object_id': 2, 'field_path': ['m_Script'], 'parser': 'json', 'parser_path': ['rows', 0]},
    {'object_id': 2, 'field_path': ['m_Script'], 'parser': 'json', 'parser_path': ['rows', 1]},
    {'object_id': 2, 'field_path': ['m_Script'], 'parser': 'json', 'parser_path': ['rows', 2]},
]


def build_fixture():
    """Real v22 LE bytes: native UI, nested arrays, PPtr, TextAsset, opaque bytes."""
    display_nodes = nested_tree()
    display_nodes[0]['type'] = 'TextMesh'
    display_nodes[1]['name'] = 'm_Text'
    payload = string('SOURCE_TEXT') + struct.pack('<i', 3)
    payload += string('SOURCE_TEXT') + string('') + string('LAST_TEXT')
    payload += struct.pack('<iq', 0, 2)
    asset_nodes = [node(0, 'TextAsset', 'Base'),
        node(1, 'string', 'm_Name'), node(2, 'Array', 'Array', flags=1),
        node(3, 'int', 'size', 4), node(3, 'char', 'data', 1),
        node(1, 'string', 'm_Script'), node(2, 'Array', 'Array', flags=1),
        node(3, 'int', 'size', 4), node(3, 'char', 'data', 1)]
    opaque_nodes = [node(0, 'SyntheticOpaque', 'Base'),
                    node(1, 'UInt64', 'unownedA', 8), node(1, 'UInt32', 'unownedB', 4)]
    asset = string('SYNTHETIC_TABLE') + string('{"rows":["TABLE_TEXT","TABLE_TEXT",""]}')
    return serialized([(102, display_nodes), (49, asset_nodes), (28, opaque_nodes)],
                      [(1, 0, payload), (2, 1, asset), (3, 2, b'OPAQUE_BYTES')])


def build_bundle_fixture(*, flags=0x40):
    return unityfs([(CONTAINER_ENTRY, build_fixture(), 4),
                    ('synthetic.resS', b'UNOWNED_RESOURCE_BYTES', 0)], flags=flags)
