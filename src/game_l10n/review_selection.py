from __future__ import annotations

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
