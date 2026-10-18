from __future__ import annotations

import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from .errors import ProviderError, ProviderFailure
from .models import (
    AgentEnvelope,
    AgentStatus,
    AttemptRecord,
    CharacterProfile,
    ContextPack,
    FailureCode,
    GlossaryEntry,
    ProjectContext,
    ReviewDecision,
    ReviewEnvelope,
    ReviewVerdict,
    ReviewVerdictEnvelope,
    Segment,
    TranslationResult,
)
from .prompts import (
    PROMPT_VERSION,
    REVIEW_ISSUE_CODES,
    REVIEW_JUDGE_SCHEMA,
    REVIEW_REWRITE_SCHEMA,
    TRANSLATION_SCHEMA,
    review_judge_prompt,
    review_rewrite_prompt,
    translation_prompt,
)
from .providers import ModelProvider, ProviderRegistry
from .review_selection import (
    DEFAULT_STRONG_REVIEW_CATEGORIES,
    DEFAULT_STRONG_REVIEW_SAMPLE_PERCENT,
    review_category,
    strong_review_selection_reasons,
)
from .store import AuditLog, ResultStore, stable_hash
from .validators import validate_integrity


@dataclass(slots=True)
class OrchestratorConfig:
    translator_chain: list[str]
    reviewer_chain: list[str]
    require_review: bool = True
    max_workers: int = 1
    max_length_ratio: float = 4.0
    extra_protected_patterns: list[str] = field(default_factory=list)
    use_style_memory: bool = True
    style_anchor_limit: int = 6
    retry_refusals_once: bool = True
    grace_retries: int = 0
    grace_retry_codes: list[str] = field(
        default_factory=lambda: [
            FailureCode.MALFORMED_RESPONSE.value,
            FailureCode.PROVIDER_ERROR.value,
            FailureCode.RATE_LIMITED.value,
            FailureCode.TIMEOUT.value,
            FailureCode.INTEGRITY_INVALID.value,
        ]
    )
    defer_fallbacks_until_primary_complete: bool = False
    preserve_source_on_failure: bool = False
    review_refusal_policy: str = "fallback"
    review_max_rounds: int = 2
    rewriter_chain: list[str] = field(default_factory=list)
    review_triager_chain: list[str] = field(default_factory=list)
    strong_review_sample_percent: float = DEFAULT_STRONG_REVIEW_SAMPLE_PERCENT
    strong_review_high_risk_categories: list[str] = field(
        default_factory=lambda: list(DEFAULT_STRONG_REVIEW_CATEGORIES)
    )


T = TypeVar("T")


class LocalizationOrchestrator:
    """Routes work and observes protocol health; it never judges translation meaning."""

    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        config: OrchestratorConfig,
        project: ProjectContext,
        glossary: list[GlossaryEntry] | None = None,
        characters: list[CharacterProfile] | None = None,
        context_pack: ContextPack | None = None,
        work_dir: Path,
    ):
        if not config.translator_chain:
            raise ValueError("translator_chain must not be empty")
        if config.require_review and not config.reviewer_chain:
            raise ValueError("reviewer_chain must not be empty when require_review is true")
        if config.grace_retries < 0:
            raise ValueError("grace_retries must be zero or greater")
        if config.review_max_rounds < 1:
            raise ValueError("review_max_rounds must be one or greater")
        if not 0.0 <= config.strong_review_sample_percent <= 100.0:
            raise ValueError("strong_review_sample_percent must be between 0 and 100")
        if config.review_refusal_policy not in {
            "fallback",
            "accept_candidate_unreviewed",
            "source_preserve",
        }:
            raise ValueError(
                "review_refusal_policy must be fallback, accept_candidate_unreviewed, or source_preserve"
            )
        self.registry = registry
        self.config = config
        self.project = project
        self.glossary = glossary or []
        self.characters = {item.name: item for item in (characters or [])}
        self.context_pack = context_pack
        self.work_dir = work_dir.resolve()
        self.store = ResultStore(self.work_dir / ".game-l10n" / "cache.sqlite3")
        self.audit = AuditLog(self.work_dir / ".game-l10n" / "audit.jsonl")
        self._style_memory: dict[str, list[dict[str, str]]] = {}
        self._memory_lock = threading.Lock()

    def _character(self, speaker: str) -> CharacterProfile | None:
        if speaker in self.characters:
            return self.characters[speaker]
        folded = speaker.casefold()
        for character in self.characters.values():
            if folded in {alias.casefold() for alias in character.aliases}:
                return character
        return None

    def _anchors(self, speaker: str) -> list[dict[str, str]]:
        if not self.config.use_style_memory or not speaker:
            return []
        with self._memory_lock:
            return list(self._style_memory.get(speaker, []))[-self.config.style_anchor_limit :]

    def _remember(self, segment: Segment, text: str) -> None:
        if not self.config.use_style_memory or not segment.speaker or not text:
            return
        with self._memory_lock:
            entries = self._style_memory.setdefault(segment.speaker, [])
            item = {"source": segment.source, "approved_translation": text}
            if item not in entries:
                entries.append(item)

    def _remember_result(self, segment: Segment, result: TranslationResult) -> None:
        if result.status in {"translated", "reviewed", "accepted_unreviewed"}:
            self._remember(segment, result.text)

    def _cache_key(self, segment: Segment, anchors: list[dict[str, str]]) -> str:
        return stable_hash(
            {
                "prompt_version": PROMPT_VERSION,
                "project": asdict(self.project),
                "segment": asdict(segment),
                "glossary": [asdict(item) for item in self.glossary],
                "character": asdict(self._character(segment.speaker)) if self._character(segment.speaker) else None,
                "context_pack": asdict(self.context_pack) if self.context_pack else None,
                "style_anchors": anchors,
                "orchestrator": asdict(self.config),
                "providers": self.registry.fingerprints(
                    list(
                        dict.fromkeys(
                            self.config.translator_chain
                            + self.config.review_triager_chain
                            + self.config.reviewer_chain
                            + self.config.rewriter_chain
                        )
                    )
                ),
            }
        )

    @staticmethod
    def _translation_envelope(raw: dict[str, Any]) -> AgentEnvelope:
        try:
            status = AgentStatus(raw.get("status"))
        except (ValueError, TypeError) as exc:
            raise ProviderError("Translator returned an unknown status") from exc
        text = raw.get("text", "")
        reason = raw.get("reason", "")
        if not isinstance(text, str) or not isinstance(reason, str):
            raise ProviderError("Translator response fields have invalid types")
        if status is AgentStatus.SUCCESS and not text.strip():
            raise ProviderError("Translator reported success with empty text")
        return AgentEnvelope(status=status, text=text, reason=reason, raw=raw)

    @staticmethod
    def _review_envelope(raw: dict[str, Any]) -> ReviewEnvelope:
        try:
            decision = ReviewDecision(raw.get("decision"))
        except (ValueError, TypeError) as exc:
            raise ProviderError("Reviewer returned an unknown decision") from exc
        issues = raw.get("issues", [])
        text = raw.get("text", "")
        if not isinstance(issues, list) or not all(isinstance(item, str) for item in issues):
            raise ProviderError("Reviewer issues must be a list of strings")
        if not isinstance(text, str):
            raise ProviderError("Reviewer text must be a string")
        return ReviewEnvelope(
            decision=decision,
            text=text,
            meaning_preserved=bool(raw.get("meaning_preserved", False)),
            issues=issues,
            reason=str(raw.get("reason", "")),
            raw=raw,
        )

    @staticmethod
    def _review_verdict_envelope(raw: dict[str, Any]) -> ReviewVerdictEnvelope:
        # Protocol-v1 reviewers returned a complete candidate on every call. Accept
        # that shape for compatibility while new reviewers use the compact verdict.
        if "verdict" not in raw and "decision" in raw:
            legacy = LocalizationOrchestrator._review_envelope(raw)
            mapping = {
                ReviewDecision.APPROVED: ReviewVerdict.OK,
                ReviewDecision.REVISED: ReviewVerdict.OK,
                ReviewDecision.REFUSED: ReviewVerdict.REFUSED,
                ReviewDecision.ERROR: ReviewVerdict.ERROR,
            }
            return ReviewVerdictEnvelope(
                verdict=mapping[legacy.decision],
                issues=legacy.issues,
                text=legacy.text if legacy.meaning_preserved else "",
                raw=raw,
            )
        try:
            verdict = ReviewVerdict(raw.get("verdict"))
        except (ValueError, TypeError) as exc:
            raise ProviderError("Reviewer returned an unknown compact verdict") from exc
        issues = raw.get("issues", [])
        if not isinstance(issues, list) or not all(isinstance(item, str) for item in issues):
            raise ProviderError("Reviewer issue codes must be a list of strings")
        if len(issues) > 4 or any(item not in REVIEW_ISSUE_CODES for item in issues):
            raise ProviderError("Reviewer returned invalid compact issue codes")
        if verdict is ReviewVerdict.REWRITE and not issues:
            raise ProviderError("Rewrite verdict requires at least one issue code")
        if verdict is not ReviewVerdict.REWRITE and issues:
            raise ProviderError("Only rewrite verdicts may include issue codes")
        return ReviewVerdictEnvelope(verdict=verdict, issues=issues, raw=raw)

    @staticmethod
    def _usage(raw: dict[str, Any]) -> dict[str, int]:
        usage = raw.get("_usage", {})
        if not isinstance(usage, dict):
            return {}
        return {
            str(key): int(value)
            for key, value in usage.items()
            if isinstance(value, int) and value >= 0
        }

    def _record(self, result: TranslationResult, record: AttemptRecord) -> None:
        # The parent/orchestration plane receives categorical control metadata only.
        # Provider explanations and reviewer prose can echo private game dialogue, so
        # they must remain inside the short-lived provider call and never propagate.
        reason = record.reason.casefold()
        if not record.ordinal:
            record.ordinal = 1
        if record.status == "refused":
            record.reason = FailureCode.EXPLICIT_PROVIDER_REFUSAL.value
        elif record.status == "error":
            if record.issue_code in {item.value for item in FailureCode}:
                record.reason = record.issue_code
            elif "quota" in reason or "insufficient" in reason:
                record.reason = FailureCode.QUOTA_EXHAUSTED.value
            elif "rate" in reason and "limit" in reason:
                record.reason = FailureCode.RATE_LIMITED.value
            elif any(token in reason for token in ("auth", "api key", "unauthorized", "forbidden")):
                record.reason = FailureCode.AUTHENTICATION_ERROR.value
            elif any(token in reason for token in ("timeout", "timed out")):
                record.reason = FailureCode.TIMEOUT.value
            elif any(token in reason for token in ("json", "schema", "format")):
                record.reason = FailureCode.MALFORMED_RESPONSE.value
            else:
                record.reason = FailureCode.PROVIDER_ERROR.value
        elif record.status == "queued":
            record.reason = "fallback_queued"
        elif record.status == "rejected":
            record.reason = record.issue_code or FailureCode.INTEGRITY_INVALID.value
        else:
            record.reason = ""
            record.issue_code = ""
        record.issue_code = record.reason
        if record.provider != "deterministic-validator" and record.issues:
            record.issues = [f"provider_reported_issues:{len(record.issues)}"]
        result.attempts.append(record)
        self.audit.append(
            {
                "segment_id": result.segment_id,
                "stage": record.stage,
                "provider": record.provider,
                "status": record.status,
                "reason": record.reason,
                "issues": record.issues,
                "duration_ms": record.duration_ms,
                "ordinal": record.ordinal,
                "model": record.model,
                "issue_code": record.issue_code,
                "candidate_generation": record.candidate_generation,
                "usage": record.usage,
            }
        )

    @staticmethod
    def _provider_model(provider: ModelProvider) -> str:
        return str(provider.fingerprint().get("model", ""))

    @staticmethod
    def _failure(exc: Exception) -> tuple[str, bool]:
        if isinstance(exc, ProviderFailure):
            return exc.code, exc.retryable
        reason = str(exc).casefold()
        if "quota" in reason or "insufficient balance" in reason:
            return FailureCode.QUOTA_EXHAUSTED.value, False
        if "rate" in reason and "limit" in reason:
            return FailureCode.RATE_LIMITED.value, True
        if any(token in reason for token in ("auth", "api key", "unauthorized", "forbidden")):
            return FailureCode.AUTHENTICATION_ERROR.value, False
        if any(token in reason for token in ("timeout", "timed out")):
            return FailureCode.TIMEOUT.value, True
        if any(token in reason for token in ("json", "schema", "format")):
            return FailureCode.MALFORMED_RESPONSE.value, True
        return FailureCode.PROVIDER_ERROR.value, True

    def _can_grace_retry(self, code: str, ordinal: int) -> bool:
        return ordinal <= self.config.grace_retries and code in self.config.grace_retry_codes

    @staticmethod
    def _set_candidate(result: TranslationResult, text: str) -> None:
        digest = stable_hash(text)
        if digest != result.candidate_digest:
            result.candidate_generation = max(1, result.candidate_generation + 1)
            result.candidate_digest = digest

    def _call_translation(
        self,
        provider: ModelProvider,
        segment: Segment,
        anchors: list[dict[str, str]],
        *,
        refusal_retry: bool = False,
    ) -> AgentEnvelope:
        system, user = translation_prompt(
            self.project,
            segment,
            self.glossary,
            self._character(segment.speaker),
            self.context_pack,
            anchors,
            refusal_retry,
        )
        return self._translation_envelope(
            provider.generate_json(
                system=system,
                user=user,
                schema=TRANSLATION_SCHEMA,
                schema_name="game_translation",
            )
        )

    def _call_review_judge(
        self,
        provider: ModelProvider,
        segment: Segment,
        candidate: str,
        anchors: list[dict[str, str]],
        *,
        refusal_retry: bool = False,
    ) -> ReviewVerdictEnvelope:
        plain_verdict = provider.fingerprint().get("review_verdict_format") == "plain"
        system, user = review_judge_prompt(
            self.project,
            segment,
            candidate,
            self.glossary,
            self._character(segment.speaker),
            self.context_pack,
            anchors,
            refusal_retry,
            plain_verdict,
        )
        return self._review_verdict_envelope(
            provider.generate_review_verdict(
                system=system,
                user=user,
                schema=REVIEW_JUDGE_SCHEMA,
                schema_name="game_translation_review_judge",
            )
        )

    def _call_review_rewrite(
        self,
        provider: ModelProvider,
        segment: Segment,
        candidate: str,
        issue_codes: list[str],
        anchors: list[dict[str, str]],
        *,
        refusal_retry: bool = False,
    ) -> AgentEnvelope:
        system, user = review_rewrite_prompt(
            self.project,
            segment,
            candidate,
            issue_codes,
            self.glossary,
            self._character(segment.speaker),
            self.context_pack,
            anchors,
            refusal_retry,
        )
        return self._translation_envelope(
            provider.generate_json(
                system=system,
                user=user,
                schema=REVIEW_REWRITE_SCHEMA,
                schema_name="game_translation_review_rewrite",
            )
        )

    def _translate_with_refusal_retry(
        self,
        result: TranslationResult,
        provider: ModelProvider,
        segment: Segment,
        anchors: list[dict[str, str]],
    ) -> AgentEnvelope | None:
        ordinal = 0
        refusal_retried = False
        retry_stage = "translate-grace-retry"
        while True:
            ordinal += 1
            started = time.monotonic()
            stage = "translate" if ordinal == 1 else retry_stage
            try:
                envelope = self._call_translation(
                    provider, segment, anchors, refusal_retry=ordinal > 1
                )
                duration = int((time.monotonic() - started) * 1000)
                self._record(
                    result,
                    AttemptRecord(
                        stage,
                        provider.name,
                        envelope.status.value,
                        envelope.reason,
                        duration_ms=duration,
                        ordinal=ordinal,
                        model=self._provider_model(provider),
                        candidate_generation=result.candidate_generation,
                        usage=self._usage(envelope.raw),
                    ),
                )
            except Exception as exc:
                duration = int((time.monotonic() - started) * 1000)
                code, retryable = self._failure(exc)
                self._record(
                    result,
                    AttemptRecord(
                        stage,
                        provider.name,
                        "error",
                        code,
                        duration_ms=duration,
                        ordinal=ordinal,
                        model=self._provider_model(provider),
                        issue_code=code,
                        candidate_generation=result.candidate_generation,
                    ),
                )
                if retryable and self._can_grace_retry(code, ordinal):
                    retry_stage = "translate-grace-retry"
                    continue
                return None
            if envelope.status is AgentStatus.REFUSED:
                if self.config.retry_refusals_once and not refusal_retried:
                    refusal_retried = True
                    retry_stage = "translate-refusal-retry"
                    continue
                return envelope
            if envelope.status is not AgentStatus.SUCCESS:
                return envelope
            integrity = validate_integrity(
                segment,
                envelope.text,
                self.glossary,
                extra_patterns=self.config.extra_protected_patterns,
                max_length_ratio=self.config.max_length_ratio,
            )
            if not integrity:
                return envelope
            self._record(
                result,
                AttemptRecord(
                    "integrity",
                    "deterministic-validator",
                    "rejected",
                    issues=[item.code for item in integrity],
                    ordinal=ordinal,
                    issue_code=FailureCode.INTEGRITY_INVALID.value,
                    candidate_generation=result.candidate_generation,
                ),
            )
            if self._can_grace_retry(FailureCode.INTEGRITY_INVALID.value, ordinal):
                retry_stage = "translate-grace-retry"
                continue
            return None

    def _judge_with_refusal_retry(
        self,
        result: TranslationResult,
        provider: ModelProvider,
        segment: Segment,
        candidate: str,
        anchors: list[dict[str, str]],
        *,
        confirmation: bool = False,
        stage_prefix: str = "review",
    ) -> ReviewVerdictEnvelope | None:
        ordinal = 0
        refusal_retried = False
        retry_stage = f"{stage_prefix}-grace-retry"
        while True:
            ordinal += 1
            started = time.monotonic()
            stage = (
                f"{stage_prefix}-confirm" if confirmation else stage_prefix
            ) if ordinal == 1 else retry_stage
            try:
                review = self._call_review_judge(
                    provider,
                    segment,
                    candidate,
                    anchors,
                    refusal_retry=ordinal > 1,
                )
                duration = int((time.monotonic() - started) * 1000)
                self._record(
                    result,
                    AttemptRecord(
                        stage,
                        provider.name,
                        {
                            ReviewVerdict.OK: ReviewDecision.APPROVED.value,
                            ReviewVerdict.REWRITE: "rewrite",
                            ReviewVerdict.REFUSED: ReviewDecision.REFUSED.value,
                            ReviewVerdict.ERROR: ReviewDecision.ERROR.value,
                        }[review.verdict],
                        "",
                        review.issues,
                        duration,
                        ordinal=ordinal,
                        model=self._provider_model(provider),
                        candidate_generation=result.candidate_generation,
                        usage=self._usage(review.raw),
                    ),
                )
            except Exception as exc:
                duration = int((time.monotonic() - started) * 1000)
                code, retryable = self._failure(exc)
                self._record(
                    result,
                    AttemptRecord(
                        stage,
                        provider.name,
                        "error",
                        code,
                        duration_ms=duration,
                        ordinal=ordinal,
                        model=self._provider_model(provider),
                        issue_code=code,
                        candidate_generation=result.candidate_generation,
                    ),
                )
                if retryable and self._can_grace_retry(code, ordinal):
                    retry_stage = f"{stage_prefix}-grace-retry"
                    continue
                return None
            if review.verdict is ReviewVerdict.REFUSED:
                if self.config.retry_refusals_once and not refusal_retried:
                    refusal_retried = True
                    retry_stage = f"{stage_prefix}-refusal-retry"
                    continue
                return review
            if review.verdict is ReviewVerdict.ERROR:
                return review
            return review

    def _rewrite_with_refusal_retry(
        self,
        result: TranslationResult,
        provider: ModelProvider,
        segment: Segment,
        candidate: str,
        issue_codes: list[str],
        anchors: list[dict[str, str]],
    ) -> AgentEnvelope | None:
        ordinal = 0
        refusal_retried = False
        retry_stage = "review-rewrite-grace-retry"
        while True:
            ordinal += 1
            started = time.monotonic()
            stage = "review-rewrite" if ordinal == 1 else retry_stage
            try:
                rewrite = self._call_review_rewrite(
                    provider,
                    segment,
                    candidate,
                    issue_codes,
                    anchors,
                    refusal_retry=ordinal > 1,
                )
                duration = int((time.monotonic() - started) * 1000)
                self._record(
                    result,
                    AttemptRecord(
                        stage,
                        provider.name,
                        rewrite.status.value,
                        rewrite.reason,
                        duration_ms=duration,
                        ordinal=ordinal,
                        model=self._provider_model(provider),
                        candidate_generation=result.candidate_generation,
                        usage=self._usage(rewrite.raw),
                    ),
                )
            except Exception as exc:
                duration = int((time.monotonic() - started) * 1000)
                code, retryable = self._failure(exc)
                self._record(
                    result,
                    AttemptRecord(
                        stage,
                        provider.name,
                        "error",
                        code,
                        duration_ms=duration,
                        ordinal=ordinal,
                        model=self._provider_model(provider),
                        issue_code=code,
                        candidate_generation=result.candidate_generation,
                    ),
                )
                if retryable and self._can_grace_retry(code, ordinal):
                    retry_stage = "review-rewrite-grace-retry"
                    continue
                return None
            if rewrite.status is AgentStatus.REFUSED:
                if self.config.retry_refusals_once and not refusal_retried:
                    refusal_retried = True
                    retry_stage = "review-rewrite-refusal-retry"
                    continue
                return rewrite
            if rewrite.status is not AgentStatus.SUCCESS:
                return rewrite
            integrity = validate_integrity(
                segment,
                rewrite.text,
                self.glossary,
                extra_patterns=self.config.extra_protected_patterns,
                max_length_ratio=self.config.max_length_ratio,
            )
            if not integrity:
                return rewrite
            self._record(
                result,
                AttemptRecord(
                    "integrity",
                    "deterministic-validator",
                    "rejected",
                    issues=[item.code for item in integrity],
                    ordinal=ordinal,
                    issue_code=FailureCode.INTEGRITY_INVALID.value,
                    candidate_generation=result.candidate_generation,
                ),
            )
            if self._can_grace_retry(FailureCode.INTEGRITY_INVALID.value, ordinal):
                retry_stage = "review-rewrite-grace-retry"
                continue
            return None

    def _review_with_refusal_retry(
        self,
        result: TranslationResult,
        provider: ModelProvider,
        segment: Segment,
        candidate: str,
        anchors: list[dict[str, str]],
    ) -> ReviewEnvelope | None:
        working = candidate
        revised = False
        for round_index in range(self.config.review_max_rounds):
            verdict = self._judge_with_refusal_retry(
                result,
                provider,
                segment,
                working,
                anchors,
                confirmation=round_index > 0,
            )
            if verdict is None:
                return ReviewEnvelope(ReviewDecision.ERROR, text=working)
            if verdict.verdict is ReviewVerdict.REFUSED:
                return ReviewEnvelope(ReviewDecision.REFUSED, text=working, raw=verdict.raw)
            if verdict.verdict is ReviewVerdict.ERROR:
                return ReviewEnvelope(ReviewDecision.ERROR, text=working, raw=verdict.raw)
            # A protocol-v1 reviewer may still return a complete corrected candidate.
            legacy_candidate_changed = False
            if verdict.text:
                integrity = validate_integrity(
                    segment,
                    verdict.text,
                    self.glossary,
                    extra_patterns=self.config.extra_protected_patterns,
                    max_length_ratio=self.config.max_length_ratio,
                )
                if integrity:
                    return ReviewEnvelope(ReviewDecision.ERROR, text=working)
                legacy_candidate_changed = verdict.text != working
                revised = revised or legacy_candidate_changed
                working = verdict.text
                self._set_candidate(result, working)
            if verdict.verdict is ReviewVerdict.OK:
                if legacy_candidate_changed:
                    if round_index + 1 >= self.config.review_max_rounds:
                        self._record(
                            result,
                            AttemptRecord(
                                "review-gate",
                                provider.name,
                                "rejected",
                                issue_code=FailureCode.MEANING_NOT_PRESERVED.value,
                                candidate_generation=result.candidate_generation,
                            ),
                        )
                        return ReviewEnvelope(ReviewDecision.ERROR, text=working)
                    continue
                return ReviewEnvelope(
                    ReviewDecision.REVISED if revised else ReviewDecision.APPROVED,
                    text=working,
                    meaning_preserved=True,
                    issues=verdict.issues,
                    raw=verdict.raw,
                )
            if round_index + 1 >= self.config.review_max_rounds:
                self._record(
                    result,
                    AttemptRecord(
                        "review-gate",
                        provider.name,
                        "rejected",
                        issue_code=FailureCode.MEANING_NOT_PRESERVED.value,
                        candidate_generation=result.candidate_generation,
                    ),
                )
                return ReviewEnvelope(ReviewDecision.ERROR, text=working)
            rewrite = None
            rewriters = self.config.rewriter_chain or [provider.name]
            for rewriter_name in rewriters:
                rewriter = self.registry.get(rewriter_name)
                rewrite = self._rewrite_with_refusal_retry(
                    result,
                    rewriter,
                    segment,
                    working,
                    verdict.issues,
                    anchors,
                )
                if rewrite is not None and rewrite.status is AgentStatus.SUCCESS:
                    break
            if rewrite is None or rewrite.status is not AgentStatus.SUCCESS:
                return ReviewEnvelope(ReviewDecision.ERROR, text=working)
            revised = revised or rewrite.text != working
            working = rewrite.text
            self._set_candidate(result, working)
        return ReviewEnvelope(ReviewDecision.ERROR, text=working)

    def _strong_review_reasons(self, segment: Segment) -> list[str]:
        return strong_review_selection_reasons(
            segment,
            sample_percent=self.config.strong_review_sample_percent,
            high_risk_categories=self.config.strong_review_high_risk_categories,
        )

    def _mark_triage_approved(
        self,
        result: TranslationResult,
        segment: Segment,
        candidate: str,
        triager_name: str,
    ) -> None:
        result.text = candidate
        result.status = "reviewed"
        result.reviewer = triager_name
        result.review_outcome = ReviewDecision.APPROVED.value
        result.acceptance_authority = "reviewer"
        result.metadata["review_tier"] = "triage"
        result.metadata["review_category"] = review_category(segment)

    def _triage_requires_strong_review(
        self,
        result: TranslationResult,
        segment: Segment,
        candidate: str,
        anchors: list[dict[str, str]],
    ) -> bool:
        if not self.config.review_triager_chain:
            return True
        for triager_name in self.config.review_triager_chain:
            outcome = self._triage_with_provider(
                result, triager_name, segment, candidate, anchors
            )
            if outcome == "unresolved":
                continue
            return outcome == "strong"
        result.metadata["review_tier"] = "strong_pending"
        result.metadata["review_category"] = review_category(segment)
        result.metadata["strong_review_reasons"] = ["triage_unresolved"]
        return True

    def _triage_with_provider(
        self,
        result: TranslationResult,
        triager_name: str,
        segment: Segment,
        candidate: str,
        anchors: list[dict[str, str]],
    ) -> str:
        verdict = self._judge_with_refusal_retry(
            result,
            self.registry.get(triager_name),
            segment,
            candidate,
            anchors,
            stage_prefix="review-triage",
        )
        if verdict is None or verdict.verdict in {
            ReviewVerdict.REFUSED,
            ReviewVerdict.ERROR,
        }:
            return "unresolved"
        reasons = self._strong_review_reasons(segment)
        if verdict.verdict is ReviewVerdict.REWRITE:
            reasons.insert(0, "triage_flagged")
        if verdict.text and verdict.text != candidate:
            reasons.insert(0, "legacy_triage_returned_candidate")
        if reasons:
            result.metadata["review_tier"] = "strong_pending"
            result.metadata["review_category"] = review_category(segment)
            result.metadata["strong_review_reasons"] = list(dict.fromkeys(reasons))
            return "strong"
        self._mark_triage_approved(result, segment, candidate, triager_name)
        return "approved"

    def _run_strong_review(
        self,
        result: TranslationResult,
        segment: Segment,
        candidate: str,
        anchors: list[dict[str, str]],
    ) -> str:
        for reviewer_name in self.config.reviewer_chain:
            review = self._review_with_refusal_retry(
                result,
                self.registry.get(reviewer_name),
                segment,
                candidate,
                anchors,
            )
            if review is not None and review.text:
                candidate = review.text
                self._set_candidate(result, candidate)
            if review is None or review.decision in {
                ReviewDecision.REFUSED,
                ReviewDecision.ERROR,
            }:
                continue
            result.text = candidate
            result.status = "reviewed"
            result.reviewer = reviewer_name
            result.review_outcome = review.decision.value
            result.acceptance_authority = "reviewer"
            result.metadata["review_tier"] = "strong"
            return candidate
        return candidate

    def translate_one(self, segment: Segment, *, force: bool = False) -> TranslationResult:
        anchors = self._anchors(segment.speaker)
        cache_key = self._cache_key(segment, anchors)
        if not force:
            cached = self.store.get(cache_key)
            if cached:
                self._remember_result(segment, cached)
                return cached

        result = TranslationResult(
            segment_id=segment.id,
            source=segment.source,
            metadata=segment.metadata.copy(),
        )
        candidate = ""
        for translator_name in self.config.translator_chain:
            translator = self.registry.get(translator_name)
            envelope = self._translate_with_refusal_retry(result, translator, segment, anchors)
            if envelope is None or envelope.status is not AgentStatus.SUCCESS:
                continue
            result.translator = translator_name
            candidate = envelope.text
            self._set_candidate(result, candidate)
            break

        if not candidate:
            result.errors.append("All translation routes were exhausted")
        elif not self.config.require_review:
            result.text = candidate
            result.status = "translated"
        else:
            needs_strong_review = self._triage_requires_strong_review(
                result, segment, candidate, anchors
            )
            if needs_strong_review:
                candidate = self._run_strong_review(
                    result, segment, candidate, anchors
                )
            if result.status != "reviewed":
                result.errors.append("All review routes were exhausted")
                self._accept_after_review_refusal(result, candidate)

        if result.status in {"translated", "reviewed", "accepted_unreviewed"}:
            self.store.put(cache_key, result)
            self._remember(segment, result.text)
            return result
        if self.config.preserve_source_on_failure or (
            candidate and self.config.review_refusal_policy == "source_preserve"
        ):
            result.text = segment.source
            result.status = "source_preserved"
            result.resolution_code = FailureCode.SOURCE_PRESERVED.value
            self.store.put(cache_key, result)
        self.audit.append(
            {"segment_id": result.segment_id, "stage": "complete", "status": result.status}
        )
        return result

    def review_existing_one(self, segment: Segment, candidate: str) -> TranslationResult:
        """Run a fresh semantic review over a reused candidate.

        This intentionally does not reuse translation-cache approval. Reuse proves only
        candidate identity; the independent reviewer must approve the current text under
        the current context pack.
        """

        if not self.config.require_review:
            raise ValueError("Existing candidates require an enabled independent review route")
        anchors = self._anchors(segment.speaker)
        result = TranslationResult(
            segment_id=segment.id,
            source=segment.source,
            translator="existing-candidate",
            metadata={**segment.metadata, "candidate_origin": "existing"},
        )
        self._set_candidate(result, candidate)
        integrity = validate_integrity(
            segment,
            candidate,
            self.glossary,
            extra_patterns=self.config.extra_protected_patterns,
            max_length_ratio=self.config.max_length_ratio,
        )
        if integrity:
            self._record(
                result,
                AttemptRecord(
                    "integrity",
                    "deterministic-validator",
                    "rejected",
                    issues=[item.code for item in integrity],
                    issue_code=FailureCode.INTEGRITY_INVALID.value,
                    candidate_generation=result.candidate_generation,
                ),
            )
            repaired = None
            for rewriter_name in self.config.rewriter_chain or self.config.reviewer_chain:
                repaired = self._rewrite_with_refusal_retry(
                    result,
                    self.registry.get(rewriter_name),
                    segment,
                    candidate,
                    ["formatting"],
                    anchors,
                )
                if repaired is not None and repaired.status is AgentStatus.SUCCESS:
                    candidate = repaired.text
                    self._set_candidate(result, candidate)
                    break
            if repaired is None or repaired.status is not AgentStatus.SUCCESS:
                result.errors.append("Existing candidate failed deterministic integrity repair")
                return self._preserve_or_fail_existing(segment, result)

        needs_strong_review = self._triage_requires_strong_review(
            result, segment, candidate, anchors
        )
        if needs_strong_review:
            candidate = self._run_strong_review(result, segment, candidate, anchors)
        if result.status == "reviewed":
            self._remember(segment, result.text)
            self.audit.append(
                {"segment_id": result.segment_id, "stage": "complete", "status": result.status}
            )
            return result

        result.errors.append("All review routes were exhausted for existing candidate")
        if self._accept_after_review_refusal(result, candidate):
            return result
        return self._preserve_or_fail_existing(segment, result)

    def _preserve_or_fail_existing(
        self, segment: Segment, result: TranslationResult
    ) -> TranslationResult:
        if self.config.preserve_source_on_failure or self.config.review_refusal_policy == "source_preserve":
            result.text = segment.source
            result.status = "source_preserved"
            result.resolution_code = FailureCode.SOURCE_PRESERVED.value
        self.audit.append(
            {"segment_id": result.segment_id, "stage": "complete", "status": result.status}
        )
        return result

    def review_existing_many(
        self,
        segments: Iterable[Segment],
        candidates: dict[str, dict[str, str]],
    ) -> list[TranslationResult]:
        items = list(segments)
        expected_ids = {item.id for item in items}
        candidate_ids = set(candidates)
        if expected_ids != candidate_ids:
            raise ValueError(
                "Full re-review requires exactly one existing candidate for every source target"
            )
        for segment in items:
            if candidates[segment.id]["source"] != segment.source:
                raise ValueError(
                    f"Existing candidate source does not match current inventory: {segment.id}"
                )
        # Ordered execution lets later targets reuse only freshly approved style anchors.
        return [
            self.review_existing_one(segment, candidates[segment.id]["text"])
            for segment in items
        ]

    def _accept_after_review_refusal(
        self, result: TranslationResult, candidate: str
    ) -> bool:
        refused = any(
            item.stage.startswith("review")
            and not item.stage.startswith("review-rewrite")
            and not item.stage.startswith("review-triage")
            and item.status == ReviewDecision.REFUSED.value
            for item in result.attempts
        )
        if not refused or self.config.review_refusal_policy != "accept_candidate_unreviewed":
            return False
        result.text = candidate
        result.status = "accepted_unreviewed"
        result.review_outcome = ReviewDecision.REFUSED.value
        result.acceptance_authority = "user_policy"
        result.resolution_code = FailureCode.USER_ACCEPTED_AFTER_REVIEW_REFUSAL.value
        return True

    def _run_deferred_stage(
        self, indexes: list[int], operation: Callable[[int], T]
    ) -> list[tuple[int, T]]:
        if self.config.max_workers <= 1 or len(indexes) <= 1:
            return [(index, operation(index)) for index in indexes]
        with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
            futures = {index: pool.submit(operation, index) for index in indexes}
            return [(index, futures[index].result()) for index in indexes]

    def _deferred_batch_key(self, items: list[Segment]) -> str:
        return stable_hash(
            {
                "prompt_version": PROMPT_VERSION,
                "project": asdict(self.project),
                "segments": [asdict(item) for item in items],
                "glossary": [asdict(item) for item in self.glossary],
                "characters": [asdict(self.characters[name]) for name in sorted(self.characters)],
                "context_pack": asdict(self.context_pack) if self.context_pack else None,
                "orchestrator": asdict(self.config),
                "providers": self.registry.fingerprints(
                    list(
                        dict.fromkeys(
                            self.config.translator_chain
                            + self.config.review_triager_chain
                            + self.config.reviewer_chain
                            + self.config.rewriter_chain
                        )
                    )
                ),
            }
        )

    @staticmethod
    def _result_from_state(payload: dict[str, Any]) -> TranslationResult:
        data = dict(payload)
        data["attempts"] = [AttemptRecord(**item) for item in data.get("attempts", [])]
        data["from_cache"] = False
        return TranslationResult(**data)

    def _save_deferred_state(
        self,
        path: Path,
        batch_key: str,
        results: list[TranslationResult | None],
        candidates: dict[int, str],
        completed_translators: set[str],
        completed_reviewers: set[str],
    ) -> None:
        payload = {
            "version": 2,
            "batch_key": batch_key,
            "completed_translators": [
                name for name in self.config.translator_chain if name in completed_translators
            ],
            "completed_reviewers": [
                name for name in self.config.reviewer_chain if name in completed_reviewers
            ],
            "candidates": {
                str(index): {
                    "text": text,
                    "translator": results[index].translator if results[index] else "",
                    "generation": results[index].candidate_generation if results[index] else 0,
                    "digest": results[index].candidate_digest if results[index] else "",
                }
                for index, text in candidates.items()
            },
            "results": {
                str(index): asdict(result)
                for index, result in enumerate(results)
                if result is not None and not result.from_cache
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        temporary.replace(path)

    def _translate_many_deferred(
        self, items: list[Segment], *, force: bool = False
    ) -> list[TranslationResult]:
        """Finish each provider stage before releasing queued items to its fallback.

        This makes refusal and quota fallback observable at batch level: all primary
        translations finish before the next translator runs, and all primary reviews
        finish before the next reviewer runs.
        """
        batch_key = self._deferred_batch_key(items)
        state_path = self.work_dir / ".game-l10n" / "deferred" / f"{batch_key}.json"
        if force:
            state_path.unlink(missing_ok=True)
        state: dict[str, Any] = {}
        if not force and state_path.exists():
            try:
                loaded = json.loads(state_path.read_text(encoding="utf-8"))
                if loaded.get("version") == 2 and loaded.get("batch_key") == batch_key:
                    state = loaded
            except (OSError, json.JSONDecodeError, AttributeError):
                state = {}

        completed_translators = set(state.get("completed_translators", []))
        completed_reviewers = set(state.get("completed_reviewers", []))
        saved_results = state.get("results", {}) if isinstance(state.get("results", {}), dict) else {}
        saved_candidates = (
            state.get("candidates", {}) if isinstance(state.get("candidates", {}), dict) else {}
        )

        results: list[TranslationResult | None] = [None] * len(items)
        anchors: dict[int, list[dict[str, str]]] = {
            index: self._anchors(segment.speaker) for index, segment in enumerate(items)
        }
        cache_keys: dict[int, str] = {
            index: stable_hash(
                {
                    "deferred_batch": batch_key,
                    "segment_index": index,
                    "segment_id": segment.id,
                }
            )
            for index, segment in enumerate(items)
        }
        candidates: dict[int, str] = {}
        pending_translation: list[int] = []

        for index, segment in enumerate(items):
            cached = self.store.get(cache_keys[index]) if not force else None
            if cached:
                results[index] = cached
                continue
            saved_result = saved_results.get(str(index))
            if isinstance(saved_result, dict):
                try:
                    partial = self._result_from_state(saved_result)
                    if partial.segment_id == segment.id and partial.source == segment.source:
                        results[index] = partial
                except (TypeError, ValueError):
                    results[index] = None
            if results[index] is None:
                results[index] = TranslationResult(
                    segment_id=segment.id,
                    source=segment.source,
                    metadata=segment.metadata.copy(),
                )
            saved_candidate = saved_candidates.get(str(index))
            if isinstance(saved_candidate, dict) and isinstance(saved_candidate.get("text"), str):
                text = saved_candidate["text"]
                digest = str(saved_candidate.get("digest", ""))
                if digest and stable_hash(text) == digest:
                    candidates[index] = text
                    results[index].translator = str(saved_candidate.get("translator", ""))
                    results[index].candidate_generation = int(saved_candidate.get("generation", 1))
                    results[index].candidate_digest = digest
                else:
                    completed_reviewers.clear()
                    pending_translation.append(index)
            else:
                pending_translation.append(index)

        for route_index, translator_name in enumerate(self.config.translator_chain):
            if not pending_translation:
                break
            if translator_name in completed_translators:
                continue
            translator = self.registry.get(translator_name)

            def translate_at(index: int) -> str | None:
                result = results[index]
                assert result is not None
                segment = items[index]
                envelope = self._translate_with_refusal_retry(
                    result, translator, segment, anchors[index]
                )
                if envelope is None or envelope.status is not AgentStatus.SUCCESS:
                    return None
                return envelope.text

            next_pending: list[int] = []
            for index, candidate in self._run_deferred_stage(pending_translation, translate_at):
                result = results[index]
                assert result is not None
                if candidate is None:
                    next_pending.append(index)
                    continue
                result.translator = translator_name
                self._set_candidate(result, candidate)
                candidates[index] = candidate
            pending_translation = next_pending
            completed_translators.add(translator_name)
            if pending_translation and route_index + 1 < len(self.config.translator_chain):
                next_provider = self.config.translator_chain[route_index + 1]
                for index in pending_translation:
                    result = results[index]
                    assert result is not None
                    self._record(
                        result,
                        AttemptRecord(
                            "translation-fallback-queued",
                            next_provider,
                            "queued",
                            "Previous translation route did not produce a valid result",
                        ),
                    )
            self._save_deferred_state(
                state_path,
                batch_key,
                results,
                candidates,
                completed_translators,
                completed_reviewers,
            )

        pending_review = sorted(
            index
            for index in candidates
            if results[index] is not None
            and results[index].status not in {"translated", "reviewed", "source_preserved"}
        )
        if not self.config.require_review:
            for index in pending_review:
                result = results[index]
                assert result is not None
                result.text = candidates[index]
                result.status = "translated"
                self.store.put(cache_keys[index], result)
                self._remember(items[index], result.text)
            pending_review = []
        else:
            if self.config.review_triager_chain:
                strong_pending: list[int] = []
                triage_pending: list[int] = []
                for index in pending_review:
                    result = results[index]
                    assert result is not None
                    if result.metadata.get("review_tier") == "strong_pending":
                        strong_pending.append(index)
                        continue
                    triage_pending.append(index)
                for triager_name in self.config.review_triager_chain:
                    if not triage_pending:
                        break

                    def triage_at(index: int) -> str:
                        result = results[index]
                        assert result is not None
                        segment = items[index]
                        review_anchors = (
                            self._anchors(segment.speaker)
                            if self.config.use_style_memory
                            else anchors[index]
                        )
                        return self._triage_with_provider(
                            result,
                            triager_name,
                            segment,
                            candidates[index],
                            review_anchors,
                        )

                    next_triage_pending: list[int] = []
                    if self.config.use_style_memory:
                        triaged_items = (
                            (index, triage_at(index)) for index in triage_pending
                        )
                    else:
                        triaged_items = iter(
                            self._run_deferred_stage(triage_pending, triage_at)
                        )
                    for index, outcome in triaged_items:
                        result = results[index]
                        assert result is not None
                        if outcome == "unresolved":
                            next_triage_pending.append(index)
                        elif outcome == "strong":
                            strong_pending.append(index)
                        else:
                            self.store.put(cache_keys[index], result)
                            self._remember(items[index], result.text)
                    triage_pending = next_triage_pending
                for index in triage_pending:
                    result = results[index]
                    assert result is not None
                    result.metadata["review_tier"] = "strong_pending"
                    result.metadata["review_category"] = review_category(items[index])
                    result.metadata["strong_review_reasons"] = ["triage_unresolved"]
                    strong_pending.append(index)
                pending_review = sorted(strong_pending)
                self._save_deferred_state(
                    state_path,
                    batch_key,
                    results,
                    candidates,
                    completed_translators,
                    completed_reviewers,
                )
            for route_index, reviewer_name in enumerate(self.config.reviewer_chain):
                if not pending_review:
                    break
                if reviewer_name in completed_reviewers:
                    continue
                reviewer = self.registry.get(reviewer_name)

                def review_at(index: int) -> tuple[str, str] | None:
                    result = results[index]
                    assert result is not None
                    segment = items[index]
                    for prior_index in range(index):
                        prior_result = results[prior_index]
                        if prior_result is not None:
                            self._remember_result(items[prior_index], prior_result)
                    review_anchors = (
                        self._anchors(segment.speaker)
                        if self.config.use_style_memory
                        else anchors[index]
                    )
                    review = self._review_with_refusal_retry(
                        result, reviewer, segment, candidates[index], review_anchors
                    )
                    if review is not None and review.text:
                        candidates[index] = review.text
                        self._set_candidate(result, review.text)
                    if review is None or review.decision in {
                        ReviewDecision.REFUSED,
                        ReviewDecision.ERROR,
                    }:
                        return None
                    return review.text, review.decision.value

                next_pending = []
                if self.config.use_style_memory:
                    reviewed_items = (
                        (index, review_at(index)) for index in pending_review
                    )
                else:
                    reviewed_items = iter(
                        self._run_deferred_stage(pending_review, review_at)
                    )
                for index, reviewed in reviewed_items:
                    result = results[index]
                    assert result is not None
                    if reviewed is None:
                        next_pending.append(index)
                        continue
                    reviewed_text, review_outcome = reviewed
                    self._set_candidate(result, reviewed_text)
                    candidates[index] = reviewed_text
                    result.text = reviewed_text
                    result.status = "reviewed"
                    result.reviewer = reviewer_name
                    result.review_outcome = review_outcome
                    result.acceptance_authority = "reviewer"
                    result.metadata["review_tier"] = "strong"
                    self.store.put(cache_keys[index], result)
                    self._remember(items[index], result.text)
                pending_review = next_pending
                completed_reviewers.add(reviewer_name)
                if pending_review and route_index + 1 < len(self.config.reviewer_chain):
                    next_provider = self.config.reviewer_chain[route_index + 1]
                    for index in pending_review:
                        result = results[index]
                        assert result is not None
                        self._record(
                            result,
                            AttemptRecord(
                                "review-fallback-queued",
                                next_provider,
                                "queued",
                                "Previous review route did not produce a valid decision",
                            ),
                        )
                self._save_deferred_state(
                    state_path,
                    batch_key,
                    results,
                    candidates,
                    completed_translators,
                    completed_reviewers,
                )

        failed_translation = set(pending_translation)
        failed_review = set(pending_review)
        for index in sorted(failed_translation | failed_review):
            result = results[index]
            assert result is not None
            if index in failed_translation:
                result.errors.append("All translation routes were exhausted")
            else:
                result.errors.append("All review routes were exhausted")
            accepted_unreviewed = (
                index in failed_review
                and index in candidates
                and self._accept_after_review_refusal(result, candidates[index])
            )
            if not accepted_unreviewed and (
                self.config.preserve_source_on_failure
                or (
                    index in failed_review
                    and self.config.review_refusal_policy == "source_preserve"
                )
            ):
                result.text = items[index].source
                result.status = "source_preserved"
                result.resolution_code = FailureCode.SOURCE_PRESERVED.value
            self.audit.append(
                {
                    "segment_id": result.segment_id,
                    "stage": "complete",
                    "status": result.status,
                }
            )
            self.store.put(cache_keys[index], result)

        state_path.unlink(missing_ok=True)
        return [result for result in results if result is not None]

    def translate_many(self, segments: Iterable[Segment], *, force: bool = False) -> list[TranslationResult]:
        items = list(segments)
        if self.config.defer_fallbacks_until_primary_complete:
            return self._translate_many_deferred(items, force=force)
        # Style anchors are causal: later lines should see previously approved lines in script order.
        if self.config.max_workers <= 1 or self.config.use_style_memory:
            return [self.translate_one(item, force=force) for item in items]
        with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
            return list(pool.map(lambda item: self.translate_one(item, force=force), items))
