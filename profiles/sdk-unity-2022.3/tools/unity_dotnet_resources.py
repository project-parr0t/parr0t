"""Non-executing .resources v1/v2 string reader and append-only value rewrite.

Never instantiate ResourceReader, deserialize objects, or load resource types.
Unknown payloads are retained verbatim; only verified string entries are exposed.
"""
import struct
from unity_clr import require, take, unpack, align


def seven(raw, pos):
    value = 0
    for shift in range(0, 35, 7):
        byte = take(raw, pos, 1)[0]
        pos += 1
        require(shift < 28 or byte <= 15, 'RESOURCE_SEVEN_BIT_INTEGER')
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, pos
    raise ValueError('RESOURCE_SEVEN_BIT_INTEGER')


def put_seven(value):
    require(0 <= value <= 0x7fffffff, 'RESOURCE_INTEGER_LIMIT')
    data = bytearray()
    while value >= 128:
        data.append((value & 127) | 128)
        value >>= 7
    data.append(value)
    return bytes(data)


def _text(raw, pos, encoding, limit):
    length, start = seven(raw, pos)
    require(start + length <= limit, 'RESOURCE_TEXT_BOUNDS')
    return take(raw, start, length).decode(encoding, errors='surrogatepass'), start + length


def parse(raw, *, independent=False):
    require(unpack('<I', raw, 0)[0] == 0xbeefcace, 'RESOURCE_MAGIC')
    header_version, header_size = unpack('<II', raw, 4)
    require(header_version >= 1, 'RESOURCE_HEADER_VERSION')
    p = 12 + header_size
    take(raw, 12, header_size)
    version, count, type_count = unpack('<III', raw, p)
    require(version in (1, 2), 'RESOURCE_VERSION_UNSUPPORTED')
    p += 12
    require(count <= len(raw) // 8 and type_count <= len(raw), 'RESOURCE_COUNT_BOUNDS')
    types = []
    for _ in range(type_count):
        name, p = _text(raw, p, 'utf-8', len(raw))
        types.append(name)
    p = align(p, 8)
    hashes = [v[0] for v in struct.iter_unpack('<i', take(raw, p, count * 4))]
    require(hashes == sorted(hashes), 'RESOURCE_HASH_ORDER')
    p += count * 4
    positions = [v[0] for v in struct.iter_unpack('<i', take(raw, p, count * 4))]
    p += count * 4
    data_start = unpack('<i', raw, p)[0]
    names_start = p + 4
    require(names_start <= data_start <= len(raw), 'RESOURCE_DATA_BOUNDARY')
    entries = []
    indices = range(count - 1, -1, -1) if independent else range(count)
    name_spans, names = [], set()
    for index in indices:
        position = positions[index]
        require(position >= 0, 'RESOURCE_NAME_POSITION')
        start = names_start + position
        name, end = _text(raw, start, 'utf-16-le', data_start)
        require(end + 4 <= data_start, 'RESOURCE_NAME_BOUNDARY')
        value_offset = unpack('<i', raw, end)[0]
        require(value_offset >= 0 and data_start + value_offset < len(raw), 'RESOURCE_VALUE_POSITION')
        require(name not in names, 'RESOURCE_DUPLICATE_NAME')
        hashed = 5381
        for unit in struct.iter_unpack('<H', name.encode('utf-16-le', errors='surrogatepass')):
            hashed = (((hashed << 5) + hashed) ^ unit[0]) & 0xffffffff
        require(hashes[index] == (hashed if hashed < 0x80000000 else hashed - 0x100000000), 'RESOURCE_NAME_HASH')
        names.add(name)
        name_spans.append((start, end + 4))
        entries.append({'entry_index': index, 'name': name, 'offset_slot': end, 'value_offset': data_start + value_offset})
    spans = sorted(name_spans)
    require(all(a[1] <= b[0] for a, b in zip(spans, spans[1:])), 'RESOURCE_NAME_OVERLAP')
    offsets = sorted({entry['value_offset'] for entry in entries} | {len(raw)})
    ends = {a: b for a, b in zip(offsets, offsets[1:])}
    for entry in entries:
        start = entry['value_offset']
        code, p = seven(raw, start)
        is_string = version == 2 and code == 1
        if version == 1 and code != 0xffffffff:
            require(code < len(types), 'RESOURCE_TYPE_INDEX')
            # The type declaration is metadata only; never resolve/load it.
            declaration = [part.strip() for part in types[code].split(',')]
            is_string = len(declaration) >= 2 and declaration[0] == 'System.String' and declaration[1] in ('mscorlib', 'System.Private.CoreLib', 'System.Runtime')
        entry['type_code'] = code
        entry['is_string'] = is_string
        if is_string:
            text, end = _text(raw, p, 'utf-8', ends[start])
            entry['source'] = text
    return {'version': version, 'data_start': data_start, 'entries': sorted(entries, key=lambda e: e['entry_index'])}


def rewrite(raw, replacements):
    parsed = parse(raw)
    entries = {entry['entry_index']: entry for entry in parsed['entries']}
    result = bytearray(raw)
    seen = set()
    for replacement in replacements:
        loc = replacement['location']
        index = loc.get('entry_index')
        require(type(index) is int and index in entries and index not in seen, 'RESOURCE_REPLACEMENT_INDEX')
        seen.add(index)
        entry = entries[index]
        require(entry['is_string'], 'RESOURCE_REPLACEMENT_NOT_STRING')
        require('entry_name' not in loc or loc['entry_name'] == entry['name'], 'RESOURCE_REPLACEMENT_NAME')
        require(isinstance(replacement.get('source'), str) and replacement['source'] == entry['source'], 'MANAGED_REPLACEMENT_SOURCE_MISMATCH')
        target = replacement.get('target')
        require(isinstance(target, str), 'MANAGED_TARGET_TYPE')
        data = target.encode('utf-8', errors='surrogatepass')
        offset = len(result) - parsed['data_start']
        require(offset <= 0x7fffffff, 'RESOURCE_INTEGER_LIMIT')
        struct.pack_into('<i', result, entry['offset_slot'], offset)
        result.extend(put_seven(entry['type_code']) + put_seven(len(data)) + data)
    return bytes(result)


def inventory(image, *, independent=False):
    occurrences, pending, resources = [], [], {}
    rva, size = unpack('<II', image.raw, image.cli + 24)
    data = take(image.raw, image.at(rva, size), size) if rva and size else b''
    for rid, row in enumerate(image.rows.get(40, []), 1):
        offset, flags, name_index, implementation = row
        token = 0x28000000 | rid
        name = image.string(name_index)
        if implementation:
            table, index = image.coded('Implementation', implementation)
            resources[token] = {'name': name, 'linked': True, 'implementation_table': table, 'implementation_row': index}
            continue
        try:
            length = unpack('<I', data, offset)[0]
            raw = take(data, offset + 4, length)
            resources[token] = {'name': name, 'linked': False, 'raw': raw, 'offset': offset}
            if raw[:4] != b'\xce\xca\xef\xbe':
                continue  # arbitrary embedded binary is not a guessed string table
            parsed = parse(raw, independent=independent)
            resources[token]['parsed'] = parsed
            for entry in parsed['entries']:
                if entry['is_string']:
                    occurrences.append({'kind': 'managed_resource', 'location': {'resource_token': token, 'entry_index': entry['entry_index'], 'resource_name': name},
                                        'source': entry['source'], 'classification': 'uncertain', 'reason': 'resource_consumer_unproven',
                                        'proof': {'resource_ref': 'resource:' + str(token)}, 'entry_name': entry['name']})
        except (ValueError, UnicodeError) as exc:
            pending.append({'reason': 'managed_resource_layout_unsupported', 'resource_token': token, 'detail': str(exc), 'scope': 'resource_layout'})
    return occurrences, pending, resources, data
