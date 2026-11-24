"""Recover one provably missing RPG Maker MZ message edge control."""
from __future__ import annotations

import re


_PROTECTED = re.compile(r"\\(?:[A-Za-z]+\[[^\]\r\n]*\]|[.!|^{}$<>\\]|[A-Za-z]+)|%[1-9][0-9]*")
_EDGE = re.compile(r"\\[.!|^{}$<>]")


def restore_missing_edge_control(source: str, candidate: str) -> str:
    """Return a corrected candidate or reject an ambiguous control sequence.

    The target's prose is untouched. A changed candidate is not approved:
    callers must preserve its prior review history and require fresh evidence.
    """
    expected = list(_PROTECTED.finditer(source))
    source_tokens = [match.group() for match in expected]
    candidate_tokens = _PROTECTED.findall(candidate)
    if source_tokens == candidate_tokens:
        return candidate
    if len(source_tokens) != len(candidate_tokens) + 1:
        raise ValueError("E_MZ_EDGE_REPAIR_NOT_UNAMBIGUOUS")

    repairs = set()
    first, last = expected[0], expected[-1]
    if first.start() == 0 and _EDGE.fullmatch(first.group()):
        repaired = first.group() + candidate
        if _PROTECTED.findall(repaired) == source_tokens:
            repairs.add(repaired)
    if last.end() == len(source) and _EDGE.fullmatch(last.group()):
        repaired = candidate + last.group()
        if _PROTECTED.findall(repaired) == source_tokens:
            repairs.add(repaired)
    if len(repairs) != 1:
        raise ValueError("E_MZ_EDGE_REPAIR_NOT_UNAMBIGUOUS")
    return repairs.pop()
