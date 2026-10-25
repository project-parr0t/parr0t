from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from .models import Segment
from .store import stable_hash


REVIEW_SELECTION_POLICY_VERSION = 1
DEFAULT_STRONG_REVIEW_SAMPLE_PERCENT = 5.0
DEFAULT_STRONG_REVIEW_CATEGORIES = (
    "allusion",
    "critical_logic",
    "parody",
    "quest_title",
    "script_sensitive",
    "wordplay",
)

_CATEGORY_KEYS = (
    "review_category",
    "text_type",
    "context_type",
    "category",
    "kind",
    "type",
)
_EXPLICIT_RISK_FLAGS = (
    "requires_strong_review",
    "high_risk_review",
    "reference_cue",
    "parody",
    "allusion",
    "wordplay",
    "logic_facing",
)

_TARGET_SCRIPT_PATTERNS = {
    "ko": re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7a3]"),
    "ja": re.compile(r"[\u3040-\u30ff\u3400-\u9fff]"),
    "zh": re.compile(r"[\u3400-\u9fff]"),
    "ru": re.compile(r"[\u0400-\u04ff]"),
}
_SHORT_IMMUTABLE_TOKEN = re.compile(r"[A-Z0-9_.:+/%\-]{1,12}")
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
_TARGET_LANGUAGE_EXCEPTIONS = {
    "acronym",
    "control_token",
    "immutable_identifier",
    "proper_name",
    "required_foreign_text",
}


def _normalized(value: Any) -> str:
    return str(value).strip().casefold().replace("-", "_").replace(" ", "_")


def review_category(segment: Segment) -> str:
    """Return a stable, text-free review category from extraction metadata."""

    for key in _CATEGORY_KEYS:
        value = segment.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return _normalized(value)
    if segment.speaker:
        return "dialogue"
    if segment.scene or segment.previous or segment.next:
        return "narrative"
    return "uncategorized"


def strong_review_selection_reasons(
    segment: Segment,
    *,
    sample_percent: float = DEFAULT_STRONG_REVIEW_SAMPLE_PERCENT,
    high_risk_categories: Iterable[str] = DEFAULT_STRONG_REVIEW_CATEGORIES,
) -> list[str]:
    """Select strong review without reading or classifying the segment text."""

    if not 0.0 <= sample_percent <= 100.0:
        raise ValueError("strong_review_sample_percent must be between 0 and 100")
    category = review_category(segment)
    configured = {_normalized(item) for item in high_risk_categories if str(item).strip()}
    reasons: list[str] = []
    if category in configured:
        reasons.append("high_risk_category")
    if any(segment.metadata.get(key) is True for key in _EXPLICIT_RISK_FLAGS):
        reasons.append("explicit_high_risk_flag")
    if sample_percent:
        digest = stable_hash(
            {
                "policy_version": REVIEW_SELECTION_POLICY_VERSION,
                "segment_id": segment.id,
                "category": category,
            }
        )
        bucket = int(digest[:12], 16) / float(16**12) * 100.0
        if bucket < sample_percent:
            reasons.append("deterministic_sample")
    return reasons


def target_language_review_reasons(
    candidate: str,
    target_language: str,
    metadata: dict[str, Any] | None = None,
) -> list[str]:
    """Flag likely language contamination without making a semantic judgment.

    Short all-caps identifiers and symbol-only values remain eligible for ordinary
    triage. Other flagged rows require correction or an evidenced metadata exception;
    a reviewer approval alone cannot waive the locked target language.
    """

    language = str(target_language).strip().casefold().split("-", 1)[0]
    expected = _TARGET_SCRIPT_PATTERNS.get(language)
    text = str(candidate).strip()
    if expected is None or not text:
        return []
    if _SHORT_IMMUTABLE_TOKEN.fullmatch(text):
        return []
    metadata = metadata or {}
    exception = metadata.get("target_language_exception")
    evidence = metadata.get("target_language_exception_evidence_sha256")
    if (
        exception in _TARGET_LANGUAGE_EXCEPTIONS
        and isinstance(evidence, str)
        and len(evidence) == 64
        and all(character in "0123456789abcdef" for character in evidence)
    ):
        return []
    target_script_count = len(expected.findall(text))
    if target_script_count:
        latin_letters = sum(
            character.isascii() and character.isalpha() for character in text
        )
        if (
            language != "en"
            and len(_LATIN_WORD.findall(text)) >= 4
            and latin_letters >= 24
            and latin_letters > target_script_count * 3
        ):
            return ["target_script_contamination"]
        return []
    if not any(character.isalpha() for character in text):
        return []
    return ["target_script_absent"]
