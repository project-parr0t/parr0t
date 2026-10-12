from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class AgentStatus(str, Enum):
    SUCCESS = "success"
    REFUSED = "refused"
    ERROR = "error"


class ReviewDecision(str, Enum):
    APPROVED = "approved"
    REVISED = "revised"
    REFUSED = "refused"
    ERROR = "error"


class FailureCode(str, Enum):
    EXPLICIT_PROVIDER_REFUSAL = "explicit_provider_refusal"
    MALFORMED_RESPONSE = "malformed_response"
    PROVIDER_ERROR = "provider_error"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    AUTHENTICATION_ERROR = "authentication_error"
    TIMEOUT = "timeout"
    INTEGRITY_INVALID = "integrity_invalid"
    MEANING_NOT_PRESERVED = "meaning_not_preserved"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BUDGET_ACCOUNTING_UNCERTAIN = "budget_accounting_uncertain"
    USER_ACCEPTED_AFTER_REVIEW_REFUSAL = "user_accepted_after_review_refusal"
    SOURCE_PRESERVED = "source_preserved"


@dataclass(slots=True, frozen=True)
class GlossaryEntry:
    source: str
    target: str
    note: str = ""
    case_sensitive: bool = False


@dataclass(slots=True, frozen=True)
class CharacterProfile:
    name: str
    aliases: tuple[str, ...] = ()
    personality: str = ""
    voice: str = ""
    formality: str = ""
    address_rules: str = ""
    speech_rules: tuple[str, ...] = ()
    example_lines: tuple[str, ...] = ()
    relationships: str = ""
    notes: str = ""


@dataclass(slots=True, frozen=True)
class ProjectContext:
    game_title: str
    source_language: str
    target_language: str
    genre: str = ""
    world: str = ""
    style_guide: str = ""
    age_rating: str = ""
    content_notes: tuple[str, ...] = ()
    fictional_content_note: str = (
        "This is fictional game localization. Translate faithfully while following the provider's policies."
    )


@dataclass(slots=True, frozen=True)
class Segment:
    id: str
    source: str
    speaker: str = ""
    scene: str = ""
    previous: str = ""
    next: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ContextPack:
    story_summary: str = ""
    global_style_rules: tuple[str, ...] = ()
    scene_summaries: dict[str, dict[str, Any]] = field(default_factory=dict)
    character_guides: dict[str, dict[str, Any]] = field(default_factory=dict)
    relationship_timeline: tuple[str, ...] = ()


@dataclass(slots=True)
class AgentEnvelope:
    status: AgentStatus
    text: str = ""
    reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ReviewEnvelope:
    decision: ReviewDecision
    text: str = ""
    meaning_preserved: bool = False
    issues: list[str] = field(default_factory=list)
    reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ValidationIssue:
    code: str
    message: str
    retryable: bool = True


@dataclass(slots=True)
class AttemptRecord:
    stage: str
    provider: str
    status: str
    reason: str = ""
    issues: list[str] = field(default_factory=list)
    duration_ms: int = 0
    ordinal: int = 1
    model: str = ""
    issue_code: str = ""
    candidate_generation: int = 0
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class TranslationResult:
    segment_id: str
    source: str
    text: str = ""
    status: str = "failed"
    translator: str = ""
    reviewer: str = ""
    from_cache: bool = False
    attempts: list[AttemptRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    candidate_generation: int = 0
    candidate_digest: str = ""
    review_outcome: str = ""
    acceptance_authority: str = ""
    resolution_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
