"""Source-bound serialized inventories and opaque safe string replacement.

Primary uses the explicitly bound UnityPy release. Independent uses public bytes
and first-party readers only (LZ4 is lazy and version-bound for compressed blocks).
Neither path asks for texture decoding, pixels, OCR, or runtime observations.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import posixpath
import struct

import unity_raw as raw_reader

FormatError = raw_reader.FormatError
_UI_TYPES = frozenset({'UnityEngine.UI.Text', 'UnityEngine.UI.InputField',
                       'TMPro.TMP_InputField', 'TMPro.TMP_Text',
                       'TMPro.TextMeshPro', 'TMPro.TextMeshProUGUI'})
_UI_REFERENCE_FIELDS = frozenset({'m_Font', 'm_FontAsset', 'm_Material',
    'm_fontAsset', 'm_sharedMaterial', 'm_TextComponent', 'm_Placeholder',
    'm_textComponent', 'm_placeholder'})
_NATIVE_TEXT = {102: {'m_Text'}, 132: {'m_Text'}, 129: {'productName'}}
_PIXEL_OBJECT_CLASSES = frozenset({27, 28, 84, 89, 117, 187, 188})


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _load_primary(raw):
    try:
        if importlib.metadata.version('UnityPy') != '1.25.4':
            raise FormatError('UNITYPY_VERSION_NOT_BOUND')
        import UnityPy
    except importlib.metadata.PackageNotFoundError as exc:
        raise FormatError('UNITYPY_DEPENDENCY_UNAVAILABLE') from exc
    except ImportError as exc:
        raise FormatError('UNITYPY_DEPENDENCY_UNAVAILABLE') from exc
    try:
        return UnityPy.load(raw)
    except Exception as exc:
        raise FormatError('PRIMARY_CONTAINER_PARSE_FAILED') from exc


def _location(obj, path, entry=None, field_index=None):
    value = dict(object_id=int(obj['path_id']), field_path=list(path),
                 object_index=obj['object_index'])
    if entry is not None:
        value['container_entry'] = entry
    if field_index is not None:
        value['field_index'] = field_index
    return value


def _native_object(raw, obj):
    """Public stripped player layouts with no dependency on class-name guessing."""
    c = raw_reader.Cursor(raw, obj['offset'], obj['offset'] + obj['size'])
    values, strings, references = {}, [], []

    def text(field):
        value, at, content_end, end = c.string()
        values[field] = value
        strings.append(dict(field_path=[field], source=value, offset=at,
                            content_end=content_end, end=end))
    cid = obj['class_id']
    if cid == 114:
        game = dict(m_FileID=c.num('<i'), m_PathID=c.num('<q'))
        c.num('<B'); c.align()
        at = c.pos
        script = dict(m_FileID=c.num('<i'), m_PathID=c.num('<q'))
        values.update(m_GameObject=game, m_Script=script)
        references.append(dict(field_path=['m_GameObject'], file_id=game['m_FileID'],
                               path_id=game['m_PathID'], target_type='UnityEngine.GameObject',
                               offset=obj['offset']))
        references.append(dict(field_path=['m_Script'], file_id=script['m_FileID'],
                               path_id=script['m_PathID'], target_type='UnityEngine.MonoScript', offset=at))
        text('m_Name')
        return dict(value=values, strings=strings, references=references, fields_offset=c.pos)
    if cid == 115:
        text('m_Name')
        c.num('<i'); c.take(16)
        for field in ('m_ClassName', 'm_Namespace', 'm_AssemblyName'):
            text(field)
    elif cid == 49:
        text('m_Name'); text('m_Script')
    else:
        raise FormatError('NATIVE_LAYOUT_UNSUPPORTED')
    if c.pos != c.end:
        raise FormatError('OBJECT_END_MISMATCH')
    return dict(value=values, strings=strings, references=references)


def _raw_documents(raw):
    def parse(payload):
        document = raw_reader.serialized(payload)
        document['objects'].sort(key=lambda obj: obj['offset'])
        return document

    if raw.startswith(b'UnityFS\0'):
        archive = raw_reader.bundle(raw)
        documents = []
        for entry in archive['entries']:
            payload = archive['payload'][entry['offset']:entry['offset'] + entry['size']]
            if len(payload) >= 12 and struct.unpack_from('>I', payload, 8)[0] == 22:
                documents.append((entry['name'], payload, parse(payload)))
            elif entry['flags'] & 4:
                raise FormatError('SERIALIZED_VERSION_UNSUPPORTED')
        return documents, archive
    return [(None, raw, parse(raw))], None


def _primary_documents(raw):
    env = _load_primary(raw)
    documents = []

    def visit(file, entry=None):
        objects = getattr(file, 'objects', None)
        if isinstance(objects, dict):
            header = getattr(file, 'header', None)
            if header is not None:
                length = file.reader.Length
                if header.version != 22 or header.endian != '<':
                    raise FormatError('SERIALIZED_LAYOUT_UNSUPPORTED')
                if (header.file_size != length or header.data_offset < 48 or
                        48 + header.metadata_size > header.data_offset or header.data_offset > length):
                    raise FormatError('SERIALIZED_HEADER_INVALID')
                cursor = header.data_offset
                for value in sorted(objects.values(), key=lambda value: value.byte_start):
                    if value.byte_start < cursor or value.byte_size > length - value.byte_start:
                        raise FormatError('OBJECT_BOUNDARY_INVALID')
                    cursor = value.byte_start + value.byte_size
            rows = []
            for index, obj in enumerate(objects.values()):
                cid = int(getattr(obj.type, 'value', obj.type))
                rows.append(dict(path_id=int(obj.path_id), class_id=cid,
                                 object_index=index, reader=obj))
            rows.sort(key=lambda row: getattr(row['reader'], 'byte_start', row['object_index']))
            external = []
            for value in getattr(file, 'externals', []):
                external.append(dict(path=str(getattr(value, 'path', ''))))
            documents.append((entry, None, dict(objects=rows, externals=external,
                                                serialized_file=file)))
            return
        children = getattr(file, 'files', None)
        if isinstance(children, dict):
            for name, child in children.items():
                visit(child, str(name))

    root = getattr(env, 'file', None)
    if root is not None:
        visit(root)
    else:
        for name, file in getattr(env, 'files', {}).items():
            visit(file, None if len(env.files) == 1 else str(name))
    if not documents:
        raise FormatError('SERIALIZED_CONTAINER_UNSUPPORTED')
    return documents


def _primary_value(obj):
    """UnityPy parses values; this walk creates identities, never raw-text guesses."""
    reader = obj['reader']
    serialized_type = getattr(reader, 'serialized_type', None)
    if serialized_type is None:
        serialized_type = getattr(reader, 'serialized_file', None)
    node = getattr(serialized_type, 'node', None)
    nodes = getattr(serialized_type, 'nodes', None)
    if node is None and not nodes:
        # Do not invoke UnityPy's missing-tree generator or external discovery.
        raise FormatError('TYPE_TREE_MISSING')
    try:
        value = reader.read_typetree()
    except Exception as exc:
        raise FormatError('PRIMARY_OBJECT_PARSE_FAILED') from exc
    strings, refs = [], []

    def walk(item, path, schema, depth=0):
        if depth > 128:
            raise FormatError('TYPE_TREE_RECURSION_LIMIT')
        wire_type = getattr(schema, 'm_Type', '')
        children = getattr(schema, 'm_Children', ())
        if isinstance(item, str):
            try:
                item.encode('utf-8')
            except UnicodeEncodeError as exc:
                raise FormatError('STRING_UTF8_INVALID') from exc
            strings.append(dict(field_path=path, source=item))
        elif isinstance(item, dict):
            if wire_type.startswith('PPtr<'):
                if (set(item) != {'m_FileID', 'm_PathID'} or
                        any(type(item[key]) is not int for key in item)):
                    raise FormatError('PPTR_LAYOUT_INVALID')
                refs.append(dict(field_path=path, file_id=item['m_FileID'],
                                 path_id=item['m_PathID'], target_type=wire_type[5:-1]))
            else:
                fields = {getattr(child, 'm_Name', None): child for child in children}
                if len(fields) != len(children):
                    raise FormatError('FIELD_BINDING_AMBIGUOUS')
                for field, child in item.items():
                    walk(child, path + [field], fields.get(field), depth + 1)
        elif isinstance(item, (list, tuple)):
            if wire_type == 'pair':
                if len(item) != 2 or len(children) != 2:
                    raise FormatError('PAIR_LAYOUT_INVALID')
                for child, element in zip(children, item):
                    walk(element, path + [child.m_Name], child, depth + 1)
                return
            array = next((child for child in children if getattr(child, 'm_Type', '') == 'Array'), schema)
            elements = getattr(array, 'm_Children', ())
            element = next((child for child in elements if getattr(child, 'm_Name', '') == 'data'), None)
            for index, child in enumerate(item):
                walk(child, path + [index], element, depth + 1)
    walk(value, [], node)
    return dict(value=value, strings=strings, references=refs)


def _primary_stripped(obj, types, descriptor=None):
    """A second, sequential descriptor interpreter for primary missing-tree files.

    This does not consume independent parse results. It receives object bytes and
    UnityPy's object boundary, and obtains managed layout only from CLR metadata.
    """
    payload = obj['reader'].get_raw_data()
    p, strings, references = 0, [], []

    def take(n):
        nonlocal p
        if n < 0 or p + n > len(payload):
            raise FormatError('BOUNDARY_INVALID')
        out = payload[p:p + n]; p += n
        return out

    def num(fmt):
        return struct.unpack('<' + fmt, take(struct.calcsize(fmt)))[0]

    def align():
        take((-p) % 4)

    def string(path):
        data = take(num('i'))
        try:
            result = data.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise FormatError('STRING_UTF8_INVALID') from exc
        align()
        strings.append(dict(field_path=path, source=result))
        return result

    def reference(path, name):
        fid, pid = num('i'), num('q')
        references.append(dict(field_path=path, file_id=fid, path_id=pid, target_type=name))
        return dict(m_FileID=fid, m_PathID=pid)

    def read(desc, path, packed=False, depth=0):
        if depth > 64:
            raise FormatError('MANAGED_DESCRIPTOR_UNSUPPORTED')
        kind = desc.get('kind')
        if kind == 'string':
            return string(path)
        if kind == 'primitive' and desc.get('name') in raw_reader.PRIMITIVES:
            fmt = raw_reader.PRIMITIVES[desc['name']]
            value = num(fmt)
            if not packed and struct.calcsize(fmt) < 4:
                align()
            return value
        if kind in ('array', 'list'):
            count = num('i')
            if count < 0 or count > len(payload) - p:
                raise FormatError('MANAGED_ARRAY_BOUNDS_INVALID')
            values = [read(desc['element'], path + [i], True, depth + 1) for i in range(count)]
            align(); return values
        if kind == 'pptr' or (kind == 'named' and raw_reader.is_object_type(types, desc)):
            return reference(path, desc.get('name'))
        if kind == 'named':
            name = desc.get('name')
            if name not in raw_reader.NATIVE_LAYOUTS and name not in raw_reader.UNSUPPORTED_NATIVE_LAYOUTS:
                typ = raw_reader.lookup_type(types, desc)
                if typ.get('enum'):
                    return num('i')
            value = {}
            for field in raw_reader.managed_fields(types, desc):
                value[field['name']] = read(field['type'], path + [field['name']],
                                            field.get('packed', False), depth + 1)
                if field.get('align_after'):
                    align()
            return value
        raise FormatError('MANAGED_DESCRIPTOR_UNSUPPORTED')

    cid = obj['class_id']
    if cid == 114:
        values = {'m_GameObject': reference(['m_GameObject'], 'UnityEngine.GameObject')}
        num('B'); align()
        values['m_Script'] = reference(['m_Script'], 'UnityEngine.MonoScript')
        values['m_Name'] = string(['m_Name'])
        if descriptor is None:
            return dict(value=values, strings=strings, references=references)
        for field in raw_reader.managed_fields(types, descriptor):
            values[field['name']] = read(field['type'], [field['name']])
    elif cid == 49:
        values = {'m_Name': string(['m_Name']), 'm_Script': string(['m_Script'])}
    elif cid == 115:
        values = {'m_Name': string(['m_Name'])}
        num('i'); take(16)
        for field in ('m_ClassName', 'm_Namespace', 'm_AssemblyName'):
            values[field] = string([field])
    else:
        raise FormatError('NATIVE_LAYOUT_UNSUPPORTED')
    if p != len(payload):
        raise FormatError('OBJECT_END_MISMATCH')
    return dict(value=values, strings=strings, references=references)


def _descriptor(script):
    assembly = script['m_AssemblyName']
    if assembly.lower().endswith('.dll'):
        assembly = assembly[:-4]
    namespace = script['m_Namespace']
    name = (namespace + '.' if namespace else '') + script['m_ClassName']
    return dict(kind='named', name=name, assembly=assembly)


def _ui_type(types, descriptor, seen=()):
    if not descriptor:
        return False
    name = descriptor.get('name')
    if name in _UI_TYPES:
        return True
    key = (descriptor.get('assembly'), name)
    if key in seen or name in raw_reader.NATIVE_BASES:
        return False
    typ = raw_reader.lookup_type(types, descriptor)
    return bool(typ.get('base') and _ui_type(types, typ['base'], seen + (key,)))


def _field_binding(types, descriptor, path):
    """Resolve declaration identity through nested/array descriptors, not names alone."""
    if not descriptor:
        return None
    desc, bound = descriptor, None
    for component in path:
        if isinstance(component, int):
            if desc.get('kind') not in ('array', 'list'):
                return None
            desc = desc['element']
        else:
            if desc.get('kind') != 'named':
                return None
            fields = raw_reader.managed_fields(types, desc)
            found = [field for field in fields if field['name'] == component]
            if len(found) != 1:
                return None
            bound, desc = found[0], found[0]['type']
    return bound


def _json_strings(text):
    """Strict JSON structural locators, including duplicates and empty values."""
    decoder = json.JSONDecoder()
    result, p = [], 0

    def ws():
        nonlocal p
        while p < len(text) and text[p] in ' \t\r\n':
            p += 1

    def value(path, depth=0):
        nonlocal p
        if depth > 128:
            raise FormatError('TEXTASSET_JSON_DEPTH_UNSUPPORTED')
        ws()
        if p >= len(text):
            raise ValueError('json')
        if text[p] == '{':
            p += 1; ws(); keys = set()
            if p < len(text) and text[p] == '}':
                p += 1; return
            while True:
                ws(); key, end = decoder.raw_decode(text, p)
                if not isinstance(key, str) or key in keys:
                    raise ValueError('json_ambiguous_key')
                keys.add(key); p = end; ws()
                if p >= len(text) or text[p] != ':':
                    raise ValueError('json')
                p += 1; value(path + [key], depth + 1); ws()
                if p < len(text) and text[p] == '}':
                    p += 1; return
                if p >= len(text) or text[p] != ',':
                    raise ValueError('json')
                p += 1
        elif text[p] == '[':
            p += 1; ws(); index = 0
            if p < len(text) and text[p] == ']':
                p += 1; return
            while True:
                value(path + [index], depth + 1); index += 1; ws()
                if p < len(text) and text[p] == ']':
                    p += 1; return
                if p >= len(text) or text[p] != ',':
                    raise ValueError('json')
                p += 1
        else:
            start = p
            item, p = decoder.raw_decode(text, p)
            if isinstance(item, str):
                result.append(dict(parser='json', parser_path=path, source=item,
                                   char_start=start, char_end=p))
    value([]); ws()
    if p != len(text):
        raise ValueError('json')
    return result


def _table_strings(text):
    # JSON decoding is syntax-bound; never infer a locale or display role.
    if text.lstrip().startswith(('{', '[')):
        try:
            return _json_strings(text)
        except (ValueError, RecursionError):
            pass
    # INI requires an explicit section. Duplicate keys retain physical entry index.
    result, section, offset, index = [], None, 0, 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip('\r\n')
        stripped = body.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            section = stripped[1:-1]
        elif section is not None and stripped and not stripped.startswith((';', '#')) and '=' in body:
            equals = body.index('=')
            source = body[equals + 1:]
            result.append(dict(parser='ini', parser_path=[section, body[:equals].strip(), index],
                               source=source, char_start=offset + equals + 1,
                               char_end=offset + len(body)))
            index += 1
        offset += len(line)
    return result


def _primary_table_strings(text):
    """Independent stdlib JSON materialization; no alternate-parser spans reused."""
    class JsonObject(dict):
        pass

    def pairs(items):
        result = JsonObject()
        for key, value in items:
            if key in result:
                raise ValueError('JSON_DUPLICATE_KEY')
            result[key] = value
        return result

    result = []
    if text.lstrip().startswith(('{', '[')):
        try:
            value = json.loads(text, object_pairs_hook=pairs)
            def visit(item, path, depth=0):
                if depth > 128:
                    raise ValueError('JSON_DEPTH_UNSUPPORTED')
                if isinstance(item, str):
                    result.append(dict(parser='json', parser_path=path, source=item))
                elif isinstance(item, JsonObject):
                    for key, child in item.items():
                        visit(child, path + [key], depth + 1)
                elif isinstance(item, list):
                    for index, child in enumerate(item):
                        visit(child, path + [index], depth + 1)
            visit(value, [])
            return result
        except (ValueError, RecursionError):
            result = []
    section = None
    for physical_line in text.splitlines():
        stripped = physical_line.strip()
        if stripped[:1] == '[' and stripped[-1:] == ']':
            section = stripped[1:-1]
            continue
        if section is None or not stripped or stripped[0] in ';#':
            continue
        key, separator, source = physical_line.partition('=')
        if separator:
            result.append(dict(parser='ini', parser_path=[section, key.strip(), len(result)], source=source))
    return result


def _resolve_reference(documents, entry, doc, ref):
    fid, pid = ref.get('file_id'), ref.get('path_id')
    if not isinstance(fid, int) or not isinstance(pid, int) or fid < 0:
        raise FormatError('PPTR_BINDING_INVALID')
    if pid == 0:
        return None, None
    if fid == 0:
        return entry, next((o for o in doc['objects'] if o['path_id'] == pid), None)
    if fid > len(doc['externals']):
        raise FormatError('PPTR_EXTERNAL_INDEX_INVALID')
    path = doc['externals'][fid - 1]['path'].replace('\\', '/')
    # archive:/.../entry is Unity's explicit bundle reference syntax.
    matches = [(name, other) for name, _, other in documents
               if name is not None and (name == path or
                  (path.startswith('archive:/') and name == path.rsplit('/', 1)[-1]))]
    if len(matches) != 1:
        return path, None
    name, other = matches[0]
    return name, next((o for o in other['objects'] if o['path_id'] == pid), None)


def bind_script_references(results: list[dict]) -> list[dict]:
    """Resolve one method's scanned MonoScript PPtrs using physical identities.

    No input is mutated. Unresolved/conflicting references produce no binding,
    so the originating object's existing pending record survives its next scan.
    Relative external paths are matched lexically, never by filesystem basename
    or CLR type names. Unity archive references name a container entry explicitly.
    """
    def logical_path(value):
        return posixpath.normpath(value.replace('\\', '/'))

    scripts = {}
    for result in results:
        for record in result.get('types', []):
            if record.get('kind') != 'monoscript':
                continue
            location = record.get('location', {})
            file = record.get('file')
            pid = location.get('object_id')
            assembly, name = record.get('assembly'), record.get('name')
            if (not isinstance(file, str) or type(pid) is not int or
                    not isinstance(assembly, str) or not isinstance(name, str)):
                continue
            entry = location.get('container_entry')
            if entry is not None and not isinstance(entry, str):
                continue
            key = (logical_path(file), entry, pid)
            scripts.setdefault(key, set()).add((assembly, name))

    entries = {}
    for key in scripts:
        if key[1] is not None:
            entries.setdefault((key[1], key[2]), set()).add(key)

    owners = {}
    for result in results:
        for edge in result.get('dependencies', []):
            if edge.get('kind') != 'serialized_reference':
                continue
            if edge.get('source_class_id', 114) != 114:
                continue
            location, target = edge.get('location', {}), edge.get('target', {})
            file, pid = edge.get('file'), location.get('object_id')
            if (location.get('field_path') != ['m_Script'] or not isinstance(file, str)
                    or type(pid) is not int or type(target.get('object_id')) is not int
                    or type(target.get('file_id')) is not int or target['file_id'] < 0
                    or target['object_id'] == 0):
                continue
            entry = location.get('container_entry')
            external = target.get('external_path')
            target_entry = target.get('container_entry')
            if (entry is not None and not isinstance(entry, str)) or (
                    external is not None and not isinstance(external, str)) or (
                    target_entry is not None and not isinstance(target_entry, str)):
                continue
            owner = (logical_path(file), entry, pid)
            signature = (target['file_id'], target['object_id'], external, target_entry)
            owners.setdefault(owner, {})[signature] = edge

    bindings = []
    for owner, references in owners.items():
        # Conflicting physical script edges remain ambiguous even when their
        # MonoScripts happen to declare the same CLR type.
        if len(references) != 1:
            continue
        edge = next(iter(references.values()))
        target = edge['target']
        file, entry, object_id = owner
        script_id = target['object_id']
        if target['file_id'] == 0:
            candidates = {(file, entry, script_id)} & scripts.keys()
        else:
            external = (target.get('external_path') or '').replace('\\', '/')
            if external.startswith('archive:/'):
                declared_entry = external.rsplit('/', 1)[-1]
                candidates = entries.get((declared_entry, script_id), set())
                local = {key for key in candidates if key[0] == file}
                if local:
                    candidates = local
            elif external:
                declared_entry = logical_path(external)
                local = {(file, declared_entry, script_id)} & scripts.keys()
                if local:
                    candidates = local
                else:
                    declared_files = {declared_entry,
                        logical_path(posixpath.join(posixpath.dirname(file), external))}
                    candidates = set(entries.get((declared_entry, script_id), ()))
                    candidates.update((target_file, None, script_id) for target_file in declared_files
                                      if (target_file, None, script_id) in scripts)
            else:
                # An unresolved target entry label alone is not evidence.
                target_entry = target.get('container_entry')
                candidates = ({(file, target_entry, script_id)} & scripts.keys()
                              if edge.get('resolved') and target_entry is not None else set())
        if len(candidates) != 1:
            continue
        script_key = next(iter(candidates))
        descriptors = scripts[script_key]
        if len(descriptors) != 1:
            continue
        assembly, name = next(iter(descriptors))
        bindings.append(dict(file=file, container_entry=entry, object_id=object_id,
                             assembly=assembly, name=name))
    return bindings


def _bound_script(types, relative, entry, obj, script_path=None, script_id=None):
    matches = []
    for binding in types.get('__script_bindings__', []):
        if (binding.get('file') == relative and binding.get('object_id') == obj['path_id']
                and binding.get('container_entry') == entry):
            matches.append(binding)
        elif (script_path is not None and binding.get('script_file') == script_path
              and binding.get('script_object_id') == script_id):
            matches.append(binding)
    if len(matches) > 1:
        raise FormatError('MONOSCRIPT_BINDING_AMBIGUOUS')
    if matches:
        return dict(kind='named', assembly=matches[0]['assembly'], name=matches[0]['name'])
    return None


def scan_serialized(path: Path, relative_path: str, *, method='primary', managed_types=None) -> dict:
    """Inventory exact physical string occurrences from one immutable file snapshot."""
    if method not in ('primary', 'independent'):
        raise ValueError('SCAN_METHOD_INVALID')
    relative = PurePosixPath(str(relative_path).replace('\\', '/')).as_posix()
    raw, types = Path(path).read_bytes(), managed_types or {}
    result = {key: [] for key in ('occurrences', 'types', 'dependencies', 'pending', 'proofs')}
    proof_id = 'serialized:' + method + ':' + _digest(raw)
    result['proofs'].append(dict(id=proof_id, method=method, file=relative,
                                 file_sha256=_digest(raw), parser='UnityPy-1.25.4' if method == 'primary'
                                 else 'first_party_v22_unityfs', runtime_observed=False))

    def pending(reason, obj=None, entry=None, affects_display=True):
        row = dict(reason=reason, file=relative, affects_display=affects_display, proof_ref=proof_id)
        if obj is not None:
            row['location'] = _location(obj, [], entry)
        result['pending'].append(row)

    try:
        documents = _primary_documents(raw) if method == 'primary' else _raw_documents(raw)[0]
    except (ValueError, OSError, EOFError, struct.error) as exc:
        pending(str(exc) if isinstance(exc, FormatError) else 'SERIALIZED_PARSE_FAILED')
        return result
    decoded, scripts = {}, {}
    for entry, payload, doc in documents:
        for index, obj in enumerate(doc['objects']):
            obj['object_index'] = index
            if obj['class_id'] in _PIXEL_OBJECT_CLASSES:
                continue  # Retain table identity for PPtrs without accessing pixel fields.
            try:
                if method == 'primary':
                    try:
                        data = _primary_value(obj)
                    except FormatError as exc:
                        if str(exc) != 'TYPE_TREE_MISSING':
                            raise
                        data = _primary_stripped(obj, types)
                elif doc['types'][obj['type_index']]['nodes']:
                    data = raw_reader.tree_object(payload, obj, doc['types'][obj['type_index']])
                else:
                    data = _native_object(payload, obj)
                decoded[(entry, obj['path_id'])] = data
                if obj['class_id'] == 115:
                    descriptor = _descriptor(data['value'])
                    scripts[(entry, obj['path_id'])] = descriptor
                    result['types'].append(dict(kind='monoscript', file=relative,
                        location=_location(obj, [], entry), assembly=descriptor['assembly'],
                        name=descriptor['name'], proof_ref=proof_id))
            except (ValueError, KeyError, TypeError, EOFError, struct.error):
                # Texture/mesh/etc. opaque native bytes are not display blockers.
                if obj['class_id'] in (49, 114, 115) or obj['class_id'] in _NATIVE_TEXT:
                    pending('DISPLAY_OBJECT_LAYOUT_UNRESOLVED', obj, entry)
    for entry, payload, doc in documents:
        for obj in doc['objects']:
            data = decoded.get((entry, obj['path_id']))
            if data is None:
                continue
            descriptor = None
            if obj['class_id'] == 114:
                script_ref = next((r for r in data['references'] if r['field_path'] == ['m_Script']), None)
                if script_ref:
                    try:
                        target_entry, target = _resolve_reference(documents, entry, doc, script_ref)
                        descriptor = scripts.get((target_entry, target['path_id'])) if target else None
                        descriptor = descriptor or _bound_script(types, relative, entry, obj,
                                                                target_entry, script_ref['path_id'])
                    except FormatError as exc:
                        pending(str(exc), obj, entry)
                descriptor = descriptor or _bound_script(types, relative, entry, obj)
                if descriptor is None:
                    pending('MONOSCRIPT_DEPENDENCY_MISSING', obj, entry)
                elif ('fields_offset' in data or (method == 'primary' and
                       getattr(getattr(obj['reader'], 'serialized_type', None), 'node', None) is None
                       and not getattr(getattr(obj['reader'], 'serialized_type', None), 'nodes', None))):
                    try:
                        if method == 'primary':
                            data = _primary_stripped(obj, types, descriptor)
                        else:
                            custom = raw_reader.managed_object(payload, obj, types, descriptor, data['fields_offset'])
                            data = dict(value={**data['value'], **custom['value']},
                                        strings=data['strings'] + custom['strings'],
                                        references=data['references'] + custom['references'])
                    except (ValueError, KeyError, TypeError, struct.error):
                        pending('MANAGED_DISPLAY_LAYOUT_UNRESOLVED', obj, entry)
            try:
                ui_type = _ui_type(types, descriptor)
            except FormatError:
                ui_type = False
                pending('MANAGED_BASE_TYPE_MISSING', obj, entry)
            for field_index, string in enumerate(data['strings']):
                field_path = string['field_path']
                if not field_path:
                    continue
                classification, reason = 'uncertain', 'SERIALIZED_STRING_UNCLASSIFIED'
                binding = None
                if descriptor and field_path[0] not in ('m_Name', 'm_EditorClassIdentifier'):
                    try:
                        binding = _field_binding(types, descriptor, field_path)
                    except FormatError:
                        pass
                if (len(field_path) == 1 and field_path[0] in _NATIVE_TEXT.get(obj['class_id'], set())):
                    classification, reason = 'display', 'NATIVE_SERIALIZED_DISPLAY_SLOT'
                elif ui_type and field_path in (['m_Text'], ['m_text']):
                    classification, reason = 'display', 'UI_SERIALIZED_TEXT_SLOT'
                elif binding and binding.get('display_consumers'):
                    classification, reason = 'display', 'MANAGED_FIELD_DISPLAY_FLOW'
                elif binding and binding.get('native_display'):
                    classification, reason = 'display', 'NATIVE_SERIALIZED_DISPLAY_SLOT'
                elif binding and binding.get('native_identity'):
                    classification, reason = 'non_display', 'NATIVE_UI_IDENTITY_FIELD'
                elif field_path == ['m_Name'] or obj['class_id'] == 115:
                    classification, reason = 'non_display', 'SERIALIZED_IDENTITY_FIELD'
                kind = 'textasset' if obj['class_id'] == 49 and field_path == ['m_Script'] else 'serialized_string'
                location = _location(obj, field_path, entry, field_index)
                proof = dict(ref=proof_id, class_id=obj['class_id'])
                if descriptor:
                    proof['managed_type'] = descriptor['assembly'] + '::' + descriptor['name']
                if binding and binding.get('display_consumers'):
                    # Large flow evidence belongs to managed scanner proofs, not every object.
                    proof['field_token'] = binding.get('proof', {}).get('field_token')
                row = dict(kind=kind, file=relative, location=location, source=string['source'],
                           classification=classification, reason=reason, proof=proof)
                result['occurrences'].append(row)
                if kind == 'textasset':
                    table_reader = _primary_table_strings if method == 'primary' else _table_strings
                    for table_index, table in enumerate(table_reader(string['source'])):
                        table_location = dict(location, parser_path=table['parser_path'],
                                              parser=table['parser'], entry_index=table_index)
                        result['occurrences'].append(dict(row, source=table['source'], location=table_location,
                            reason='TEXTASSET_TABLE_VALUE_UNBOUND'))
            for ref in data['references']:
                location = _location(obj, ref['field_path'], entry)
                try:
                    target_entry, target = _resolve_reference(documents, entry, doc, ref)
                    fid, pid = ref['file_id'], ref['path_id']
                    target_value = dict(file_id=fid, object_id=pid)
                    if target is not None:
                        target_value['class_id'] = target['class_id']
                    if fid and fid <= len(doc['externals']):
                        target_value['external_path'] = doc['externals'][fid - 1]['path']
                    if target_entry is not None:
                        target_value['container_entry'] = target_entry
                    bound = None
                    if descriptor and ref['field_path'] not in (['m_Script'], ['m_GameObject']):
                        try:
                            bound = _field_binding(types, descriptor, ref['field_path'])
                        except FormatError:
                            pass
                    native_ui_reference = ((ui_type or obj['class_id'] in (102, 132)) and
                        len(ref['field_path']) == 1 and ref['field_path'][0] in _UI_REFERENCE_FIELDS)
                    display_dependency = native_ui_reference or bool(bound and
                        (bound.get('display_consumers') or bound.get('native_reference')))
                    edge = dict(kind='serialized_reference', file=relative, location=location,
                                source_class_id=obj['class_id'], target=target_value,
                                target_type=ref.get('target_type'),
                                display_dependency=display_dependency, resolved=bool(target) or pid == 0,
                                proof_ref=proof_id)
                    result['dependencies'].append(edge)
                    if display_dependency and pid and not target:
                        pending('DISPLAY_REFERENCE_TARGET_MISSING', obj, entry)
                    elif display_dependency and target and target['class_id'] in (28, 213):
                        # This is a structural media dependency, never permission to read pixels.
                        pending('DISPLAY_MEDIA_CONTENT_UNVERIFIED', obj, entry, affects_display=False)
                except (FormatError, KeyError, TypeError):
                    pending('PPTR_BINDING_UNRESOLVED', obj, entry)
    # A source-bound field-to-display flow can bind a referenced TextAsset's
    # payload. Mere resource proximity and unclassified PPtrs never do so.
    displayed_assets = set()
    for edge in result['dependencies']:
        if edge['display_dependency'] and edge['resolved']:
            target = edge['target']
            target_entry = target.get('container_entry', edge['location'].get('container_entry'))
            displayed_assets.add((target_entry, target['object_id']))
    table_assets = {(row['location'].get('container_entry'), row['location']['object_id'])
                    for row in result['occurrences']
                    if row['kind'] == 'textasset' and 'parser_path' in row['location']}
    for row in result['occurrences']:
        location = row['location']
        asset = (location.get('container_entry'), location['object_id'])
        if row['kind'] != 'textasset' or asset not in displayed_assets:
            continue
        if 'parser_path' in location:
            continue  # A reference to the asset does not prove a table-entry consumer.
        if asset in table_assets:
            # Keep one possible payload projection, not overlapping payload and
            # entry writes. The consumer has not selected either interpretation.
            row['display_dependency'] = True
            row['display_projection_required'] = True
            row['reason'] = 'TEXTASSET_DISPLAY_PROJECTION_UNRESOLVED'
            result['pending'].append(dict(reason=row['reason'], file=relative,
                location=dict(location), affects_display=True, proof_ref=proof_id))
        else:
            row['classification'] = 'display'
            row['reason'] = 'TEXTASSET_DISPLAY_REFERENCE'
    return result


def _replace_tables(source, rows):
    entries = _table_strings(source)
    changes = []
    for replacement in rows:
        location = replacement['location']
        matches = [e for e in entries if e['parser_path'] == location.get('parser_path')
                   and e['parser'] == location.get('parser')]
        if len(matches) != 1 or matches[0]['source'] != replacement['source']:
            raise FormatError('TEXTASSET_ENTRY_BINDING_INVALID')
        entry = matches[0]
        target = replacement['target']
        if entry['parser'] == 'json':
            target = json.dumps(target, ensure_ascii=False)
        elif '\n' in target or '\r' in target:
            raise FormatError('TEXTASSET_INI_MULTILINE_UNSUPPORTED')
        changes.append((entry['char_start'], entry['char_end'], target))
    for start, end, target in sorted(changes, reverse=True):
        source = source[:start] + target + source[end:]
    return source


def patch_serialized(raw: bytes, replacements: list[dict], *, managed_types=None) -> bytes:
    """Replace exact, uniquely bound strings; retain every unowned object byte.

    Each replacement requires location.{object_id,field_path,container_entry?},
    source and target. Optional file_sha256 binds the complete input snapshot.
    TextAsset table replacements additionally carry parser and parser_path.
    Standalone metadata/header and UnityFS directory relocation are the only
    non-string bytes permitted to change. No UnityPy execution is needed.
    """
    if not isinstance(raw, bytes) or not isinstance(replacements, list):
        raise FormatError('PATCH_INPUT_INVALID')
    documents, archive = _raw_documents(raw)
    if not replacements:
        return raw
    types, digest, groups, seen = managed_types or {}, _digest(raw), {}, set()
    if any(not isinstance(row, dict) for row in replacements):
        raise FormatError('REPLACEMENT_BINDING_INVALID')
    labels = [row['file'] for row in replacements if row.get('file') is not None]
    if any(not isinstance(label, str) for label in labels) or len(set(labels)) > 1:
        raise FormatError('SOURCE_FILE_BINDING_AMBIGUOUS')
    relative = labels[0] if labels else ''
    for replacement in replacements:
        location = replacement.get('location')
        if (not isinstance(location, dict) or not isinstance(replacement.get('source'), str)
                or not isinstance(replacement.get('target'), str)):
            raise FormatError('REPLACEMENT_BINDING_INVALID')
        if replacement.get('file_sha256', digest) != digest:
            raise FormatError('SOURCE_FILE_BINDING_MISMATCH')
        pid, path, entry = location.get('object_id'), location.get('field_path'), location.get('container_entry')
        if (type(pid) is not int or not isinstance(path, list) or not path
                or (entry is not None and not isinstance(entry, str))
                or any(type(p) not in (str, int) for p in path)):
            raise FormatError('REPLACEMENT_LOCATION_INVALID')
        key = (entry, pid, tuple(path), json.dumps(location.get('parser_path'), ensure_ascii=True))
        if key in seen:
            raise FormatError('REPLACEMENT_BINDING_AMBIGUOUS')
        seen.add(key)
        groups.setdefault(entry, {}).setdefault(pid, {}).setdefault(tuple(path), []).append(replacement)
    if not set(groups) <= {entry for entry, _, _ in documents}:
        raise FormatError('CONTAINER_ENTRY_BINDING_MISSING')
    scripts = {}
    for entry, payload, doc in documents:
        for index, obj in enumerate(doc['objects']):
            obj['object_index'] = index
            if obj['class_id'] == 115:
                typ = doc['types'][obj['type_index']]
                data = raw_reader.tree_object(payload, obj, typ) if typ['nodes'] else _native_object(payload, obj)
                scripts[(entry, obj['path_id'])] = _descriptor(data['value'])
    changed_entries = {}
    for entry, payload, doc in documents:
        if entry not in groups:
            continue
        by_id, changed = {o['path_id']: o for o in doc['objects']}, {}
        if not set(groups[entry]) <= set(by_id):
            raise FormatError('OBJECT_BINDING_MISSING')
        for pid, fields in groups[entry].items():
            obj = by_id[pid]
            if obj['class_id'] in _PIXEL_OBJECT_CLASSES:
                raise FormatError('PIXEL_OBJECT_PATCH_FORBIDDEN')
            typ = doc['types'][obj['type_index']]
            if typ['nodes']:
                data = raw_reader.tree_object(payload, obj, typ)
            else:
                data = _native_object(payload, obj)
                if obj['class_id'] == 114:
                    script_ref = next(r for r in data['references'] if r['field_path'] == ['m_Script'])
                    target_entry, target = _resolve_reference(documents, entry, doc, script_ref)
                    descriptor = scripts.get((target_entry, target['path_id'])) if target else None
                    descriptor = descriptor or _bound_script(types, relative, entry, obj, target_entry, script_ref['path_id'])
                    if not descriptor:
                        raise FormatError('MONOSCRIPT_DEPENDENCY_MISSING')
                    custom = raw_reader.managed_object(payload, obj, types, descriptor, data['fields_offset'])
                    data['strings'].extend(custom['strings'])
            spans = []
            for path, rows in fields.items():
                matches = [s for s in data['strings'] if tuple(s['field_path']) == path]
                if len(matches) != 1:
                    raise FormatError('FIELD_BINDING_AMBIGUOUS')
                string = matches[0]
                table_rows = [r for r in rows if 'parser_path' in r['location']]
                if table_rows:
                    if len(table_rows) != len(rows) or obj['class_id'] != 49 or path != ('m_Script',):
                        raise FormatError('TEXTASSET_ENTRY_BINDING_AMBIGUOUS')
                    target_text = _replace_tables(string['source'], table_rows)
                else:
                    if len(rows) != 1 or rows[0]['source'] != string['source']:
                        raise FormatError('SOURCE_STRING_BINDING_MISMATCH')
                    target_text = rows[0]['target']
                encoded = target_text.encode('utf-8')
                if len(encoded) > 0x7fffffff:
                    raise FormatError('STRING_SIZE_UNSUPPORTED')
                start = string['offset']
                new_content_end = start + 4 + len(encoded)
                padding = (-new_content_end) % 4
                # Retain original padding byte-for-byte when its width is unchanged.
                old_padding = payload[string['content_end']:string['end']]
                suffix = old_padding if len(old_padding) == padding else bytes(padding)
                new_value = struct.pack('<i', len(encoded)) + encoded + suffix
                spans.append((start - obj['offset'], string['end'] - obj['offset'], new_value))
            value = payload[obj['offset']:obj['offset'] + obj['size']]
            last = len(value)
            for start, end, new_value in sorted(spans, reverse=True):
                if end > last:
                    raise FormatError('FIELD_REGIONS_OVERLAP')
                value = value[:start] + new_value + value[end:]
                last = start
            changed[pid] = value
        changed_entries[entry] = raw_reader.rebuild(payload, doc, changed)
    if archive is None:
        return changed_entries[None]
    return raw_reader.rebuild_bundle(raw, archive, changed_entries)
