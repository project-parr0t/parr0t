"""Windows resource-template and Unity app.info display bindings, not raw scans."""
from __future__ import annotations

import re
from collections import Counter
import struct
from pathlib import Path
from typing import Any


def _span(raw: bytes, offset: int, size: int) -> bytes:
    if offset < 0 or size < 0 or offset + size > len(raw):
        raise ValueError("NATIVE_RESOURCE_BOUNDARY_INVALID")
    return raw[offset:offset + size]


def _u16(raw: bytes, offset: int) -> int:
    return struct.unpack("<H", _span(raw, offset, 2))[0]


def _u32(raw: bytes, offset: int) -> int:
    return struct.unpack("<I", _span(raw, offset, 4))[0]


def _z(raw: bytes, offset: int) -> tuple[str, int]:
    end = offset
    while _u16(raw, end):
        end += 2
    return _span(raw, offset, end - offset).decode("utf-16-le"), end + 2


class Template:
    def __init__(self, raw: bytes):
        self.raw, self.position = raw, 0
        self.parts: list[tuple[str, Any]] = []
        self.strings: list[str] = []

    def take(self, size: int) -> bytes:
        value = _span(self.raw, self.position, size)
        self.position += size
        self.parts.append(("raw", value))
        return value

    def align(self, alignment: int) -> None:
        size = (-self.position) % alignment
        value = _span(self.raw, self.position, size)
        self.position += size
        self.parts.append(("align", (alignment, value)))

    def text(self, *, display: bool = False, ordinal: bool = True) -> None:
        if ordinal and _u16(self.raw, self.position) == 0xffff:
            self.take(4)
            return
        text, end = _z(self.raw, self.position)
        if display:
            self.parts.append(("text", len(self.strings)))
            self.strings.append(text)
            self.position = end
        else:
            self.take(end - self.position)

    def finish(self) -> None:
        self.take(len(self.raw) - self.position)

    def rebuild(self, targets: dict[int, str]) -> bytes:
        output = bytearray()
        for kind, value in self.parts:
            if kind == "raw":
                output.extend(value)
            elif kind == "text":
                output.extend(targets.get(value, self.strings[value]).encode("utf-16-le") + b"\0\0")
            else:
                alignment, original = value
                size = (-len(output)) % alignment
                output.extend(original if size == len(original) else bytes(size))
        return bytes(output)


def _dialog(raw: bytes) -> Template:
    result = Template(raw)
    extended = _u16(raw, 0) == 1 and _u16(raw, 2) == 0xffff
    if extended:
        header = result.take(26)
        style, count = _u32(header, 12), _u16(header, 16)
    else:
        header = result.take(18)
        style, count = _u32(header, 0), _u16(header, 8)
    result.text()
    result.text()
    result.text(display=True, ordinal=False)
    if style & 0x40:
        result.take(6 if extended else 2)
        result.text(ordinal=False)
    for _ in range(count):
        result.align(4)
        result.take(24 if extended else 18)
        result.text()
        result.text(display=True)
        size = _u16(raw, result.position)
        result.take(max(2, size))
    result.finish()
    return result


def _menu(raw: bytes) -> Template:
    result = Template(raw)
    version, offset = _u16(raw, 0), _u16(raw, 2)
    if version not in {0, 1}:
        raise ValueError("NATIVE_MENU_VERSION_UNSUPPORTED")
    result.take(4 + offset)

    def items(depth: int = 0) -> None:
        if depth > 128:
            raise ValueError("NATIVE_MENU_DEPTH_INVALID")
        while True:
            if version:
                result.align(4)
                header = result.take(14)
                flags = _u16(header, 12)
                popup = bool(flags & 1)
            else:
                header = result.take(2)
                flags = _u16(header, 0)
                popup = bool(flags & 0x10)
                if not popup:
                    result.take(2)
            result.text(display=True, ordinal=False)
            if popup:
                if version:
                    result.align(4)
                    result.take(4)
                items(depth + 1)
            if flags & 0x80:
                break
    items()
    result.finish()
    return result


def _string_table(raw: bytes) -> list[str]:
    values, cursor = [], 0
    for _ in range(16):
        count = _u16(raw, cursor)
        cursor += 2
        values.append(_span(raw, cursor, count * 2).decode("utf-16-le"))
        cursor += count * 2
    if cursor != len(raw):
        raise ValueError("NATIVE_STRING_TABLE_TRAILING_BYTES")
    return values


def _messages(raw: bytes, codepage: int) -> tuple[list[str], list[tuple[int, int, list[int]]], list[int]]:
    count = _u32(raw, 0)
    _span(raw, 4, count * 12)
    values, blocks, flags = [], [], []
    regions = []
    for index in range(count):
        low, high, offset = struct.unpack("<III", _span(raw, 4 + index * 12, 12))
        if high < low or high - low + 1 > len(raw) // 4:
            raise ValueError("NATIVE_MESSAGE_RANGE_INVALID")
        indices = []
        start = offset
        for _ in range(high - low + 1):
            length, flag = struct.unpack("<HH", _span(raw, offset, 4))
            if length < 4 or flag not in {0, 1}:
                raise ValueError("NATIVE_MESSAGE_ENTRY_INVALID")
            data = _span(raw, offset + 4, length - 4)
            if flag:
                end = 0
                while _u16(data, end):
                    end += 2
                text = data[:end].decode("utf-16-le")
            else:
                if not codepage:
                    raise ValueError("NATIVE_ANSI_CODEPAGE_UNBOUND")
                end = data.find(b"\0")
                if end < 0:
                    raise ValueError("NATIVE_MESSAGE_TERMINATOR_MISSING")
                text = data[:end].decode("cp" + str(codepage))
            indices.append(len(values))
            values.append(text)
            flags.append(flag)
            offset += length
        if start < 4 + count * 12 or any(start < end and offset > begin for begin, end in regions):
            raise ValueError("NATIVE_MESSAGE_OVERLAP")
        regions.append((start, offset))
        blocks.append((low, high, indices))
    return values, blocks, flags


def _message_payload(values: list[str], blocks: list[tuple[int, int, list[int]]], flags: list[int], targets: dict[int, str], codepage: int) -> bytes:
    result = bytearray(4 + len(blocks) * 12)
    struct.pack_into("<I", result, 0, len(blocks))
    for index, (low, high, indices) in enumerate(blocks):
        struct.pack_into("<III", result, 4 + index * 12, low, high, len(result))
        for entry in indices:
            text = targets.get(entry, values[entry])
            flag = flags[entry]
            data = text.encode("utf-16-le") + b"\0\0" if flag else text.encode("cp" + str(codepage)) + b"\0"
            length = (4 + len(data) + 3) & ~3
            if length > 65535:
                raise ValueError("NATIVE_MESSAGE_TOO_LONG")
            result.extend(struct.pack("<HH", length, flag) + data + bytes(length - 4 - len(data)))
    return bytes(result)


def _leaves(raw: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from unity_pe import directory_info, read_rva
    rva, size = directory_info(raw, 2)
    if not rva and not size:
        return {"header": bytes(16), "entries": []}, []
    data = read_rva(raw, rva, size)
    leaves: list[dict[str, Any]] = []

    def directory(offset: int, path: list[str | int], active: set[int]) -> dict[str, Any]:
        if offset in active or len(path) > 3:
            raise ValueError("NATIVE_RESOURCE_DIRECTORY_INVALID")
        header = _span(data, offset, 16)
        count = _u16(header, 12) + _u16(header, 14)
        _span(data, offset + 16, count * 8)
        entries = []
        for index in range(count):
            name, child = struct.unpack("<II", _span(data, offset + 16 + index * 8, 8))
            if name & 0x80000000:
                start = name & 0x7fffffff
                units = _u16(data, start)
                key: str | int = _span(data, start + 2, units * 2).decode("utf-16-le")
            else:
                key = name
            if child & 0x80000000:
                value = directory(child & 0x7fffffff, path + [key], active | {offset})
            else:
                payload_rva, payload_size, codepage, reserved = struct.unpack("<IIII", _span(data, child, 16))
                value = {"path": path + [key], "data": read_rva(raw, payload_rva, payload_size), "codepage": codepage, "reserved": reserved}
                leaves.append(value)
            entries.append((key, value))
        return {"header": header, "entries": entries}
    return directory(0, [], set()), leaves


def _rebuild_tree(tree: dict[str, Any], base_rva: int) -> bytes:
    data = bytearray()
    def allocate(size: int, alignment: int = 4) -> int:
        data.extend(bytes((-len(data)) % alignment))
        offset = len(data)
        data.extend(bytes(size))
        return offset
    def directory(node: dict[str, Any]) -> int:
        offset = allocate(16 + 8 * len(node["entries"]))
        data[offset:offset + 16] = node["header"]
        for index, (key, child) in enumerate(node["entries"]):
            if isinstance(key, str):
                name_bytes = key.encode("utf-16-le")
                name_offset = allocate(2 + len(name_bytes), 2)
                struct.pack_into("<H", data, name_offset, len(name_bytes) // 2)
                data[name_offset + 2:name_offset + 2 + len(name_bytes)] = name_bytes
                name = name_offset | 0x80000000
            else:
                name = key
            if "entries" in child:
                child_offset = directory(child) | 0x80000000
            else:
                child_offset = allocate(16)
                payload_offset = allocate(len(child["data"]))
                data[payload_offset:payload_offset + len(child["data"])] = child["data"]
                struct.pack_into("<IIII", data, child_offset, base_rva + payload_offset, len(child["data"]), child["codepage"], child["reserved"])
            struct.pack_into("<II", data, offset + 16 + index * 8, name, child_offset)
        return offset
    if directory(tree) != 0:
        raise ValueError("NATIVE_RESOURCE_ROOT_INVALID")
    return bytes(data)


def _leaf_strings(leaf: dict[str, Any]) -> list[str]:
    kind, raw = leaf["path"][0], leaf["data"]
    if kind == 6:
        return _string_table(raw)
    if kind == 5:
        return _dialog(raw).strings
    if kind == 4:
        return _menu(raw).strings
    if kind == 11:
        return _messages(raw, leaf["codepage"])[0]
    return []


def scan_native(path: Path, relative_path: str, *, method: str = "primary") -> dict[str, Any]:
    if method not in {"primary", "independent"}:
        raise ValueError("INVENTORY_METHOD_INVALID")
    raw = path.read_bytes()
    if path.name == "app.info":
        lines = raw.decode("utf-8-sig").splitlines()
        occurrences = []
        if len(lines) >= 2:
            occurrences.append({"kind": "app_info", "file": relative_path, "location": {"line_index": 1}, "source": lines[1], "classification": "display" if lines[1] else "non_display", "reason": "UNITY_PRODUCT_WINDOW_TITLE", "proof": {"format": "unity-app-info", "role": "product_name"}})
            if lines[1]:
                occurrences[-1]["display_projection_required"] = True
                return {"occurrences": occurrences, "types": {}, "dependencies": [], "proofs": {},
                        "pending": [{"reason": "UNITY_STARTUP_IDENTITY_REQUIRES_DISPLAY_PROJECTION",
                                     "scope": "display_dependency", "location": {"line_index": 1}}]}
        return {"occurrences": occurrences, "types": {}, "dependencies": [], "pending": [], "proofs": {}}
    from unity_native_resources import scan_resource_consumers
    _, leaves = _leaves(raw)
    consumers = scan_resource_consumers(raw, relative_path, method=method)
    bindings: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for binding in consumers["bindings"]:
        bindings.setdefault((binding["resource_type"], binding["resource_id"]), []).append(binding)
    # Both methods traverse the resource tree separately; one dispatches by
    # directory leaf, the other groups supported public resource types first.
    if method == "independent":
        leaves = [leaf for kind in (4, 5, 6, 11) for leaf in leaves if leaf["path"][0] == kind]
    occurrences, pending = [], list(consumers["pending"])
    found = set()
    for leaf in leaves:
        kind = leaf["path"][0]
        role = kind in {4, 5}
        try:
            if kind == 11:
                values, blocks, _ = _messages(leaf["data"], leaf["codepage"])
                identifiers = [identifier for low, high, _ in blocks for identifier in range(low, high + 1)]
            else:
                values = _leaf_strings(leaf)
                block = leaf["path"][1] if len(leaf["path"]) > 1 else None
                identifiers = [(block - 1) * 16 + index if kind == 6 and type(block) is int and block > 0 else None for index in range(len(values))]
        except (ValueError, UnicodeError, LookupError) as exc:
            relevant = role or any(key[0] == kind for key in bindings)
            pending.append({"reason": str(exc), "file": relative_path, "location": {"resource_path": leaf["path"]}, "affects_display": relevant})
            continue
        for index, value in enumerate(values):
            identifier = identifiers[index]
            evidence = bindings.get((kind, identifier), [])
            if evidence:
                found.add((kind, identifier))
            display = bool(value) and (role or bool(evidence))
            reason = ("EMPTY_RESOURCE_SLOT" if not value else
                      "WINDOWS_RESOURCE_CAPTION_ROLE" if role else
                      "BOUND_RESOURCE_DISPLAY_CONSUMER" if evidence else
                      "UNCLASSIFIED_RESOURCE_TABLE_VALUE")
            proof = {"format": "windows-resource", "type": kind}
            if role:
                proof["role"] = "menu_caption" if kind == 4 else "dialog_caption"
            if evidence:
                proof["consumers"] = evidence
            occurrences.append({"kind": "native_resource", "file": relative_path, "location": {"resource_path": leaf["path"], "entry_index": index}, "source": value, "classification": "display" if display else "uncertain" if value else "non_display", "reason": reason, "proof": proof})
    for key in sorted(set(bindings) - found):
        pending.append({"reason": "native_display_resource_missing", "scope": "display_dependency",
                        "file": relative_path, "resource_type": key[0], "resource_id": key[1],
                        "consumers": bindings[key]})
    return {"occurrences": occurrences, "types": {}, "dependencies": consumers["dependencies"], "pending": pending, "proofs": {}}


def patch_native(raw: bytes, replacements: list[dict[str, Any]], *, app_info: bool = False) -> bytes:
    if app_info:
        bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
        lines = raw[len(bom):].decode("utf-8").splitlines(keepends=True)
        seen = set()
        for row in replacements:
            index = row["location"]["line_index"]
            if index != 1 or index >= len(lines) or index in seen:
                raise ValueError("APP_INFO_BINDING_INVALID")
            seen.add(index)
            original = lines[index].rstrip("\r\n")
            if original != row["source"] or any(char in row["target"] for char in "\r\n\0"):
                raise ValueError("APP_INFO_SOURCE_OR_TARGET_INVALID")
            lines[index] = row["target"] + lines[index][len(original):]
        return bom + "".join(lines).encode("utf-8")
    from unity_pe import append_data, set_directory
    tree, leaves = _leaves(raw)
    by_path = {tuple(leaf["path"]): leaf for leaf in leaves}
    groups: dict[tuple[Any, ...], dict[int, str]] = {}
    for row in replacements:
        location = row["location"]
        key, index = tuple(location["resource_path"]), location["entry_index"]
        if key not in by_path or type(index) is not int:
            raise ValueError("NATIVE_RESOURCE_BINDING_INVALID")
        leaf = by_path[key]
        values = _leaf_strings(leaf)
        targets = groups.setdefault(key, {})
        if index < 0 or index >= len(values) or values[index] != row["source"] or index in targets or "\0" in row["target"]:
            raise ValueError("NATIVE_RESOURCE_SOURCE_MISMATCH")
        if key[0] == 11:
            pattern = r"%(?:[1-9][0-9]*(?:![^!]*!)?|[0nrbt!%])"
            if Counter(re.findall(pattern, row["source"])) != Counter(re.findall(pattern, row["target"])):
                raise ValueError("NATIVE_MESSAGE_CONTROL_MISMATCH")
        targets[index] = row["target"]
    for key, targets in groups.items():
        leaf, kind = by_path[key], key[0]
        if kind == 6:
            values = _string_table(leaf["data"])
            leaf["data"] = b"".join(struct.pack("<H", len(value.encode("utf-16-le")) // 2) + value.encode("utf-16-le") for index, source in enumerate(values) for value in [targets.get(index, source)])
        elif kind in {4, 5}:
            template = _menu(leaf["data"]) if kind == 4 else _dialog(leaf["data"])
            leaf["data"] = template.rebuild(targets)
        elif kind == 11:
            values, blocks, flags = _messages(leaf["data"], leaf["codepage"])
            leaf["data"] = _message_payload(values, blocks, flags, targets, leaf["codepage"])
    payload = _rebuild_tree(tree, 0)
    rebuilt, rva = append_data(raw, payload)
    final_payload = _rebuild_tree(tree, rva)
    pe = _u32(rebuilt, 0x3c)
    count, optional_size = _u16(rebuilt, pe + 6), _u16(rebuilt, pe + 20)
    table = pe + 24 + optional_size
    offset = None
    for index in range(count):
        section = _span(rebuilt, table + index * 40, 40)
        virtual_size, address, disk_size, disk = struct.unpack_from("<IIII", section, 8)
        if address <= rva < address + max(virtual_size, disk_size):
            offset = disk + rva - address
            break
    if offset is None:
        raise ValueError("NATIVE_RESOURCE_RELOCATION_INVALID")
    output = bytearray(rebuilt)
    output[offset:offset + len(final_payload)] = final_payload
    output = set_directory(bytes(output), 2, rva, len(final_payload))
    _, verified = _leaves(output)
    if [(leaf["path"], leaf["data"], leaf["codepage"], leaf["reserved"]) for leaf in leaves] != [(leaf["path"], leaf["data"], leaf["codepage"], leaf["reserved"]) for leaf in verified]:
        raise ValueError("NATIVE_RESOURCE_ROUNDTRIP_MISMATCH")
    return output
