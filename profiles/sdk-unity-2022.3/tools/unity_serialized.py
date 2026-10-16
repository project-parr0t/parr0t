"""Bounded Unity v22 standalone serialized object replacement."""
import copy
import struct

def pack_string(text):
    b = text.encode('utf-8')
    return struct.pack('<i', len(b)) + b + bytes((-len(b)) % 4)

def string_at(raw, start):
    if not isinstance(start, int) or start < 0 or start + 4 > len(raw):
        raise ValueError('STRING_BOUNDARY_INVALID')
    n = struct.unpack_from('<i', raw, start)[0]
    if n < 0 or start + 4 + n > len(raw):
        raise ValueError('STRING_BOUNDARY_INVALID')
    end = (start + 4 + n + 3) & ~3
    if end > len(raw):
        raise ValueError('STRING_PADDING_INVALID')
    try:
        return raw[start + 4:start + 4 + n].decode('utf-8'), end
    except UnicodeDecodeError as exc:
        raise ValueError('STRING_UTF8_INVALID') from exc

def unique_boundary(raw, encoded):
    if not encoded:
        raise ValueError('BINDING_BOUNDARY_AMBIGUOUS')
    start = raw.find(encoded)
    if start < 0 or raw.find(encoded, start + 1) >= 0:
        raise ValueError('BINDING_BOUNDARY_AMBIGUOUS')
    return start, start + len(encoded)

def environment(raw):
    # Loading raw serialized bytes never requests a texture/image decoder.
    if len(raw) < 48:
        raise ValueError('SERIALIZED_HEADER_BOUNDARY_INVALID')
    import UnityPy
    e = UnityPy.load(raw)
    if e.file.__class__.__name__ != 'SerializedFile':
        raise ValueError('STANDALONE_SERIALIZED_FILE_REQUIRED')
    if e.file.header.version != 22 or e.file.header.endian != '<':
        raise ValueError('SERIALIZED_LAYOUT_NOT_BOUND')
    return e

def _validate_regions(raw, f):
    offset = f.header.data_offset
    metadata_size = f.header.metadata_size
    if (offset < 48 or offset > len(raw) or metadata_size < 0
            or 48 + metadata_size > offset):
        raise ValueError('SERIALIZED_HEADER_BOUNDARY_INVALID')
    cursor = offset
    for obj in sorted(f.objects.values(), key=lambda item: item.byte_start):
        if obj.byte_start < cursor:
            raise ValueError('OBJECT_REGIONS_OVERLAP')
        if obj.byte_size < 0 or obj.byte_start + obj.byte_size > len(raw):
            raise ValueError('OBJECT_BOUNDARY_INVALID')
        cursor = obj.byte_start + obj.byte_size

def object_table(raw, env):
    f = env.file
    _validate_regions(raw, f)
    entries = b''.join(struct.pack('<qqIi', o.path_id,
        o.byte_start - f.header.data_offset, o.byte_size, o.type_id)
        for o in f.objects.values())
    start, end = unique_boundary(raw[:f.header.data_offset], entries)
    if start < 48 or end > 48 + f.header.metadata_size:
        raise ValueError('OBJECT_TABLE_OUTSIDE_METADATA')
    return start

def rebuild_serialized(raw, changed):
    """Preserve exact original metadata and gaps, changing only offsets/sizes.

    Object payloads are opaque bytes, including textures. No object serializer
    normalizes unrelated fields. Unity v22's table is bound against every entry.
    """
    env = environment(raw)
    f = env.file
    table = object_table(raw, env)
    if not set(changed) <= set(f.objects):
        raise ValueError('OBJECT_BINDING_MISSING')
    if any(not isinstance(value, bytes) for value in changed.values()):
        raise ValueError('OBJECT_PAYLOAD_INVALID')
    data = bytearray(raw[:f.header.data_offset])
    cursor = f.header.data_offset
    positions = {}
    for o in sorted(f.objects.values(), key=lambda x: x.byte_start):
        if o.byte_start < cursor:
            raise ValueError('OBJECT_REGIONS_OVERLAP')
        data.extend(raw[cursor:o.byte_start])
        value = changed.get(o.path_id, o.get_raw_data())
        positions[o.path_id] = (len(data) - f.header.data_offset, len(value))
        data.extend(value)
        # Preserve the existing gap and add only relocation alignment padding.
        data.extend(bytes((o.byte_size - len(value)) % 8))
        cursor = o.byte_start + o.byte_size
    data.extend(raw[cursor:])
    for i, o in enumerate(f.objects.values()):
        off, size = positions[o.path_id]
        struct.pack_into('<qI', data, table + i * 24 + 8, off, size)
    struct.pack_into('>q', data, 24, len(data))
    result = bytes(data)
    check = environment(result)
    if [(o.path_id,o.type.name) for o in env.objects] != [(o.path_id,o.type.name) for o in check.objects]:
        raise ValueError('OBJECT_STRUCTURE_CHANGED')
    for o in check.objects:
        if o.get_raw_data() != changed.get(o.path_id, f.objects[o.path_id].get_raw_data()):
            raise ValueError('OBJECT_RAW_BYTES_CHANGED')
    # All metadata bytes except the relocation fields/file size remain exact.
    before_meta, after_meta = bytearray(raw[:f.header.data_offset]), bytearray(result[:f.header.data_offset])
    before_meta[24:32] = after_meta[24:32] = bytes(8)
    for i in range(len(f.objects)):
        p = table + i * 24 + 8
        before_meta[p:p+12] = after_meta[p:p+12] = bytes(12)
    if before_meta != after_meta:
        raise ValueError('NONTEXT_METADATA_CHANGED')
    return result


def replace_font_payload(raw, path_id, font_data, font_names):
    """Change only a bound Font's embedded payload and family names."""
    env = environment(raw)
    obj = env.file.objects.get(path_id)
    if (obj is None or obj.type.name != "Font"
            or not isinstance(font_data, bytes) or not font_data
            or not isinstance(font_names, (list, tuple)) or not font_names
            or any(not isinstance(name, str) or not name for name in font_names)):
        raise ValueError("FONT_PAYLOAD_BINDING_INVALID")
    tree = obj.read_typetree()
    changed = copy.deepcopy(tree)
    changed["m_FontData"] = list(font_data)
    changed["m_FontNames"] = list(font_names)
    payload = obj.save_typetree(changed)
    rebuilt = rebuild_serialized(raw, {path_id: payload})
    check = environment(rebuilt).file.objects[path_id].read_typetree()
    if check != changed:
        raise ValueError("FONT_PAYLOAD_ROUNDTRIP_INVALID")
    return rebuilt
