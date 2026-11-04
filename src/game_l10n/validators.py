from __future__ import annotations

import hashlib
import re
from collections import Counter

from .models import GlossaryEntry, Segment, ValidationIssue
from .store import stable_hash

# Game/format tokens that must survive byte-for-byte. More patterns can be configured per project.
DEFAULT_PROTECTED_PATTERNS = (
    r"\{\{[^{}]*\}\}",
    r"\{[A-Za-z_][A-Za-z0-9_.:-]*\}",
    # No space flag: "50% off" is prose, not "% o". Add "% d" per project if an engine uses it.
    r"%(?:\d+\$)?[-+#0']*\d*(?:\.\d+)?[diouxXeEfFgGcrsa%]",
    r"<\/?[A-Za-z][^>]*>",
    # One alternation so "\nWord" yields only "\n" and no escape is counted twice.
    r"\\(?:[A-Za-z]+\[[^\]]*\]|[nrt]|[A-Za-z]+)",
    r"\$[A-Za-z_][A-Za-z0-9_]*",
)
_DEFAULT_PROTECTED_RE = re.compile("|".join(f"(?:{p})" for p in DEFAULT_PROTECTED_PATTERNS))

SUSPICIOUS_ADDITIONS = (
    re.compile(r"<script\b", re.IGNORECASE),
    re.compile(r"javascript\s*:", re.IGNORECASE),
    re.compile(r"\b(?:eval|exec)\s*\(", re.IGNORECASE),
    re.compile(r"\b(?:powershell|cmd\.exe|/bin/sh)\b", re.IGNORECASE),
)

# Latin (with accents), Greek and Cyrillic letters; scripts without word spacing are excluded.
_ALPHABETIC = "A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f\u0386\u0388-\u03ff\u0400-\u0481\u048a-\u04ff"


def contains_term(term: str, text: str, *, case_sensitive: bool = False) -> bool:
    """Match a source term without hitting it inside a longer Latin word ("Ice" in "Device").

    Boundaries apply only at alphabetic-letter edges, so CJK names followed by particles still match
    and a plain English plural ("Potions") still counts as the glossary term.
    """
    if not term:
        return False
    if not case_sensitive:
        term, text = term.casefold(), text.casefold()
    # Alphabetic letters only: digits ("HP100") and CJK particles still count as edges.
    letter = f"[{_ALPHABETIC}]"
    left = f"(?<!{letter})" if re.match(letter, term[0]) else ""
    right = f"(?:e?s)?(?!{letter})" if re.match(letter, term[-1]) else ""
    return re.search(left + re.escape(term) + right, text) is not None


def protected_tokens(text: str, extra_patterns: list[str] | None = None) -> Counter[str]:
    # Defaults scan as one leftmost alternation, so nested matches ("$d" in "%1$d") never double-count.
    tokens = _DEFAULT_PROTECTED_RE.findall(text)
    for pattern in extra_patterns or []:
        tokens.extend(re.findall(pattern, text))
    return Counter(tokens)


def empty_target_allowed(segment: Segment) -> bool:
    """Read optional-slot authority only from the bound inventory, never a result row."""
    metadata = segment.metadata
    contract = metadata.get("ui_empty_target")
    if not isinstance(contract, dict) or set(contract) != {
        "schema", "reason_code", "source_sha256", "composition_sha256", "evidence_sha256"
    }:
        return False
    if (
        contract["schema"] != "parr0t.ui-empty-target/v1"
        or contract["reason_code"] != "optional_grammatical_slot"
        or metadata.get("ui_slot_role") != "template_fragment"
    ):
        return False
    related = metadata.get("ui_composed_with_ids")
    pattern = metadata.get("ui_render_pattern")
    prefix = metadata.get("ui_fixed_target_prefix", "")
    suffix = metadata.get("ui_fixed_target_suffix", "")
    if (
        not isinstance(related, list) or not related
        or not all(isinstance(item, str) and item and item != segment.id for item in related)
        or len(set(related)) != len(related)
        or not isinstance(pattern, str) or "{value}" not in pattern
        or not isinstance(prefix, str) or not isinstance(suffix, str)
        or not (prefix.strip() or suffix.strip())
    ):
        return False
    if not all(
        isinstance(contract[key], str) and re.fullmatch(r"[0-9a-f]{64}", contract[key])
        for key in ("source_sha256", "composition_sha256", "evidence_sha256")
    ):
        return False
    composition = {
        "target_id": segment.id,
        "source_sha256": hashlib.sha256(segment.source.encode("utf-8")).hexdigest(),
        "ui_slot_role": metadata["ui_slot_role"],
        "ui_composed_with_ids": related,
        "ui_render_pattern": pattern,
        "ui_fixed_target_prefix": prefix,
        "ui_fixed_target_suffix": suffix,
    }
    return (
        contract["source_sha256"] == composition["source_sha256"]
        and contract["composition_sha256"] == stable_hash(composition)
    )


def validate_integrity(
    segment: Segment,
    candidate: str,
    glossary: list[GlossaryEntry],
    *,
    extra_patterns: list[str] | None = None,
    max_length_ratio: float = 4.0,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    exact_nonprinting_source = (
        not segment.source.strip() and candidate == segment.source
    )
    if not candidate.strip() and not (
        exact_nonprinting_source
        or (candidate == "" and empty_target_allowed(segment))
    ):
        return [ValidationIssue("empty", "Translation is empty")]

    source_tokens = protected_tokens(segment.source, extra_patterns)
    candidate_tokens = protected_tokens(candidate, extra_patterns)
    if source_tokens != candidate_tokens:
        missing = list((source_tokens - candidate_tokens).elements())
        added = list((candidate_tokens - source_tokens).elements())
        issues.append(
            ValidationIssue(
                "protected-token-mismatch",
                f"Protected tokens changed; missing={missing!r}, added={added!r}",
            )
        )

    candidate_folded = candidate.casefold()
    for entry in glossary:
        target = entry.target if entry.case_sensitive else entry.target.casefold()
        target_haystack = candidate if entry.case_sensitive else candidate_folded
        if (
            contains_term(entry.source, segment.source, case_sensitive=entry.case_sensitive)
            and target not in target_haystack
        ):
            issues.append(
                ValidationIssue(
                    "glossary-missing",
                    f"Required glossary target {entry.target!r} is absent for {entry.source!r}",
                )
            )

    for pattern in SUSPICIOUS_ADDITIONS:
        if pattern.search(candidate) and not pattern.search(segment.source):
            issues.append(
                ValidationIssue(
                    "script-intrusion",
                    f"Candidate introduced script-like content matching {pattern.pattern!r}",
                    retryable=False,
                )
            )

    slot_role = str(segment.metadata.get("ui_slot_role", "")).casefold()
    if slot_role in {"value", "state_value", "template_fragment"}:
        normalized_candidate = candidate.strip().casefold()
        fixed_prefix = segment.metadata.get("ui_fixed_target_prefix")
        fixed_suffix = segment.metadata.get("ui_fixed_target_suffix")
        if (
            isinstance(fixed_prefix, str)
            and fixed_prefix.strip()
            and normalized_candidate.startswith(fixed_prefix.strip().casefold())
        ):
            issues.append(
                ValidationIssue(
                    "ui-composition-duplication",
                    "Candidate repeats the fixed target prefix rendered outside this slot",
                )
            )
        if (
            isinstance(fixed_suffix, str)
            and fixed_suffix.strip()
            and normalized_candidate.endswith(fixed_suffix.strip().casefold())
        ):
            issues.append(
                ValidationIssue(
                    "ui-composition-duplication",
                    "Candidate repeats the fixed target suffix rendered outside this slot",
                )
            )

    max_chars = segment.metadata.get("ui_max_chars")
    if isinstance(max_chars, int) and not isinstance(max_chars, bool) and max_chars >= 0:
        if len(candidate) > max_chars:
            issues.append(
                ValidationIssue(
                    "ui-length-limit",
                    f"Candidate length {len(candidate)} exceeds the declared UI limit {max_chars}",
                )
            )

    if segment.source and len(candidate) > max(200, int(len(segment.source) * max_length_ratio)):
        issues.append(
            ValidationIssue(
                "length-outlier",
                f"Candidate length {len(candidate)} is an outlier for source length {len(segment.source)}",
            )
        )
    return issues
