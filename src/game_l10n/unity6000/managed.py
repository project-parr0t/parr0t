"""Targeted unsigned CLR MethodDef ldstr edits; metadata is relocated additively."""
from __future__ import annotations

import struct

from .contracts import AdapterError, categorical, fail, validate_text


def align(value, unit):
    if unit <= 0 or unit & (unit - 1):
        fail("PE_ALIGNMENT_INVALID")
    return (value + unit - 1) & ~(unit - 1)


def compressed_uint(value):
    if not 0 <= value < 0x20000000:
        fail("USER_STRING_TOO_LONG")
    if value < 0x80:
        return bytes([value])
    if value < 0x4000:
        return struct.pack(">H", value | 0x8000)
    return struct.pack(">I", value | 0xC0000000)


def user_string(value):
    encoded = value.encode("utf-16-le")
    # ECMA-335 II.24.2.4: special flag examines UTF-16 bytes, not codepoints.
    special = any(encoded[i + 1] or encoded[i] in {*range(1, 9), *range(14, 32), 39, 45, 127} for i in range(0, len(encoded), 2))
    return compressed_uint(len(encoded) + 1) + encoded + bytes([bool(special)])


def _load(data):
    import dnfile
    try:
        pe = dnfile.dnPE(data=data)
        if not pe.net or not pe.net.metadata or not pe.net.mdtables:
            fail("CLR_METADATA_REQUIRED")
        if pe.net.struct.StrongNameSignatureSize or pe.net.struct.Flags & 8:
            fail("SIGNED_ASSEMBLY_UNSUPPORTED")
        if pe.OPTIONAL_HEADER.DATA_DIRECTORY[4].Size:
            fail("AUTHENTICODE_UNSUPPORTED")
        if not pe.net.struct.Flags & 1 or pe.net.struct.ManagedNativeHeaderSize:
            fail("NATIVE_ASSEMBLY_UNSUPPORTED")
        if b"#US" not in pe.net.metadata.streams:
            fail("USER_STRING_HEAP_REQUIRED")
        return pe
    except AdapterError:
        raise
    except Exception:
        fail("CLR_PARSE_INVALID")


def _body(pe, data, token):
    from dncil.cil.body import CilMethodBody
    from dncil.cil.body.reader import CilMethodBodyReaderBytes
    if type(token) is not int or token >> 24 != 6 or not 1 <= (token & 0xFFFFFF) <= len(pe.net.mdtables.MethodDef.rows):
        fail("METHOD_TOKEN_INVALID")
    method = pe.net.mdtables.MethodDef.rows[(token & 0xFFFFFF) - 1]
    if not method.Rva:
        fail("METHOD_BODY_REQUIRED")
    try:
        offset = pe.get_offset_from_rva(method.Rva)
        return offset, CilMethodBody(CilMethodBodyReaderBytes(data[offset:]))
    except Exception:
        fail("METHOD_BODY_INVALID")


def discover(data):
    pe = _load(data)
    records = []
    for rid, method in enumerate(pe.net.mdtables.MethodDef.rows, 1):
        if not method.Rva:
            continue
        _, body = _body(pe, data, 0x06000000 | rid)
        for instruction in body.instructions:
            if instruction.opcode.name == "ldstr":
                token = instruction.operand.value
                if token >> 24 != 0x70:
                    fail("USER_STRING_TOKEN_INVALID")
                value = pe.net.user_strings.get(token & 0xFFFFFF)
                if value is None:
                    fail("USER_STRING_TOKEN_INVALID")
                records.append({"kind": "managed_literal", "method_token": 0x06000000 | rid, "il_offset": instruction.offset, "source": str(value)})
    return records


@categorical
def patch(data, changes):
    pe = _load(data)
    result = bytearray(data)
    heap = bytearray(pe.net.metadata.streams[b"#US"].__data__)
    added = {}
    locations = set()
    for change in changes:
        location = (change["method_token"], change["il_offset"])
        if location in locations:
            fail("DUPLICATE_LOCATION")
        locations.add(location)
        offset, body = _body(pe, data, location[0])
        instruction = next((i for i in body.instructions if i.offset == location[1]), None)
        if instruction is None or instruction.opcode.name != "ldstr" or data[offset + instruction.offset] != 0x72:
            fail("MANAGED_LOCATION_MISMATCH")
        token = instruction.operand.value
        value = pe.net.user_strings.get(token & 0xFFFFFF)
        if token >> 24 != 0x70 or value is None or str(value) != change["source"]:
            fail("MANAGED_SOURCE_MISMATCH")
        validate_text(change["source"], change["target"])
        if change["target"] not in added:
            if len(heap) > 0xFFFFFF:
                fail("USER_STRING_TOKEN_OVERFLOW")
            added[change["target"]] = len(heap)
            heap.extend(user_string(change["target"]))
        struct.pack_into("<I", result, offset + instruction.offset + 1, 0x70000000 | added[change["target"]])
    if not locations:
        fail("EMPTY_PATCH")
    meta_offset = pe.get_offset_from_rva(pe.net.struct.MetaDataRva)
    metadata = data[meta_offset:meta_offset + pe.net.struct.MetaDataSize]
    streams = pe.net.metadata.streams
    first = min(stream.struct.Offset for stream in streams.values())
    header = bytearray(metadata[:first])
    body = bytearray()
    for name, stream in streams.items():
        body.extend(b"\0" * (align(len(body), 4) - len(body)))
        blob = bytes(heap) if name == b"#US" else stream.__data__
        position = stream.struct.get_file_offset() - meta_offset
        if position < 0 or position + 8 > len(header):
            fail("METADATA_HEADER_INVALID")
        struct.pack_into("<II", header, position, len(header) + len(body), len(blob))
        body.extend(blob)
    new_metadata = header + body
    if not pe.sections:
        fail("PE_SECTION_REQUIRED")
    last = max(pe.sections, key=lambda s: s.VirtualAddress)
    if last is not max(pe.sections, key=lambda s: s.PointerToRawData):
        fail("PE_SECTION_LAYOUT_UNSUPPORTED")
    raw_end = last.PointerToRawData + last.SizeOfRawData
    if last.Misc_VirtualSize > last.SizeOfRawData:
        fail("PE_BSS_TAIL_UNSUPPORTED")
    if raw_end != len(data):
        fail("PE_OVERLAY_UNSUPPORTED")
    raw_position = align(raw_end, 4)
    result.extend(b"\0" * (raw_position - len(result)))
    result.extend(new_metadata)
    virtual_size = len(result) - last.PointerToRawData
    raw_size = align(virtual_size, pe.OPTIONAL_HEADER.FileAlignment)
    result.extend(b"\0" * (last.PointerToRawData + raw_size - len(result)))
    section_offset = last.get_file_offset()
    struct.pack_into("<I", result, section_offset + 8, virtual_size)
    struct.pack_into("<I", result, section_offset + 16, raw_size)
    struct.pack_into("<I", result, section_offset + 36, (last.Characteristics & ~0x02000000) | 0x40000040)
    new_rva = last.VirtualAddress + raw_position - last.PointerToRawData
    struct.pack_into("<II", result, pe.net.struct.get_file_offset() + 8, new_rva, len(new_metadata))
    struct.pack_into("<I", result, pe.OPTIONAL_HEADER.get_field_absolute_offset("SizeOfImage"), align(last.VirtualAddress + virtual_size, pe.OPTIONAL_HEADER.SectionAlignment))
    struct.pack_into("<I", result, pe.OPTIONAL_HEADER.get_field_absolute_offset("CheckSum"), 0)
    patched = bytes(result)
    checked = {(r["method_token"], r["il_offset"]): r["source"] for r in discover(patched)}
    for change in changes:
        if checked.get((change["method_token"], change["il_offset"])) != change["target"]:
            fail("MANAGED_ROUNDTRIP_INVALID")
    # All unselected literals retain their decoded source and exact instruction location.
    for original in discover(data):
        key = original["method_token"], original["il_offset"]
        if key not in locations and checked.get(key) != original["source"]:
            fail("MANAGED_UNSELECTED_LITERAL_CHANGED")
    return patched
