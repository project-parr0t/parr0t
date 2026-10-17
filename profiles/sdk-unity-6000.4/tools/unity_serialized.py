"""Bounded Unity 6000.4 serialized object replacement."""
import copy
import struct


def aligned_behaviour_node(records):
    """Repair only the native MonoBehaviour enabled-byte alignment boundary.

    Caller-supplied schemas remain private. A lossless object round trip is
    still mandatory before any typed edit; this repair cannot validate a schema.
    Reconstructing the node also discards cached parser state.
    """
    from UnityPy.helpers.TypeTreeNode import TypeTreeNode
    records = copy.deepcopy(records)
    fields = [r for r in records if r['m_Level'] == 1]
    if (len(fields) < 3 or [r['m_Name'] for r in fields[:3]] !=
            ['m_GameObject', 'm_Enabled', 'm_Script']
            or fields[1]['m_Type'] != 'UInt8'):
        raise ValueError('BEHAVIOUR_SCHEMA_NOT_BOUND')
    fields[1]['m_MetaFlag'] |= 0x4000
    return TypeTreeNode.from_list(records)


def metadata(file):
    """Snapshot serialized type bytes and protected header/script metadata."""
    from UnityPy.streams import EndianBinaryWriter
    tables = []
    for types, reference in ((file.types, False), (file.ref_types, True)):
        writer = EndianBinaryWriter(endian=file.header.endian)
        for typ in types:
            typ.write(file, writer, reference)
        tables.append(writer.bytes)
    return (file.unity_version, file.header.version, file.header.endian,
            file.header.reserved, file._m_target_platform, file._enable_type_tree,
            file.unknown, file.userInformation, *tables,
            [(s.local_serialized_file_index, s.local_identifier_in_file)
             for s in file.script_types])


def externals(file):
    return [(e.path, e.temp_empty, e.guid, e.type) for e in file.externals]


def _field(tree, path):
    if not isinstance(path, list) or not path:
        raise ValueError('FIELD_PATH_INVALID')
    value = tree
    try:
        for key in path[:-1]:
            if type(key) not in (str, int) or (type(key) is int and key < 0):
                raise ValueError('FIELD_PATH_INVALID')
            value = value[key]
        key = path[-1]
        if type(key) not in (str, int) or (type(key) is int and key < 0):
            raise ValueError('FIELD_PATH_INVALID')
        current = value[key]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError('FIELD_BINDING_MISSING') from exc
    return value, key, current


def apply_serialized(raw, bindings, *, generator=None, nodes=None):
    """Apply explicit typed paths OR nonoverlapping padded raw spans per object.

    Each row binds object_id, source, target, and either field_path or start/end.
    This is bounded application, not discovery or translation eligibility.
    TextAsset-specific paths are deliberately outside this profile.
    """
    env = environment(raw)
    env.typetree_generator = generator
    groups = {}
    for row in bindings:
        if (not isinstance(row.get('source'), str)
                or not isinstance(row.get('target'), str)
                or type(row.get('object_id')) is not int):
            raise ValueError('TEXT_BINDING_INVALID')
        groups.setdefault(row['object_id'], []).append(row)
    replacements, expected = {}, {}
    for oid, rows in groups.items():
        obj = env.file.objects.get(oid)
        if obj is None or obj.type.name == 'TextAsset':
            raise ValueError('OBJECT_BINDING_UNSUPPORTED')
        original = obj.get_raw_data()
        typed = ['field_path' in row for row in rows]
        if any(typed) != all(typed):
            raise ValueError('OBJECT_BINDING_MODES_MIXED')
        if all(typed):
            node = (nodes or {}).get(oid)
            tree = obj.read_typetree(nodes=node)
            if obj.save_typetree(tree, nodes=node) != original:
                raise ValueError('TYPE_TREE_NOT_LOSSLESS')
            paths = [tuple(r['field_path']) for r in rows]
            if len(paths) != len(set(paths)):
                raise ValueError('FIELD_BINDING_AMBIGUOUS')
            changed = copy.deepcopy(tree)
            for row in rows:
                parent, key, value = _field(changed, row['field_path'])
                if value != row['source'] or not isinstance(value, str):
                    raise ValueError('FIELD_SOURCE_CHANGED')
                parent[key] = row['target']
            replacements[oid] = obj.save_typetree(changed, nodes=node)
            expected[oid] = changed
        else:
            spans = []
            for row in rows:
                start, end = row.get('start'), row.get('end')
                if type(start) is not int or type(end) is not int or start % 4:
                    raise ValueError('STRING_BOUNDARY_INVALID')
                text, actual_end = string_at(original, start)
                if text != row['source'] or actual_end != end:
                    raise ValueError('STRING_SOURCE_CHANGED')
                spans.append((start, end, row['target']))
            spans.sort()
            if any(b[0] < a[1] for a, b in zip(spans, spans[1:])):
                raise ValueError('STRING_SPANS_OVERLAP')
            payload = original
            for start, end, target in reversed(spans):
                payload = payload[:start] + pack_string(target) + payload[end:]
            replacements[oid] = payload
    rebuilt = rebuild_serialized(raw, replacements)
    check = environment(rebuilt)
    check.typetree_generator = generator
    for oid, tree in expected.items():
        if check.file.objects[oid].read_typetree(nodes=(nodes or {}).get(oid)) != tree:
            raise ValueError('FIELD_READBACK_CHANGED')
    return rebuilt


def bundle_members(raw):
    import UnityPy
    env = UnityPy.load(raw)
    if env.file.__class__.__name__ != 'BundleFile' or not raw.startswith(b'UnityFS\0'):
        raise ValueError('UNITYFS_REQUIRED')
    return env, {name: (file.reader.bytes if hasattr(file, 'objects') else file.bytes)
                 for name, file in env.file.files.items()}


def rebuild_bundle(raw, replacements):
    """Rebuild explicit serialized members; preserve entry flags/name and streams.

    Replacements must already pass serialized application/font verification.
    This layer verifies exact serialized object bytes, metadata and entry identity
    after UnityFS recompression without decoding opaque resource streams.
    """
    from UnityPy.streams import EndianBinaryReader
    env, members = bundle_members(raw)
    bundle = env.file
    if not set(replacements) <= set(members):
        raise ValueError('BUNDLE_MEMBER_MISSING')
    entry_metadata = {name: (file.flags, getattr(file, 'name', name)) for name, file in bundle.files.items()}
    expected = {}
    for name, data in replacements.items():
        if not hasattr(bundle.files[name], 'objects'):
            raise ValueError('OPAQUE_MEMBER_REPLACEMENT_UNSUPPORTED')
        file = environment(data).file
        expected[name] = (metadata(file), externals(file),
                          {oid: (o.type_id, o.get_raw_data()) for oid, o in file.objects.items()})
    # Byte readers avoid normalizing even an untouched serialized member.
    for name in members:
        reader = EndianBinaryReader(replacements.get(name, members[name]))
        reader.flags, reader.name = entry_metadata[name]
        bundle.files[name] = reader
    result = bundle.save(packer='lz4')
    check, after = bundle_members(result)
    if set(after) != set(members):
        raise ValueError('BUNDLE_MEMBERS_CHANGED')
    for name, file in check.file.files.items():
        if (file.flags, getattr(file, 'name', name)) != entry_metadata[name]:
            raise ValueError('BUNDLE_ENTRY_METADATA_CHANGED')
        if name not in replacements:
            if after[name] != members[name]:
                raise ValueError('UNOWNED_BUNDLE_MEMBER_CHANGED')
        elif (metadata(file), externals(file),
              {oid: (o.type_id, o.get_raw_data()) for oid, o in file.objects.items()}) != expected[name]:
            raise ValueError('BUNDLE_SERIALIZED_READBACK_CHANGED')
    return result

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
    if (e.file.header.version != 22 or e.file.header.endian != '<'
            or not e.file.unity_version.startswith('6000.4.')):
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
