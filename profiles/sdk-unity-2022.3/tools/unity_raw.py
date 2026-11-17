"""First-party, bounded Unity 2022.3 serialized-byte and UnityFS readers.

No object deserializer or image decoder is imported. Unknown layouts fail closed;
opaque objects and container entries can still be retained by reconstruction.
"""
from __future__ import annotations

import importlib.metadata
import lzma
import struct


class FormatError(ValueError):
    """A categorical, payload-free serialized-format failure."""


class Cursor:
    def __init__(self, raw, pos=0, end=None):
        self.raw, self.pos = raw, pos
        self.end = len(raw) if end is None else end
        if not 0 <= pos <= self.end <= len(raw):
            raise FormatError('BOUNDARY_INVALID')

    def take(self, size):
        if size < 0 or size > self.end - self.pos:
            raise FormatError('BOUNDARY_INVALID')
        start = self.pos
        self.pos += size
        return self.raw[start:self.pos]

    def num(self, fmt):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def count(self, minimum=1):
        count = self.num('<i')
        if count < 0 or count > (self.end - self.pos) // minimum:
            raise FormatError('COUNT_INVALID')
        return count

    def z(self):
        end = self.raw.find(b'\0', self.pos, self.end)
        if end < 0:
            raise FormatError('STRING_TERMINATOR_MISSING')
        value = self.take(end - self.pos)
        self.take(1)
        try:
            return value.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise FormatError('STRING_UTF8_INVALID') from exc

    def align(self, alignment=4):
        self.take((-self.pos) % alignment)

    def string(self):
        start = self.pos
        count = self.num('<i')
        try:
            text = self.take(count).decode('utf-8')
        except UnicodeDecodeError as exc:
            raise FormatError('STRING_UTF8_INVALID') from exc
        content_end = self.pos
        self.align()
        return text, start, content_end, self.pos


# Public Unity type-tree common string buffer, indexed by its byte offsets.
_COMMON_NAMES = ('AABB', 'AnimationClip', 'AnimationCurve', 'AnimationState', 'Array',
    'Base', 'BitField', 'bitset', 'bool', 'char', 'ColorRGBA', 'Component', 'data',
    'deque', 'double', 'dynamic_array', 'FastPropertyName', 'first', 'float', 'Font',
    'GameObject', 'Generic Mono', 'GradientNEW', 'GUID', 'GUIStyle', 'int', 'list',
    'long long', 'map', 'Matrix4x4f', 'MdFour', 'MonoBehaviour', 'MonoScript',
    'm_ByteSize', 'm_Curve', 'm_EditorClassIdentifier', 'm_EditorHideFlags',
    'm_Enabled', 'm_ExtensionPtr', 'm_GameObject', 'm_Index', 'm_IsArray',
    'm_IsStatic', 'm_MetaFlag', 'm_Name', 'm_ObjectHideFlags', 'm_PrefabInternal',
    'm_PrefabParentObject', 'm_Script', 'm_StaticEditorFlags', 'm_Type', 'm_Version',
    'Object', 'pair', 'PPtr<Component>', 'PPtr<GameObject>', 'PPtr<Material>',
    'PPtr<MonoBehaviour>', 'PPtr<MonoScript>', 'PPtr<Object>', 'PPtr<Prefab>',
    'PPtr<Sprite>', 'PPtr<TextAsset>', 'PPtr<Texture>', 'PPtr<Texture2D>',
    'PPtr<Transform>', 'Prefab', 'Quaternionf', 'Rectf', 'RectInt', 'RectOffset',
    'second', 'set', 'short', 'size', 'SInt16', 'SInt32', 'SInt64', 'SInt8',
    'staticvector', 'string', 'TextAsset', 'TextMesh', 'Texture', 'Texture2D',
    'Transform', 'TypelessData', 'UInt16', 'UInt32', 'UInt64', 'UInt8',
    'unsigned int', 'unsigned long long', 'unsigned short', 'vector', 'Vector2f',
    'Vector3f', 'Vector4f', 'm_ScriptingClassIdentifier', 'Gradient', 'Type*',
    'int2_storage', 'int3_storage', 'BoundsInt', 'm_CorrespondingSourceObject',
    'm_PrefabInstance', 'm_PrefabAsset', 'FileSize', 'Hash128')
COMMON = {}
_common_offset = 0
for _name in _COMMON_NAMES:
    COMMON[_common_offset] = _name
    _common_offset += len(_name) + 1

FORMATS = {'bool': 'B', 'char': 'B', 'SInt8': 'b', 'UInt8': 'B',
    'short': 'h', 'SInt16': 'h', 'UInt16': 'H', 'unsigned short': 'H',
    'int': 'i', 'SInt32': 'i', 'UInt32': 'I', 'unsigned int': 'I',
    'long long': 'q', 'SInt64': 'q', 'UInt64': 'Q',
    'unsigned long long': 'Q', 'float': 'f', 'double': 'd', 'FileSize': 'Q'}
PRIMITIVES = {'bool': 'B', 'byte': 'B', 'sbyte': 'b', 'char': 'H',
    'int16': 'h', 'uint16': 'H', 'int32': 'i', 'uint32': 'I',
    'int64': 'q', 'uint64': 'Q', 'float32': 'f', 'float64': 'd'}
NATIVE_BASES = {'System.Object', 'System.ValueType', 'System.Enum',
    'UnityEngine.Object', 'UnityEngine.Component', 'UnityEngine.Behaviour',
    'UnityEngine.MonoBehaviour', 'UnityEngine.ScriptableObject'}


def _wire_field(name, descriptor, **flags):
    return dict(name=name, type=descriptor, serialized=True, static=False, **flags)


def _primitive_fields(names, primitive):
    return [_wire_field(name, dict(kind='primitive', name=primitive)) for name in names]


def _native_named(name):
    return dict(kind='named', assembly='UnityEngine.IMGUIModule' if 'GUI' in name else
                'UnityEngine.CoreModule', name='UnityEngine.' + name)


# Native-owned value layouts differ from their CLR pointer/backing fields.
# This is immutable public wire-schema data shared by independent interpreters.
NATIVE_LAYOUTS = {
    'UnityEngine.Vector2': _primitive_fields(('x', 'y'), 'float32'),
    'UnityEngine.Vector3': _primitive_fields(('x', 'y', 'z'), 'float32'),
    'UnityEngine.Vector4': _primitive_fields(('x', 'y', 'z', 'w'), 'float32'),
    'UnityEngine.Quaternion': _primitive_fields(('x', 'y', 'z', 'w'), 'float32'),
    'UnityEngine.Color': _primitive_fields(('r', 'g', 'b', 'a'), 'float32'),
    'UnityEngine.Rect': _primitive_fields(('x', 'y', 'width', 'height'), 'float32'),
    'UnityEngine.RectOffset': _primitive_fields(('left', 'right', 'top', 'bottom'), 'int32'),
    'UnityEngine.LayerMask': _primitive_fields(('m_Bits',), 'int32'),
    'UnityEngine.Color32': [_wire_field(n, dict(kind='primitive', name='byte'), packed=True,
        align_after=n == 'a') for n in ('r', 'g', 'b', 'a')],
    'UnityEngine.GUIStyleState': [
        _wire_field('m_Background', dict(kind='pptr', name='UnityEngine.Texture2D'), native_reference=True),
        _wire_field('m_TextColor', _native_named('Color'))],
    'UnityEngine.GUIContent': [
        _wire_field('m_Text', dict(kind='string'), native_display=True),
        _wire_field('m_Image', dict(kind='pptr', name='UnityEngine.Texture'), native_reference=True),
        _wire_field('m_Tooltip', dict(kind='string'), native_display=True)],
}
NATIVE_LAYOUTS['UnityEngine.GUIStyle'] = (
    [_wire_field('m_Name', dict(kind='string'), native_identity=True)] +
    [_wire_field(n, _native_named('GUIStyleState')) for n in
     ('m_Normal', 'm_Hover', 'm_Active', 'm_Focused', 'm_OnNormal', 'm_OnHover', 'm_OnActive', 'm_OnFocused')] +
    [_wire_field(n, _native_named('RectOffset')) for n in ('m_Border', 'm_Margin', 'm_Padding', 'm_Overflow')] +
    [_wire_field('m_Font', dict(kind='pptr', name='UnityEngine.Font'), native_reference=True)] +
    _primitive_fields(('m_FontSize', 'm_FontStyle', 'm_Alignment'), 'int32') +
    [_wire_field(n, dict(kind='primitive', name='bool'), packed=True, align_after=n == 'm_RichText')
     for n in ('m_WordWrap', 'm_RichText')] +
    _primitive_fields(('m_TextClipping', 'm_ImagePosition'), 'int32') +
    [_wire_field('m_ContentOffset', _native_named('Vector2'))] +
    _primitive_fields(('m_FixedWidth', 'm_FixedHeight'), 'float32') +
    [_wire_field(n, dict(kind='primitive', name='bool'), packed=True, align_after=n == 'm_StretchHeight')
     for n in ('m_StretchWidth', 'm_StretchHeight')])
UNSUPPORTED_NATIVE_LAYOUTS = frozenset({'UnityEngine.AnimationCurve', 'UnityEngine.Gradient',
    'UnityEngine.GUISettings'})


def serialized(raw):
    """Parse v22 LE metadata, retaining exact object-table relocation positions."""
    if len(raw) < 48 or struct.unpack_from('>I', raw, 8)[0] != 22:
        raise FormatError('SERIALIZED_VERSION_UNSUPPORTED')
    if raw[16] != 0:
        raise FormatError('SERIALIZED_ENDIAN_UNSUPPORTED')
    metadata_size, file_size, data_offset = struct.unpack_from('>IQQ', raw, 20)
    if file_size != len(raw) or not 48 <= 48 + metadata_size <= data_offset <= file_size:
        raise FormatError('SERIALIZED_HEADER_INVALID')
    c = Cursor(raw, 48, 48 + metadata_size)
    version, platform, trees = c.z(), c.num('<i'), c.num('<B')
    if trees not in (0, 1):
        raise FormatError('TYPE_TREE_FLAG_INVALID')

    def read_type(reference=False):
        cid, stripped, script_index = c.num('<i'), c.num('<B'), c.num('<h')
        if stripped not in (0, 1):
            raise FormatError('TYPE_STRIPPED_FLAG_INVALID')
        if (reference and script_index >= 0) or cid == 114:
            c.take(16)
        c.take(16)
        nodes = []
        if trees:
            count, string_size = c.num('<i'), c.num('<i')
            if count < 0 or string_size < 0 or count * 32 + string_size > c.end - c.pos:
                raise FormatError('TYPE_TREE_BOUNDS_INVALID')
            for _ in range(count):
                values = struct.unpack('<HBBIIiiiQ', c.take(32))
                _, level, flags, ty, name, size, _, meta, _ = values
                nodes.append(dict(level=level, flags=flags, type_offset=ty,
                                  name_offset=name, size=size, meta=meta))
            strings = c.take(string_size)

            def resolve(offset):
                if offset & 0x80000000:
                    try:
                        return COMMON[offset & 0x7fffffff]
                    except KeyError as exc:
                        raise FormatError('TYPE_TREE_COMMON_STRING_UNKNOWN') from exc
                if offset >= len(strings):
                    raise FormatError('TYPE_TREE_STRING_BOUNDS_INVALID')
                return Cursor(strings, offset).z()

            for index, node in enumerate(nodes):
                node['type'] = resolve(node.pop('type_offset'))
                node['name'] = resolve(node.pop('name_offset'))
                if (index == 0 and node['level'] != 0) or (index and
                        (node['level'] == 0 or node['level'] > nodes[index - 1]['level'] + 1)):
                    raise FormatError('TYPE_TREE_HIERARCHY_INVALID')
                end = index + 1
                while end < len(nodes) and nodes[end]['level'] > node['level']:
                    end += 1
                node['end'] = end
        if trees:
            if reference:
                c.z(); c.z(); c.z()
            else:
                for _ in range(c.count(4)):
                    c.num('<i')
        return dict(class_id=cid, script_index=script_index, nodes=nodes)

    types = [read_type() for _ in range(c.count(23))]
    objects, ids = [], set()
    for _ in range(c.count(24)):
        c.align()
        table_pos = c.pos
        pid, offset, size, tid = struct.unpack('<qqIi', c.take(24))
        offset += data_offset
        if pid in ids or not 0 <= tid < len(types):
            raise FormatError('OBJECT_ID_OR_TYPE_INVALID')
        if offset < data_offset or size > len(raw) - offset:
            raise FormatError('OBJECT_BOUNDARY_INVALID')
        ids.add(pid)
        objects.append(dict(path_id=pid, offset=offset, size=size, type_index=tid,
                            class_id=types[tid]['class_id'], table_pos=table_pos))
    scripts = []
    for _ in range(c.count(12)):
        fid = c.num('<i'); c.align()
        scripts.append(dict(file_id=fid, path_id=c.num('<q')))
    externals = []
    for _ in range(c.count(22)):
        c.z()
        guid, kind, path = c.take(16).hex(), c.num('<i'), c.z()
        externals.append(dict(guid=guid, kind=kind, path=path))
    for _ in range(c.count(23)):
        read_type(True)
    c.z()  # User information is opaque, but its terminator must be present.
    if c.pos != c.end:
        raise FormatError('METADATA_END_MISMATCH')
    cursor = data_offset
    for obj in sorted(objects, key=lambda o: o['offset']):
        if obj['offset'] < cursor:
            raise FormatError('OBJECT_REGIONS_OVERLAP')
        cursor = obj['offset'] + obj['size']
    return dict(version=version, platform=platform, types=types, objects=objects,
                externals=externals, scripts=scripts, data_offset=data_offset)


def tree_object(raw, obj, typ):
    """Independent metadata-directed field walk, strictly bounded to one object."""
    c = Cursor(raw, obj['offset'], obj['offset'] + obj['size'])
    nodes, strings, refs = typ['nodes'], [], []
    if not nodes:
        raise FormatError('TYPE_TREE_MISSING')

    def walk(index, path, depth=0, defer_alignment=False):
        if depth > 128:
            raise FormatError('TYPE_TREE_RECURSION_LIMIT')
        node = nodes[index]
        ty, end = node['type'], node['end']
        start = c.pos
        if ty == 'string':
            value, at, content_end, padded_end = c.string()
            strings.append(dict(field_path=path, source=value, offset=at,
                                content_end=content_end, end=padded_end))
        elif ty == 'TypelessData':
            count = c.num('<i')
            value = c.take(count)
        elif node['flags'] & 1 or ty == 'Array':
            count = c.num('<i')
            if count < 0 or count > c.end - c.pos or index + 2 >= end:
                raise FormatError('ARRAY_LAYOUT_INVALID')
            size_node, element = nodes[index + 1], index + 2
            if size_node['name'] != 'size' or nodes[element]['end'] != end:
                raise FormatError('ARRAY_LAYOUT_INVALID')
            aligned_elements = bool(nodes[element]['meta'] & 0x4000)
            value = [walk(element, path + [i], depth + 1, aligned_elements) for i in range(count)]
            if aligned_elements:
                c.align()
        elif ty in FORMATS:
            value = c.num('<' + FORMATS[ty])
        elif index + 1 < end:
            value, child = {}, index + 1
            while child < end:
                field = nodes[child]['name']
                if field in value:
                    raise FormatError('FIELD_BINDING_AMBIGUOUS')
                # Array is a wire-layout wrapper, not a public field component.
                child_path = path if nodes[child]['type'] == 'Array' else path + [field]
                value[field] = walk(child, child_path, depth + 1)
                child = nodes[child]['end']
            if ty.startswith('PPtr<'):
                if set(value) != {'m_FileID', 'm_PathID'} or c.pos - start != 12:
                    raise FormatError('PPTR_LAYOUT_INVALID')
                refs.append(dict(field_path=path, file_id=value['m_FileID'],
                                 path_id=value['m_PathID'], target_type=ty[5:-1], offset=start))
            elif len(value) == 1 and 'Array' in value:
                value = value['Array']
        elif node['size'] >= 0:
            value = c.take(node['size'])
        else:
            raise FormatError('TYPE_TREE_LAYOUT_UNSUPPORTED')
        if node['meta'] & 0x4000 and not defer_alignment:
            c.align()
        return value

    value = walk(0, [])
    if c.pos != c.end:
        raise FormatError('OBJECT_END_MISMATCH')
    return dict(value=value, strings=strings, references=refs)


def lookup_type(types, descriptor):
    if not isinstance(descriptor, dict) or descriptor.get('kind') != 'named':
        raise FormatError('MANAGED_DESCRIPTOR_UNSUPPORTED')
    key = descriptor.get('assembly', '') + '::' + descriptor.get('name', '')
    if key not in types:
        raise FormatError('MANAGED_TYPE_MISSING')
    return types[key]


def managed_fields(types, descriptor, seen=()):
    name = descriptor.get('name')
    if name in NATIVE_BASES:
        return []
    if name in NATIVE_LAYOUTS:
        return NATIVE_LAYOUTS[name]
    if name in UNSUPPORTED_NATIVE_LAYOUTS:
        raise FormatError('NATIVE_MANAGED_LAYOUT_UNSUPPORTED')
    key = (descriptor.get('assembly'), name)
    if key in seen or len(seen) > 64:
        raise FormatError('MANAGED_INHERITANCE_CYCLE')
    typ = lookup_type(types, descriptor)
    fields = managed_fields(types, typ['base'], seen + (key,)) if typ.get('base') else []
    fields = list(fields)
    for field in typ.get('fields', []):
        if field.get('serialized') and not field.get('static'):
            if any(previous['name'] == field['name'] for previous in fields):
                raise FormatError('FIELD_BINDING_AMBIGUOUS')
            fields.append(field)
    return fields


def is_object_type(types, descriptor, seen=()):
    name = descriptor.get('name')
    if name in NATIVE_LAYOUTS or name in UNSUPPORTED_NATIVE_LAYOUTS:
        return False
    if name == 'UnityEngine.Object':
        return True
    if name in NATIVE_BASES - {'UnityEngine.Object'}:
        return name.startswith('UnityEngine.')
    key = (descriptor.get('assembly'), name)
    if key in seen:
        raise FormatError('MANAGED_INHERITANCE_CYCLE')
    typ = lookup_type(types, descriptor)
    return bool(typ.get('base') and is_object_type(types, typ['base'], seen + (key,)))


def managed_object(raw, obj, types, descriptor, fields_offset):
    c = Cursor(raw, fields_offset, obj['offset'] + obj['size'])
    strings, refs = [], []

    def read(desc, path, packed=False, depth=0):
        if depth > 64 or not isinstance(desc, dict):
            raise FormatError('MANAGED_DESCRIPTOR_UNSUPPORTED')
        kind, start = desc.get('kind'), c.pos
        if kind == 'string':
            value, at, content_end, end = c.string()
            strings.append(dict(field_path=path, source=value, offset=at,
                                content_end=content_end, end=end))
            return value
        if kind == 'primitive' and desc.get('name') in PRIMITIVES:
            fmt = PRIMITIVES[desc['name']]
            value = c.num('<' + fmt)
            if not packed and struct.calcsize(fmt) < 4:
                c.align()
            return value
        if kind in ('array', 'list'):
            count = c.num('<i')
            if count < 0 or count > c.end - c.pos:
                raise FormatError('MANAGED_ARRAY_BOUNDS_INVALID')
            result = [read(desc['element'], path + [i], True, depth + 1) for i in range(count)]
            c.align()
            return result
        if kind == 'pptr' or (kind == 'named' and is_object_type(types, desc)):
            fid, pid = c.num('<i'), c.num('<q')
            refs.append(dict(field_path=path, file_id=fid, path_id=pid,
                             target_type=desc.get('name'), offset=start))
            return dict(m_FileID=fid, m_PathID=pid)
        if kind == 'named':
            name = desc.get('name')
            if name not in NATIVE_LAYOUTS and name not in UNSUPPORTED_NATIVE_LAYOUTS:
                typ = lookup_type(types, desc)
                if typ.get('enum'):
                    return c.num('<i')
            value = {}
            for field in managed_fields(types, desc):
                value[field['name']] = read(field['type'], path + [field['name']],
                                            field.get('packed', False), depth + 1)
                if field.get('align_after'):
                    c.align()
            return value
        raise FormatError('MANAGED_DESCRIPTOR_UNSUPPORTED')

    result = {f['name']: read(f['type'], [f['name']]) for f in managed_fields(types, descriptor)}
    if c.pos != c.end:
        raise FormatError('MANAGED_OBJECT_END_MISMATCH')
    return dict(value=result, strings=strings, references=refs)


def rebuild(raw, document, changed):
    """Opaque v22 relocation: never deserialize or normalize an unowned object."""
    by_id = {o['path_id']: o for o in document['objects']}
    if not set(changed) <= set(by_id) or any(not isinstance(v, bytes) for v in changed.values()):
        raise FormatError('OBJECT_BINDING_INVALID')
    if not changed:
        return raw
    data_offset = document['data_offset']
    out, cursor, positions = bytearray(raw[:data_offset]), data_offset, {}
    for obj in sorted(document['objects'], key=lambda o: o['offset']):
        start, size, pid = obj['offset'], obj['size'], obj['path_id']
        out.extend(raw[cursor:start])
        value = changed.get(pid, raw[start:start + size])
        positions[pid] = (len(out) - data_offset, len(value))
        out.extend(value)
        # Preserve existing gaps; only insert alignment needed by relocation.
        out.extend(bytes((size - len(value)) % 8))
        cursor = start + size
    out.extend(raw[cursor:])
    for obj in document['objects']:
        struct.pack_into('<qI', out, obj['table_pos'] + 8, *positions[obj['path_id']])
    struct.pack_into('>Q', out, 24, len(out))
    return bytes(out)


def _decode_block(data, codec, size):
    if codec == 0:
        result = data
    elif codec == 1:
        if len(data) < 5:
            raise FormatError('LZMA_HEADER_INVALID')
        prop, dictionary = data[0], struct.unpack_from('<I', data, 1)[0]
        if prop >= 225 or dictionary > (1 << 28):
            raise FormatError('LZMA_PROPERTIES_UNSUPPORTED')
        lc, rest = prop % 9, prop // 9
        lp, pb = rest % 5, rest // 5
        try:
            decoder = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[
                dict(id=lzma.FILTER_LZMA1, dict_size=max(dictionary, 4096), lc=lc, lp=lp, pb=pb)])
            result = decoder.decompress(data[5:], max_length=size + 1)
        except (lzma.LZMAError, ValueError) as exc:
            raise FormatError('LZMA_DATA_INVALID') from exc
        if decoder.unused_data:
            raise FormatError('LZMA_TRAILING_DATA')
    elif codec in (2, 3):
        try:
            if importlib.metadata.version('lz4') != '4.4.5':
                raise FormatError('LZ4_VERSION_NOT_BOUND')
            import lz4.block
        except importlib.metadata.PackageNotFoundError as exc:
            raise FormatError('LZ4_DEPENDENCY_UNAVAILABLE') from exc
        except ImportError as exc:
            raise FormatError('LZ4_DEPENDENCY_UNAVAILABLE') from exc
        try:
            result = lz4.block.decompress(data, uncompressed_size=size)
        except Exception as exc:
            raise FormatError('LZ4_DATA_INVALID') from exc
    else:
        raise FormatError('BUNDLE_CODEC_UNSUPPORTED')
    if len(result) != size:
        raise FormatError('BUNDLE_DECOMPRESSED_SIZE_MISMATCH')
    return result


def bundle(raw):
    """Read UnityFS v6–8 entries. No archive paths are written to the filesystem."""
    c = Cursor(raw)
    if c.z() != 'UnityFS':
        raise FormatError('BUNDLE_SIGNATURE_UNSUPPORTED')
    version = c.num('>I')
    if version not in (6, 7, 8):
        raise FormatError('BUNDLE_VERSION_UNSUPPORTED')
    unity_version, revision = c.z(), c.z()
    declared, compressed, uncompressed, flags = (c.num('>Q'), c.num('>I'), c.num('>I'), c.num('>I'))
    if declared != len(raw) or not flags & 0x40 or flags & ~0x3ff:
        raise FormatError('BUNDLE_HEADER_UNSUPPORTED')
    if version >= 7:
        c.align(16)
    header_end = c.pos
    info_start = len(raw) - compressed if flags & 0x80 else c.pos
    if not header_end <= info_start <= len(raw) or compressed > len(raw) - info_start:
        raise FormatError('BUNDLE_INFO_BOUNDS_INVALID')
    # Prevent attacker-declared unbounded decompression before decoding.
    if uncompressed > (1 << 28):
        raise FormatError('BUNDLE_INFO_SIZE_UNSUPPORTED')
    info = _decode_block(raw[info_start:info_start + compressed], flags & 0x3f, uncompressed)
    d = Cursor(info)
    digest = d.take(16)
    block_count = d.num('>i')
    if block_count < 0 or block_count > (d.end - d.pos) // 10:
        raise FormatError('BUNDLE_BLOCK_COUNT_INVALID')
    blocks = [struct.unpack('>IIH', d.take(10)) for _ in range(block_count)]
    if sum(plain_size for plain_size, _, _ in blocks) > (1 << 30):
        raise FormatError('BUNDLE_PAYLOAD_SIZE_UNSUPPORTED')
    entry_count = d.num('>i')
    if entry_count < 0 or entry_count > (d.end - d.pos) // 21:
        raise FormatError('BUNDLE_ENTRY_COUNT_INVALID')
    entries, names = [], set()
    for _ in range(entry_count):
        offset, size, entry_flags, name = d.num('>q'), d.num('>q'), d.num('>I'), d.z()
        if name in names or offset < 0 or size < 0:
            raise FormatError('BUNDLE_ENTRY_BINDING_AMBIGUOUS')
        names.add(name)
        entries.append(dict(name=name, offset=offset, size=size, flags=entry_flags))
    if d.pos != d.end:
        raise FormatError('BUNDLE_INFO_END_MISMATCH')
    pos = header_end if flags & 0x80 else info_start + compressed
    if flags & 0x200:
        pos = (pos + 15) & ~15
    data_end = info_start if flags & 0x80 else len(raw)
    payload = bytearray()
    for plain_size, compressed_size, block_flags in blocks:
        if block_flags & ~0x7f or compressed_size > data_end - pos or plain_size > (1 << 30):
            raise FormatError('BUNDLE_BLOCK_BOUNDS_INVALID')
        payload.extend(_decode_block(raw[pos:pos + compressed_size], block_flags & 0x3f, plain_size))
        pos += compressed_size
    if pos != data_end:
        raise FormatError('BUNDLE_DATA_END_MISMATCH')
    cursor = 0
    for entry in sorted(entries, key=lambda e: e['offset']):
        if entry['offset'] < cursor or entry['size'] > len(payload) - entry['offset']:
            raise FormatError('BUNDLE_ENTRY_BOUNDS_INVALID')
        cursor = entry['offset'] + entry['size']
    return dict(version=version, unity_version=unity_version, revision=revision,
                entries=entries, payload=bytes(payload), flags=flags, digest=digest)


def rebuild_bundle(raw, document, changed):
    if not changed:
        return raw
    if not set(changed) <= {e['name'] for e in document['entries']}:
        raise FormatError('BUNDLE_ENTRY_BINDING_MISSING')
    if any(not isinstance(value, bytes) for value in changed.values()):
        raise FormatError('BUNDLE_ENTRY_PAYLOAD_INVALID')
    original, payload, cursor, positions = document['payload'], bytearray(), 0, {}
    for entry in sorted(document['entries'], key=lambda e: e['offset']):
        start, size, name = entry['offset'], entry['size'], entry['name']
        payload.extend(original[cursor:start])
        value = changed.get(name, original[start:start + size])
        positions[name] = (len(payload), len(value))
        payload.extend(value)
        cursor = start + size
    payload.extend(original[cursor:])
    # UnityFS allows zero hash (unspecified). Never retain a stale content hash.
    info = bytearray(bytes(16))
    chunk_size = 16 * 1024 * 1024
    blocks = [payload[p:p + chunk_size] for p in range(0, len(payload), chunk_size)]
    info.extend(struct.pack('>i', len(blocks)))
    for block in blocks:
        info.extend(struct.pack('>IIH', len(block), len(block), 0))
    info.extend(struct.pack('>i', len(document['entries'])))
    for entry in document['entries']:
        info.extend(struct.pack('>qqI', *positions[entry['name']], entry['flags']))
        info.extend(entry['name'].encode('utf-8') + b'\0')
    header = bytearray(b'UnityFS\0' + struct.pack('>I', document['version']))
    header.extend(document['unity_version'].encode('utf-8') + b'\0')
    header.extend(document['revision'].encode('utf-8') + b'\0')
    size_at = len(header)
    flags = document['flags'] & ~(0x3f | 0x80)
    header.extend(struct.pack('>QIII', 0, len(info), len(info), flags))
    if document['version'] >= 7:
        header.extend(bytes((-len(header)) % 16))
    header.extend(info)
    if flags & 0x200:
        header.extend(bytes((-len(header)) % 16))
    header.extend(payload)
    struct.pack_into('>Q', header, size_at, len(header))
    return bytes(header)
