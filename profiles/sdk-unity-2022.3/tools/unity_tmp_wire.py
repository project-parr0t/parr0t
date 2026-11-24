"""Exact wire spans and non-destructive object appends for offline TMP assets."""
from __future__ import annotations

from functools import lru_cache
import importlib.metadata
import re
import struct

import unity_raw as reader
from unity_objects import _descriptor, _native_object, _raw_documents

FormatError = reader.FormatError
_VERSION = re.compile(r'2022\.3\.(\d+)([abfp])(\d+)(?:c\d+)?', re.ASCII)


@lru_cache(maxsize=64)
def _version(value):
    match = _VERSION.fullmatch(value)
    if not match:
        raise FormatError('TMP_UNITY_VERSION_UNSUPPORTED')
    patch, release, build = match.groups()
    release_type = {'a': 0, 'b': 1, 'f': 3, 'p': 4}[release]
    if int(patch) > 0xffff or int(build) > 0xff:
        raise FormatError('TMP_UNITY_VERSION_UNSUPPORTED')
    return 2022, 3, int(patch), (release_type << 8) | int(build)


@lru_cache(maxsize=64)
def _native_tree(class_id, version):
    # Use the exact SDK-pinned release schema only for native stripped objects.
    try:
        if importlib.metadata.version('UnityPy') != '1.25.4':
            raise FormatError('UNITYPY_VERSION_NOT_BOUND')
        from UnityPy.helpers.Tpk import get_typetree_node
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        raise FormatError('UNITYPY_DEPENDENCY_UNAVAILABLE') from exc
    nodes = []

    def flatten(node, level):
        item = dict(level=level, type=node.m_Type, name=node.m_Name,
                    size=node.m_ByteSize, flags=node.m_TypeFlags or 0,
                    meta=node.m_MetaFlag or 0)
        nodes.append(item)
        for child in node.m_Children:
            flatten(child, level + 1)
        item['end'] = len(nodes)

    try:
        flatten(get_typetree_node(class_id, version), 0)
    except (KeyError, ValueError, AssertionError) as exc:
        raise FormatError('TMP_NATIVE_LAYOUT_UNSUPPORTED') from exc
    return {'nodes': nodes}


def pointer(value):
    if (not isinstance(value, dict) or set(value) != {'m_FileID', 'm_PathID'}
            or type(value['m_FileID']) is not int or value['m_FileID'] < 0
            or type(value['m_PathID']) is not int):
        raise FormatError('TMP_POINTER_INVALID')
    fid, pid = value['m_FileID'], value['m_PathID']
    if fid and not pid:
        raise FormatError('TMP_POINTER_INVALID')
    return fid, pid


def require_references(data, fields):
    refs = {tuple(ref['field_path']): (ref['file_id'], ref['path_id']) for ref in data['references']}
    for path, value in fields:
        if refs.get(path) != pointer(value):
            raise FormatError('TMP_REFERENCE_FIELD_UNBOUND')


def pptr(pid):
    return struct.pack('<iq', 0, pid)


class Container:
    def __init__(self, raw, managed_types):
        if not isinstance(raw, bytes):
            raise FormatError('TMP_SERIALIZED_BYTES_REQUIRED')
        self.raw = raw
        self.types = managed_types or {}
        documents, self.archive = _raw_documents(raw)
        self.entries = {}
        for entry, payload, document in documents:
            if entry in self.entries:
                raise FormatError('TMP_CONTAINER_ENTRY_AMBIGUOUS')
            self.entries[entry] = (payload, document, {o['path_id']: o for o in document['objects']})
        self._decoded = {}
        self._behaviours = {}

    def object(self, key, class_id):
        entry, pid = key
        if entry not in self.entries:
            raise FormatError('TMP_CONTAINER_ENTRY_MISSING')
        payload, document, by_id = self.entries[entry]
        obj = by_id.get(pid)
        if obj is None:
            raise FormatError('TMP_OBJECT_MISSING')
        if obj['class_id'] != class_id:
            raise FormatError('TMP_OBJECT_CLASS_MISMATCH')
        _version(document['version'])
        return payload, obj, document['types'][obj['type_index']]

    def target(self, key, value, class_id, *, nullable=False):
        fid, pid = pointer(value)
        if not pid:
            if nullable:
                return None
            raise FormatError('TMP_POINTER_NULL')
        entry = key[0]
        if fid:
            externals = self.entries[entry][1]['externals']
            if fid > len(externals):
                raise FormatError('TMP_EXTERNAL_INDEX_INVALID')
            path = externals[fid - 1]['path'].replace('\\', '/')
            matches = [name for name in self.entries if name is not None and (name == path or
                       (path.startswith('archive:/') and name == path.rsplit('/', 1)[-1]))]
            if len(matches) != 1:
                raise FormatError('TMP_EXTERNAL_REFERENCE_UNSUPPORTED')
            entry = matches[0]
        target = (entry, pid)
        self.object(target, class_id)
        return target

    def _native(self, key, class_id):
        payload, obj, typ = self.object(key, class_id)
        if not typ['nodes']:
            version = self.entries[key[0]][1]['version']
            typ = _native_tree(class_id, _version(version))
        return payload, obj, typ

    def native_data(self, key, class_id):
        if key not in self._decoded:
            self._decoded[key] = reader.tree_object(*self._native(key, class_id))
        return self._decoded[key]

    def native(self, key, class_id):
        return self.native_data(key, class_id)['value']

    def behaviour(self, key, names, *, allow_prefix=False):
        cache_key = key, names, allow_prefix
        if cache_key in self._behaviours:
            return self._behaviours[cache_key]
        payload, obj, typ = self.object(key, 114)
        prefix = reader.tree_object(payload, obj, typ) if typ['nodes'] else _native_object(payload, obj)
        script_ref = prefix['value'].get('m_Script')
        require_references(prefix, [(('m_Script',), script_ref)])
        script_key = self.target(key, script_ref, 115)
        script_raw, script_obj, script_typ = self.object(script_key, 115)
        script = (reader.tree_object(script_raw, script_obj, script_typ) if script_typ['nodes']
                  else _native_object(script_raw, script_obj))
        descriptor = _descriptor(script['value'])
        if descriptor['assembly'] != 'Unity.TextMeshPro' or descriptor['name'] not in names:
            raise FormatError('TMP_SCRIPT_TYPE_MISMATCH')
        result = prefix, None
        if not typ['nodes']:
            start = prefix['fields_offset']
            data = reader.managed_object(payload, obj, self.types, descriptor, start, allow_prefix=allow_prefix)
            result = data, (descriptor, start)
        self._behaviours[cache_key] = result
        return result

    def rewrite(self, key, class_id, changes, wire=None):
        if wire is not None:
            payload, obj, _ = self.object(key, class_id)
            slots = _managed_spans(payload, obj, self.types, *wire, set(changes))
        else:
            payload, obj, typ = self._native(key, class_id)
            slots = _tree_spans(payload, obj, typ['nodes'], set(changes))
        if set(slots) != set(changes):
            raise FormatError('TMP_WRITE_FIELD_UNBOUND')
        payload = memoryview(payload)
        cursor, parts = obj['offset'], []
        for start, end, new, old_width in sorted((*slots[path], *value) for path, value in changes.items()):
            size = end - start
            old_padding = size - old_width if old_width is not None else 0
            if start < cursor or not 0 <= old_padding < 4:
                raise FormatError('TMP_WRITE_SPAN_INVALID')
            new_padding = (size - len(new)) % 4
            parts.extend((payload[cursor:start], new))
            if new_padding:
                parts.append(payload[end - old_padding:end] if old_padding == new_padding
                             else bytes(new_padding))
            cursor = end
        parts.append(payload[cursor:obj['offset'] + obj['size']])
        return b''.join(parts)

    def append(self, entry, replacement_id, replacement, additions):
        payload, _, objects = self.entries[entry]
        built = _append(payload, objects, replacement_id, replacement, additions)
        if self.archive is None:
            return built
        return reader.rebuild_bundle(self.raw, self.archive, {entry: built})


def _tree_spans(raw, obj, nodes, wanted):
    """Use the same paths/alignment as tree_object; retain bytes outside them."""
    c = reader.Cursor(memoryview(raw), obj['offset'], obj['offset'] + obj['size'])
    slots = {}

    def walk(index, path, depth=0, defer_alignment=False):
        if depth > 128:
            raise FormatError('TMP_TREE_DEPTH_LIMIT')
        node = nodes[index]
        ty, end, start = node['type'], node['end'], c.pos
        if ty == 'string':
            c.take(c.num('<i'))
            c.align()
        elif ty == 'TypelessData':
            c.take(c.num('<i'))
        elif node['flags'] & 1 or ty == 'Array':
            count = c.count()
            if index + 2 >= end or nodes[index + 1]['name'] != 'size' or nodes[index + 2]['end'] != end:
                raise FormatError('ARRAY_LAYOUT_INVALID')
            element = index + 2
            align_elements = bool(nodes[element]['meta'] & 0x4000)
            elem_fmt = reader.FORMATS.get(nodes[element]['type'])
            if elem_fmt and not any(path == p[:len(path)] and len(path) < len(p) for p in wanted):
                c.take(count * struct.calcsize(elem_fmt))
            else:
                for i in range(count):
                    walk(element, (*path, i), depth + 1, align_elements)
            if align_elements:
                c.align()
        elif ty in reader.FORMATS:
            c.take(struct.calcsize(reader.FORMATS[ty]))
        elif index + 1 < end:
            child = index + 1
            while child < end:
                field = nodes[child]
                child_path = path if field['type'] == 'Array' else (*path, field['name'])
                walk(child, child_path, depth + 1)
                child = field['end']
        elif node['size'] >= 0:
            c.take(node['size'])
        else:
            raise FormatError('TYPE_TREE_LAYOUT_UNSUPPORTED')
        if node['meta'] & 0x4000 and not defer_alignment:
            c.align()
        if path in wanted:
            # An Array wrapper shares its parent's public path. The outer
            # span includes its alignment and replaces the inner candidate.
            slots[path] = (start, c.pos)

    walk(0, ())
    if c.pos != c.end:
        raise FormatError('OBJECT_END_MISMATCH')
    return slots


def _managed_spans(raw, obj, types, descriptor, start, wanted):
    """Locate whole typed managed fields after a successful managed_object read."""
    c = reader.Cursor(memoryview(raw), start, obj['offset'] + obj['size'])
    slots = {}

    def walk(desc, path, packed=False, depth=0):
        if depth > 64:
            raise FormatError('MANAGED_DESCRIPTOR_UNSUPPORTED')
        kind, start = desc['kind'], c.pos
        if reader.empty_cull_event(desc):
            if c.num('<i') == 0:
                if path in wanted:
                    slots[path] = (start, c.pos)
                return
            c.pos = start
        if kind == 'string':
            c.take(c.num('<i'))
            c.align()
        elif kind == 'primitive' and desc.get('name') in reader.PRIMITIVES:
            size = struct.calcsize(reader.PRIMITIVES[desc['name']])
            c.take(size)
            if not packed and size < 4:
                c.align()
        elif kind in ('list', 'array'):
            count = c.count()
            for i in range(count):
                walk(desc['element'], (*path, i), True, depth + 1)
            c.align()
        elif kind == 'pptr' or (kind == 'named' and reader.is_object_type(types, desc)):
            c.take(12)
        elif kind == 'named':
            name = desc['name']
            if (name not in reader.NATIVE_LAYOUTS and name not in reader.UNSUPPORTED_NATIVE_LAYOUTS
                    and reader.lookup_type(types, desc).get('enum')):
                c.take(4)
            else:
                fields(desc, path, depth)
        else:
            raise FormatError('MANAGED_DESCRIPTOR_UNSUPPORTED')
        if path in wanted:
            slots[path] = (start, c.pos)

    def fields(desc, path, depth):
        for field in reader.managed_fields(types, desc):
            walk(field['type'], (*path, field['name']), field.get('packed', False), depth + 1)
            if field.get('align_after'):
                c.align()

    fields(descriptor, (), 0)
    if c.pos != c.end:
        raise FormatError('MANAGED_OBJECT_END_MISMATCH')
    return slots


def replace_fields(payload, obj, references, changes):
    """Patch only exact twelve-byte decoded renderer pointers, including prefixes."""
    start, end = obj['offset'], obj['offset'] + obj['size']
    body = bytearray(payload[start:end])
    for field, pid in changes.items():
        matches = [r for r in references if r['field_path'] == [field]]
        if len(matches) != 1:
            raise FormatError('TMP_RENDERER_POINTER_UNBOUND')
        ref, = matches
        offset = ref['offset'] - start
        before = struct.pack('<iq', ref['file_id'], ref['path_id'])
        if offset < 0 or body[offset:offset + 12] != before:
            raise FormatError('TMP_RENDERER_POINTER_UNBOUND')
        body[offset:offset + 12] = pptr(pid)
    return bytes(body)


def _append(raw, objects, replacement_id, replacement, additions):
    """Append v22 records without changing any prior object, except fixed-width renderer pointers."""
    ordered = sorted(objects.values(), key=lambda obj: obj['table_pos'])
    first, last = ordered[0]['table_pos'], ordered[-1]['table_pos'] + 24
    if last != first + 24 * len(ordered):
        raise FormatError('TMP_OBJECT_TABLE_NOT_CONTIGUOUS')
    count = struct.pack('<i', len(ordered))
    matches = [at for at in range(first - 7, first - 3) if raw[at:at + 4] == count]
    if len(matches) != 1:
        raise FormatError('TMP_OBJECT_COUNT_UNBOUND')
    count_at, = matches
    metadata_size, _, data_offset = struct.unpack_from('>IQQ', raw, 20)
    metadata_end = 48 + metadata_size
    replaced = objects[replacement_id]
    if len(replacement) != replaced['size']:
        raise FormatError('TMP_RENDERER_SIZE_CHANGED')
    source = memoryview(raw)
    data_size, start = len(raw) - data_offset, replaced['offset']
    data = [source[data_offset:start], replacement, source[start + len(replacement):]]
    records = bytearray()
    for pid, type_index, body in additions:
        pad = (-data_size) % 8
        data_size += pad
        records.extend(struct.pack('<qqIi', pid, data_size, len(body), type_index))
        data.extend((bytes(pad), body))
        data_size += len(body)
    header = bytearray(source[:count_at])
    header.extend(struct.pack('<i', len(ordered) + len(additions)))
    header.extend(source[count_at + 4:last])
    header.extend(records)
    header.extend(source[last:metadata_end])
    header.extend(source[metadata_end:data_offset])
    header.extend(bytes((-len(records)) % 16))
    struct.pack_into('>IQQ', header, 20, metadata_size + len(records), len(header) + data_size, len(header))
    return b''.join((header, *data))
