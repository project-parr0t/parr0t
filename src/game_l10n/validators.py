from __future__ import annotations

import re
from collections import Counter

from .models import GlossaryEntry, Segment, ValidationIssue

# Game/format tokens that must survive byte-for-byte. More patterns can be configured per project.
DEFAULT_PROTECTED_PATTERNS = (
    r"\{\{[^{}]*\}\}",
    r"\{[A-Za-z_][A-Za-z0-9_.:-]*\}",
    r"%(?:\d+\$)?[-+#0 ']*\d*(?:\.\d+)?[diouxXeEfFgGcrsa%]",
    r"<\/?[A-Za-z][^>]*>",
    r"\\[nrt]",
    r"\\[A-Za-z]+(?:\[[^\]]*\])?",
    r"\$[A-Za-z_][A-Za-z0-9_]*",
)

SUSPICIOUS_ADDITIONS = (
    re.compile(r"<script\b", re.IGNORECASE),
    re.compile(r"javascript\s*:", re.IGNORECASE),
    re.compile(r"\b(?:eval|exec)\s*\(", re.IGNORECASE),
    re.compile(r"\b(?:powershell|cmd\.exe|/bin/sh)\b", re.IGNORECASE),
)


def protected_tokens(text: str, extra_patterns: list[str] | None = None) -> Counter[str]:
    tokens: list[str] = []
    for pattern in (*DEFAULT_PROTECTED_PATTERNS, *(extra_patterns or [])):
        tokens.extend(re.findall(pattern, text))
    return Counter(tokens)


def validate_integrity(
    segment: Segment,
    candidate: str,
    glossary: list[GlossaryEntry],
    *,
    extra_patterns: list[str] | None = None,
    max_length_ratio: float = 4.0,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not candidate.strip():
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

    source_folded = segment.source.casefold()
    candidate_folded = candidate.casefold()
    for entry in glossary:
        source = entry.source if entry.case_sensitive else entry.source.casefold()
        target = entry.target if entry.case_sensitive else entry.target.casefold()
        source_haystack = segment.source if entry.case_sensitive else source_folded
        target_haystack = candidate if entry.case_sensitive else candidate_folded
        if source in source_haystack and target not in target_haystack:
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
