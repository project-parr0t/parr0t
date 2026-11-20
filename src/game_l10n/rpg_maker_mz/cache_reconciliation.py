"""Reconcile audited JavaScript controls from two offline inventory caches.

A rule must already establish native receiver ownership in its private,
digest-bound witness. Syntax, cache agreement, and ledger agreement are
additional checks, not substitutes for that semantic evidence. This command
proposes scope corrections; it does not write inventories or a Core receipt.
"""
from __future__ import annotations

import argparse
import json
import re
from bisect import bisect_left, bisect_right
from collections import Counter
from hashlib import sha256
from pathlib import Path

_ROLES = ("primary", "independent")
_CACHE_SCHEMAS = ("parr0t.primary-source-cache.v1", "parr0t.independent-structural-cache.v2")
_PENDING = "control_storage_with_projection"
_EXACT = re.compile(r"[0-9a-f]{64}")
_NAME = r"[A-Za-z_$][A-Za-z0-9_$]*"
_MEMBER = rf"{_NAME}(?:\.{_NAME})*"
_MEMBER_RE = re.compile(_MEMBER)
_QUOTED = r'''(?:"[^"\\\r\n]*"|'[^'\\\r\n]*')'''
_OPERAND = rf"(?:{_QUOTED}|{_NAME}(?:\.{_NAME}|\[[0-9]+\])*|[0-9]+)"
_EXPRESSION = rf"{_OPERAND}(?:\s*[+*-]\s*{_OPERAND})*"
_LEX = re.compile(
    r'''\s+|//[^\r\n]*|/\*[\s\S]*?\*/|"(?:[^"\\\r\n]|\\[^\r\n])*"|'''
    r"'(?:[^'\\\r\n]|\\[^\r\n])*'|"
    rf"""{_NAME}|[0-9]+|[^'"`/\s]"""
)
_LITERALS = re.compile(_QUOTED)
_SELECTORS = {
    "createElement": re.compile(r"[A-Za-z][A-Za-z0-9-]*"),
    "getContext": re.compile(r"2d|webgl2?|experimental-webgl|bitmaprenderer"),
    "addEventListener": re.compile(r"[A-Za-z][A-Za-z0-9_.:-]*"),
    "removeEventListener": re.compile(r"[A-Za-z][A-Za-z0-9_.:-]*"),
}
_PAINT = {"fillStyle", "strokeStyle", "shadowColor"}
_NAME_RE = re.compile(_NAME)
_NUMBER = re.compile(r"[0-9]+")
_INPUTS = {"rules", *(f"{role}_{kind}" for role in _ROLES for kind in ("cache", "inventory", "raw"))}


def _digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


def _fingerprint(obj):
    return _digest(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _span(value):
    if (isinstance(value, list) and len(value) == 2 and all(type(n) is int for n in value)
            and 0 <= value[0] < value[1]):
        return tuple(value)
    return None


def _location(loc):
    if not isinstance(loc, dict) or set(loc) != {"file", "span", "path"} or loc["path"] != []:
        return None
    span = _span(loc["span"])
    return (loc["file"], span) if isinstance(loc["file"], str) and loc["file"] and span else None


def _cache_files(cache, role):
    if not isinstance(cache, dict) or cache.get("schema") != _CACHE_SCHEMAS[_ROLES.index(role)]:
        raise ValueError(f"unsupported {role} cache schema")
    records = cache.get("files")
    if role == "primary" and isinstance(records, dict):
        entries = records.items()
    elif role == "independent" and isinstance(records, list):
        entries = ((entry.get("path") if isinstance(entry, dict) else None, entry) for entry in records)
    else:
        raise ValueError(f"invalid {role} cache file collection")
    files = {}
    for path, entry in entries:
        if (not isinstance(path, str) or not path or path in files or not isinstance(entry, dict)
                or not isinstance(entry.get("text"), str) or not isinstance(entry.get("sha256"), str)
                or not _EXACT.fullmatch(entry["sha256"])):
            raise ValueError(f"invalid or duplicate {role} cache file record")
        files[path] = entry
    return files


def _code_tokens(text, start, end):
    tokens = []
    while start < end:
        # Unsupported templates, regexp literals and division stay pending.
        match = _LEX.match(text, start, end)
        if not match:
            return []
        value = match[0]
        if not value.isspace() and not value.startswith(("//", "/*")):
            tokens.append((value, start, match.end()))
        start = match.end()
    return tokens


def _member_end(values, i):
    if i >= len(values) or not _NAME_RE.fullmatch(values[i]):
        return i
    i += 1
    while i + 1 < len(values) and values[i] == "." and _NAME_RE.fullmatch(values[i + 1]):
        i += 2
    return i


def _operand_end(values, i):
    if i == len(values):
        return i
    if _NUMBER.fullmatch(values[i]) or _LITERALS.fullmatch(values[i]):
        return i + 1
    if not _NAME_RE.fullmatch(values[i]):
        return i
    i += 1
    while i < len(values):
        if i + 1 < len(values) and values[i] == "." and _NAME_RE.fullmatch(values[i + 1]):
            i += 2
        elif (i + 2 < len(values) and values[i] == "[" and values[i + 2] == "]"
              and _NUMBER.fullmatch(values[i + 1])):
            i += 3
        else:
            break
    return i


def _token_literals(tokens, member, kind):
    values = [t[0] for t in tokens]
    if not values or values[-1] != ";":
        return {}
    start = 0
    if (kind == "selector" and len(values) >= 3 and values[0] in {"var", "let", "const"}
            and _NAME_RE.fullmatch(values[1]) and values[2] == "="):
        start = 3
    tail = _member_end(values, start)
    if "".join(values[start:tail]) != member:
        return {}
    if kind == "selector":
        if len(values) - tail < 4 or values[tail] != "(" or not _LITERALS.fullmatch(values[tail + 1]):
            return {}
        literal = tail + 1
        i = literal + 1
        if member.rsplit(".", 1)[-1] in {"addEventListener", "removeEventListener"}:
            if values[i] != "," or (callback := _member_end(values, i + 1)) == i + 1:
                return {}
            i = callback
            if i + 1 < len(values) and values[i] == "," and values[i + 1] in {"true", "false"}:
                i += 2
        chosen = [literal] if values[i:] == [")", ";"] else []
    elif tail >= len(values) or values[tail] != "=":
        return {}
    elif kind == "media_type":
        first = _member_end(values, tail + 1)
        end = _member_end(values, first + 3)
        chosen = [first + 1] if (first > tail + 1 and end > first + 3 and end == len(values) - 1
                                 and values[first:first + 3] in (["+", '"/"', "+"], ["+", "'/'", "+"])) else []
    else:
        chosen, i = [], tail + 1
        while True:
            end = _operand_end(values, i)
            if end == i or end >= len(values):
                return {}
            if end == i + 1 and _LITERALS.fullmatch(values[i]):
                chosen.append(i)
            if end == len(values) - 1:
                break
            if values[end] not in {"+", "-", "*", "/"}:
                return {}
            i = end + 1
    return {(tokens[i][1], tokens[i][2]): values[i][1:-1] for i in chosen}


def _pattern(member, kind):
    name = re.escape(member)
    method = member.rsplit(".", 1)[-1]
    if kind == "selector":
        callback = (rf",\s*{_MEMBER}\s*(?:,\s*(?:true|false)\s*)?"
                    if method in {"addEventListener", "removeEventListener"} else "")
        return re.compile(rf"\s*(?:(?:var|let|const)\s+{_NAME}\s*=\s*)?{name}\s*"
                          rf"\(\s*(?P<literal>{_QUOTED})\s*{callback}\)\s*;\s*")
    if kind == "media_type":
        return re.compile(rf'''\s*{name}\s*=\s*{_MEMBER}\s*\+\s*(?P<literal>"/"|'/')\s*\+\s*{_MEMBER}\s*;\s*''')
    return re.compile(rf"\s*{name}\s*=\s*(?P<expression>{_EXPRESSION})\s*;\s*")


def _textual_literals(pattern, text, start, end, kind):
    match = pattern.fullmatch(text, start, end)
    if match is None:
        return {}
    if kind != "canvas_paint":
        return {match.span("literal"): match["literal"][1:-1]}
    return {item.span(): item[0][1:-1]
            for item in _LITERALS.finditer(text, *match.span("expression"))}


def _rule_index(files, rules):
    windows, hits, witnesses, patterns = {}, {}, {}, {}
    for rule in rules:
        file, kind, member = rule["file"], rule["kind"], rule["member"]
        span = _span(rule["span"])
        witness = rule["receiver_witness"]
        outer = _span(witness["span"])
        method = member.rsplit(".", 1)[-1]
        if (not span or not outer or not outer[0] <= span[0] < span[1] <= outer[1]
                or not _EXACT.fullmatch(rule["sha256"]) or not _EXACT.fullmatch(witness["sha256"])
                or "." not in member or not _MEMBER_RE.fullmatch(member)
                or not ((kind == "selector" and method in _SELECTORS)
                        or (kind == "canvas_paint" and method in _PAINT)
                        or (kind == "media_type" and method == "type"))):
            raise ValueError("invalid audited consumer rule")
        start, end = span
        windows.setdefault(file, []).append(span)
        left, right = (index.get(file) for index in files)
        if left is None or right is None or left["sha256"] != right["sha256"]:
            continue
        left, right = left["text"], right["text"]
        key = (file, outer, witness["sha256"])
        if key not in witnesses:
            a, b = outer
            tokens = (_code_tokens(left, a, b)
                      if all(b <= len(text) and _digest(text[a:b]) == key[2] for text in (left, right)) else [])
            witnesses[key] = tokens, [token[1] for token in tokens]
        tokens, starts = witnesses[key]
        if not tokens or not all(_digest(text[start:end]) == rule["sha256"] for text in (left, right)):
            continue
        i, j = bisect_left(starts, start), bisect_left(starts, end)
        if i == j or tokens[j - 1][2] > end or (i and (tokens[i - 1][2] > start or tokens[i - 1][0] not in {";", "{", "}"})):
            continue
        pattern_key = (member, kind)
        if pattern_key not in patterns:
            patterns[pattern_key] = _pattern(member, kind)
        candidates = _token_literals(tokens[i:j], member, kind)
        if not candidates or candidates != _textual_literals(patterns[pattern_key], right, start, end, kind):
            continue
        rule_id = _fingerprint(rule)
        for location, value in candidates.items():
            if kind == "selector" and not _SELECTORS[method].fullmatch(value):
                continue
            address = (file, location)
            hit = (value, rule_id)
            hits[address] = hit if hits.get(address, hit) == hit else None
    # Prefix maximums support one binary search per occurrence, even when audited
    # ranges overlap. Retain the individual starts; partial overlap is not coverage.
    window_index = {}
    for file, intervals in windows.items():
        starts, ends, furthest = [], [], 0
        for start, end in sorted(intervals):
            furthest = max(furthest, end)
            starts.append(start)
            ends.append(furthest)
        window_index[file] = starts, ends
    return hits, window_index


def _covered(windows, file, span):
    starts, ends = windows.get(file, ((), ()))
    i = bisect_right(starts, span[0]) - 1
    return i >= 0 and span[1] <= ends[i]


def _inventory_index(document):
    rows = {}
    for row in document["segments"]:
        key = row["metadata"].get("inventory_comparison_key")
        if key is None:
            continue
        if key in rows:
            raise ValueError("duplicate inventory comparison key")
        rows[key] = row
    return rows


def reconcile(caches, inventories, raw_ledgers, rules, bindings):
    """Return evidence-backed proposals; unresolved counts partition all misses."""
    if len(inventories) != 2 or len(raw_ledgers) != 2 or raw_ledgers[0] is raw_ledgers[1]:
        raise ValueError("reconciliation requires two independent inventories and raw ledgers")
    scopes = [doc["coverage"]["scope_correction"] for doc in inventories]
    if scopes[0] != scopes[1] or any(inventories[0][k] == inventories[1][k]
                                     for k in ("method", "implementation_fingerprint")):
        raise ValueError("independent inventory scope is not bound")
    files = [_cache_files(cache, role) for role, cache in zip(_ROLES, caches, strict=True)]
    hits, windows = _rule_index(files, rules)
    primary, independent = map(_inventory_index, inventories)
    eligible, misses, seen = {}, Counter(), set()
    pending = 0
    for record in scopes[0]["records"]:
        if record["classification"] != _PENDING or record["projection_ids"]:
            continue
        pending += 1
        key = record["comparison_key"]
        if key in seen:
            raise ValueError("duplicate pending comparison key")
        seen.add(key)
        a, b = primary.get(key), independent.get(key)
        if a is None or b is None:
            misses["inventory_row_missing"] += 1
            continue
        p, q = (row["metadata"].get("source_location") for row in (a, b))
        if p != q:
            misses["location_disagrees"] += 1
            continue
        location = _location(p)
        if location is None:
            misses["location_unsupported"] += 1
            continue
        hit = hits.get(location)
        if hit is None:
            misses["rule_not_proven" if _covered(windows, *location) else "outside_rules"] += 1
            continue
        value, rule = hit
        digest = record["source_sha256"]
        if a["source"] != value or b["source"] != value or _digest(value) != digest:
            misses["source_disagrees"] += 1
            continue
        eligible[key] = ((*location, digest), rule, p, (a, b))
    expected = Counter(values[0] for values in eligible.values())
    ledgers = []
    for ledger in raw_ledgers if eligible else ():
        counts = Counter()
        for row in ledger["occurrences"]:
            if (location := _location(row["location"])) is not None:
                token = (*location, row["source_sha256"])
                if token in expected:
                    counts[token] += 1
        ledgers.append(counts)
    proposals = []
    for key, (physical, rule, location, rows) in eligible.items():
        if expected[physical] != 1 or any(counts[physical] != 1 for counts in ledgers):
            misses["raw_occurrence_not_unique"] += 1
            continue
        evidence = []
        for role, scanner, doc, row in zip(_ROLES, ("js_tokens", "statement_grammar"), inventories, rows, strict=True):
            fact = {
                "scanner": scanner, "method": doc["method"],
                "implementation_fingerprint": doc["implementation_fingerprint"],
                "occurrence_id": row["id"], "comparison_key": key,
                "location": location, "source_sha256": physical[-1], "rule_sha256": rule,
                "cache_sha256": bindings[f"{role}_cache"], "raw_ledger_sha256": bindings[f"{role}_raw"],
            }
            evidence.append({**fact, "evidence_sha256": _fingerprint(fact)})
        proposals.append({"comparison_key": key, "evidence": evidence})
    return {"schema": "parr0t.mz-cache-reconciliation-report.v1", "pending": pending,
            "proposed": len(proposals), "unresolved": dict(sorted(misses.items())), "proposals": proposals}


def _bound_json(workspace, path, expected, seen):
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("input escaped the workspace")
    resolved = (workspace / relative).resolve(strict=True)
    if not resolved.is_relative_to(workspace) or not resolved.is_file():
        raise ValueError("input escaped the workspace")
    if resolved in seen:
        raise ValueError("input bindings must name distinct files")
    seen.add(resolved)
    data = resolved.read_bytes()
    if sha256(data).hexdigest() != expected:
        raise ValueError("bound input digest changed")
    return json.loads(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--request", required=True, help="Workspace-relative JSON input binding")
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--report", required=True, help="Fresh workspace-relative private output")
    args = parser.parse_args()
    workspace = args.workspace.resolve(strict=True)
    seen = set()
    request = _bound_json(workspace, args.request, args.request_sha256, seen)
    if request.get("schema") != "parr0t.mz-cache-reconciliation-request.v1" or set(request["bindings"]) != _INPUTS:
        raise ValueError("invalid cache reconciliation request")
    bindings = request["bindings"]
    data = {key: _bound_json(workspace, entry["path"], entry["sha256"], seen) for key, entry in bindings.items()}
    report = reconcile(*(tuple(data[f"{role}_{kind}"] for role in _ROLES) for kind in ("cache", "inventory", "raw")),
                       data["rules"], {key: entry["sha256"] for key, entry in bindings.items()})
    relative = Path(args.report)
    output = (workspace / relative).resolve()
    if relative.is_absolute() or not output.is_relative_to(workspace):
        raise ValueError("output escaped the workspace")
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, separators=(",", ":"))
        stream.write("\n")


if __name__ == "__main__":
    main()
