from __future__ import annotations

import json
import random
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from .errors import ProviderError, ProviderFailure
from .framework_session import require_localization_mode
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
    retry_feedback,
    review_judge_prompt,
    review_rewrite_prompt,
    translation_prompt,
)
from .providers import ModelProvider, ProviderRegistry
from .review_selection import (
    DEFAULT_STRONG_REVIEW_CATEGORIES,
    DEFAULT_STRONG_REVIEW_SAMPLE_PERCENT,
    dialogue_review_reasons,
    provenance_review_reasons,
    review_category,
    rewrite_issue_codes,
    strong_review_selection_reasons,
    target_language_review_reasons,
)
from .store import AuditLog, ResultStore, stable_hash
from .review_policy import (
    REVIEW_SETTING_FIELDS, review_call_limit,
    review_worker_limit, validate_review_limits, validate_review_settings,
)
from .validators import preflight_constraint_issues, validate_integrity


@dataclass(slots=True)
class OrchestratorConfig:
    translator_chain: list[str]
    reviewer_chain: list[str]
    translator_retries: dict[str, int] = field(default_factory=dict)
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
            FailureCode.OUTPUT_TRUNCATED.value,
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
    review_attempt_limits: dict[str, int] = field(default_factory=dict)
    triage_max_calls: int | None = None
    strong_max_calls: int | None = None
    review_max_workers: int | None = None
    triage_max_workers: int | None = None
    strong_max_workers: int | None = None
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
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        metadata_only: bool = False,
    ):
        if not (config.translator_chain or config.reviewer_chain or config.rewriter_chain):
            raise ValueError("At least one execution role chain must be configured")
        if config.require_review and not config.reviewer_chain:
            raise ValueError("reviewer_chain must not be empty when require_review is true")
        if not isinstance(config.max_workers, int) or isinstance(config.max_workers, bool) or config.max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        if config.grace_retries < 0:
            raise ValueError("grace_retries must be zero or greater")
        unknown_attempt_routes = set(config.translator_retries) - set(
            config.translator_chain
        )
        if unknown_attempt_routes:
            raise ValueError(
                "translator_retries contains routes outside translator_chain"
            )
        if any(
            not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or attempts < 0
            for attempts in config.translator_retries.values()
        ):
            raise ValueError("translator_retries values must be non-negative integers")
        validate_review_limits(config.review_attempt_limits)
        validate_review_settings({key: getattr(config, key) for key in REVIEW_SETTING_FIELDS})
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
        self._metadata_only = metadata_only
        if not metadata_only:
            self._require_execution()
        self.store = None if metadata_only else ResultStore(self.work_dir / ".game-l10n" / "cache.sqlite3")
        self.audit = None if metadata_only else AuditLog(self.work_dir / ".game-l10n" / "audit.jsonl")
        self._style_memory: dict[str, list[dict[str, Any]]] = {}
        self._memory_lock = threading.Lock()
        self._progress_callback = progress_callback
        self._progress_lock = threading.Lock()
        self._progress_total = 0
        self._progress_attempts = 0
        self._provider_cooldown_lock = threading.Lock()
        self._provider_cooldowns: dict[str, float] = {}
        semantic_config = asdict(config)
        semantic_config.pop("max_workers", None)
        semantic_config.pop("review_attempt_limits", None)
        for key in REVIEW_SETTING_FIELDS:
            semantic_config.pop(key, None)
        self._semantic_config = semantic_config
        self._provider_fingerprints = self.registry.fingerprints(
            list(
                dict.fromkeys(
                    self.config.translator_chain
                    + self.config.review_triager_chain
                    + self.config.reviewer_chain
                    + self.config.rewriter_chain
                )
            )
        )

    def _require_execution(self) -> None:
        if self._metadata_only:
            raise ValueError("E_METADATA_ONLY_EXECUTION_FORBIDDEN")
        require_localization_mode(self.work_dir)

    def _emit_progress(self, stage: str, **statistics: int) -> None:
        if self._progress_callback is None:
            return
        try:
            self._progress_callback({"stage": stage, **statistics})
        except (TypeError, ValueError, OSError):
            # Progress reporting is observational and must never corrupt work.
            return

    def _progress_attempt(self, stage: str) -> None:
        with self._progress_lock:
            self._progress_attempts += 1
            attempts = self._progress_attempts
        self._emit_progress(stage, attempts=attempts)

    def _character(self, speaker: str) -> CharacterProfile | None:
        if speaker in self.characters:
            return self.characters[speaker]
        folded = speaker.casefold()
        for character in self.characters.values():
            if folded in {alias.casefold() for alias in character.aliases}:
                return character
        return None

    def _anchors(self, segment: Segment) -> list[dict[str, Any]]:
        if not self.config.use_style_memory or not segment.speaker:
            return []
        current = (
            self.context_pack.dialogue_contexts.get(segment.id, {})
            if self.context_pack
            else {}
        )
        current_group = current.get("consistency_group_id")
        current_addressees = set(current.get("addressee_ids", []))
        current_relationship = current.get("relationship_state")
        with self._memory_lock:
            entries = list(self._style_memory.get(segment.speaker, []))
        ranked = sorted(
            enumerate(entries),
            key=lambda pair: (
                pair[1].get("dialogue_context", {}).get("consistency_group_id")
                == current_group
                and bool(current_group),
                bool(
                    current_addressees
                    & set(pair[1].get("dialogue_context", {}).get("addressee_ids", []))
                ),
                pair[1].get("dialogue_context", {}).get("relationship_state")
                == current_relationship
                and bool(current_relationship),
                pair[0],
            ),
            reverse=True,
        )
        return [item for _, item in ranked[: self.config.style_anchor_limit]]

    def _remember(self, segment: Segment, text: str) -> None:
        if not self.config.use_style_memory or not segment.speaker or not text:
            return
        item: dict[str, Any] = {
            "target_id": segment.id,
            "source": segment.source,
            "approved_translation": text,
            "scene": segment.scene,
        }
        if self.context_pack:
            context = self.context_pack.dialogue_contexts.get(segment.id)
            if context:
                item["dialogue_context"] = context
        with self._memory_lock:
            entries = self._style_memory.setdefault(segment.speaker, [])
            if item not in entries:
                entries.append(item)

    def _remember_result(self, segment: Segment, result: TranslationResult) -> None:
        if result.status == "reviewed":
            self._remember(segment, result.text)

    def _wait_for_provider(self, provider_name: str) -> None:
        while True:
            with self._provider_cooldown_lock:
                deadline = self._provider_cooldowns.get(provider_name, 0.0)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(remaining)

    def _schedule_provider_retry(
        self,
        provider_name: str,
        code: str,
        ordinal: int,
        exc: Exception,
    ) -> None:
        status_code = exc.status_code if isinstance(exc, ProviderFailure) else None
        if code != FailureCode.RATE_LIMITED.value and not (
            isinstance(status_code, int) and 500 <= status_code < 600
        ):
            return
        retry_after = (
            exc.retry_after_seconds if isinstance(exc, ProviderFailure) else None
        )
        delay = retry_after
        if delay is None:
            delay = min(30.0, float(2 ** min(max(ordinal - 1, 0), 5)))
            delay += random.uniform(0.0, 0.25)
        deadline = time.monotonic() + delay
        with self._provider_cooldown_lock:
            self._provider_cooldowns[provider_name] = max(
                self._provider_cooldowns.get(provider_name, 0.0), deadline
            )

    def _translation_preflight(
        self, result: TranslationResult, segment: Segment
    ) -> bool:
        issues = preflight_constraint_issues(
            segment, extra_patterns=self.config.extra_protected_patterns
        )
        if not issues:
            return True
        self._record(
            result,
            AttemptRecord(
                "preflight",
                "deterministic-validator",
                "rejected",
                issues=[item.code for item in issues],
                issue_code=FailureCode.CONSTRAINT_UNSATISFIABLE.value,
            ),
        )
        result.errors.append("Translation constraints are unsatisfiable")
        return False

    def _accept_translation_candidate(
        self, result: TranslationResult, segment: Segment, candidate: str
    ) -> bool:
        integrity = validate_integrity(
            segment,
            candidate,
            self.glossary,
            extra_patterns=self.config.extra_protected_patterns,
            max_length_ratio=self.config.max_length_ratio,
        )
        language_risk = target_language_review_reasons(
            candidate, self.project.target_language, segment.metadata
        )
        if integrity or language_risk:
            if language_risk:
                result.metadata["target_language_risk"] = language_risk
            result.errors.append("Candidate failed the deterministic acceptance gate")
            return False
        result.text = candidate
        result.status = "translated"
        return True

    def _cached_result_is_usable(
        self, segment: Segment, result: TranslationResult
    ) -> bool:
        if result.segment_id != segment.id or result.source != segment.source:
            return False
        if result.status == "source_preserved":
            return result.text == segment.source
        if result.status not in {"translated", "reviewed", "accepted_unreviewed"}:
            return False
        return not validate_integrity(
            segment,
            result.text,
            self.glossary,
            extra_patterns=self.config.extra_protected_patterns,
            max_length_ratio=self.config.max_length_ratio,
        ) and not target_language_review_reasons(
            result.text, self.project.target_language, segment.metadata
        )

    def _cache_key(self, segment: Segment, anchors: list[dict[str, Any]]) -> str:
        character = self._character(segment.speaker)
        system, user = translation_prompt(
            self.project,
            segment,
            self.glossary,
            character,
            self.context_pack,
            anchors,
        )
        return stable_hash(
            {
                "cache_schema": "parr0t.semantic-cache.v2",
                "prompt_version": PROMPT_VERSION,
                "focused_translation_prompt": {"system": system, "user": user},
                # Review may make a glossary entry relevant through the candidate,
                # so glossary identity remains job-wide even though context entries
                # are limited to what the prompt can actually consume.
                "glossary": [asdict(item) for item in self.glossary],
                "orchestrator": self._semantic_config,
                "providers": self._provider_fingerprints,
            }
        )

    def _legacy_cache_key(
        self, segment: Segment, anchors: list[dict[str, Any]]
    ) -> str:
        return stable_hash(
            {
                "prompt_version": PROMPT_VERSION,
                "project": asdict(self.project),
                "segment": asdict(segment),
                "glossary": [asdict(item) for item in self.glossary],
                "character": (
                    asdict(self._character(segment.speaker))
                    if self._character(segment.speaker)
                    else None
                ),
                "context_pack": asdict(self.context_pack) if self.context_pack else None,
                "style_anchors": anchors,
                "orchestrator": asdict(self.config),
                "providers": self._provider_fingerprints,
            }
        )

    @staticmethod
    def _translation_envelope(raw: dict[str, Any]) -> AgentEnvelope:
        try:
            status = AgentStatus(raw.get("status"))
        except (ValueError, TypeError) as exc:
            raise ProviderFailure("malformed_response", retryable=True, usage=LocalizationOrchestrator._usage(raw)) from exc
        text = raw.get("text", "")
        reason = raw.get("reason", "")
        if not isinstance(text, str) or not isinstance(reason, str):
            raise ProviderFailure("malformed_response", retryable=True, usage=LocalizationOrchestrator._usage(raw))
        return AgentEnvelope(status=status, text=text, reason=reason, raw=raw)

    @staticmethod
    def _review_envelope(raw: dict[str, Any]) -> ReviewEnvelope:
        try:
            decision = ReviewDecision(raw.get("decision"))
        except (ValueError, TypeError) as exc:
            raise ProviderFailure("malformed_response", retryable=True, usage=LocalizationOrchestrator._usage(raw)) from exc
        issues = raw.get("issues", [])
        text = raw.get("text", "")
        if not isinstance(issues, list) or not all(isinstance(item, str) for item in issues):
            raise ProviderFailure("malformed_response", retryable=True, usage=LocalizationOrchestrator._usage(raw))
        if not isinstance(text, str):
            raise ProviderFailure("malformed_response", retryable=True, usage=LocalizationOrchestrator._usage(raw))
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
            raise ProviderFailure("malformed_response", retryable=True, usage=LocalizationOrchestrator._usage(raw)) from exc
        issues = raw.get("issues", [])
        if not isinstance(issues, list) or not all(isinstance(item, str) for item in issues):
            raise ProviderFailure("malformed_response", retryable=True, usage=LocalizationOrchestrator._usage(raw))
        if len(issues) > 4 or any(item not in REVIEW_ISSUE_CODES for item in issues):
            raise ProviderFailure("malformed_response", retryable=True, usage=LocalizationOrchestrator._usage(raw))
        if verdict is ReviewVerdict.REWRITE and not issues:
            raise ProviderFailure("malformed_response", retryable=True, usage=LocalizationOrchestrator._usage(raw))
        if verdict is not ReviewVerdict.REWRITE and issues:
            raise ProviderFailure("malformed_response", retryable=True, usage=LocalizationOrchestrator._usage(raw))
        return ReviewVerdictEnvelope(verdict=verdict, issues=issues, raw=raw)

    @staticmethod
    def _usage(raw: dict[str, Any]) -> dict[str, int]:
        usage = raw.get("_usage", {})
        if not isinstance(usage, dict):
            return {}
        return {
            str(key): int(value)
            for key, value in usage.items()
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        }

    @staticmethod
    def _failure_usage(exc: Exception) -> dict[str, int]:
        if not isinstance(exc, ProviderFailure):
            return {}
        return dict(exc.usage)

    def _record(self, result: TranslationResult, record: AttemptRecord) -> None:
        if record.provider != "deterministic-validator":
            record.provider_fingerprint = stable_hash(
                self.registry.get(record.provider).fingerprint()
            )
            if record.status != "error" and record.operational is None:
                record.operational = False
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
        if record.issue_code == FailureCode.INPUT_CAPACITY_EXCEEDED.value:
            result.metadata["capacity_repair"] = {
                "provider": record.provider,
                "action": "compact_focused_context_or_configure_larger_input_capacity",
                "capacity_fields": ["max_input_chars", "task_max_input_chars"],
                "user_budget_exhausted": False,
            }
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
                "operational": record.operational,
                "provider_fingerprint": record.provider_fingerprint,
                "retryable": record.retryable,
            }
        )

    @staticmethod
    def _provider_model(provider: ModelProvider) -> str:
        return str(provider.fingerprint().get("model", ""))

    @staticmethod
    def _operational_failure(exc: Exception) -> bool | None:
        if not isinstance(exc, ProviderFailure):
            return None
        return exc.code in {
            "provider_error", "timeout", "rate_limited",
            "authentication_error", "quota_exhausted",
        }

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

    def _translation_attempt_limit(self, provider_name: str) -> int | None:
        """Return an exact configured route cap, or None for legacy retry policy."""

        retries = self.config.translator_retries.get(provider_name)
        return None if retries is None else retries + 1

    @staticmethod
    def _set_candidate(result: TranslationResult, text: str) -> None:
        result.text = text
        digest = stable_hash(text)
        if digest != result.candidate_digest:
            result.candidate_generation = max(1, result.candidate_generation + 1)
            result.candidate_digest = digest

    def _call_translation(
        self,
        provider: ModelProvider,
        segment: Segment,
        anchors: list[dict[str, Any]],
        *,
        refusal_retry: bool = False,
        retry_feedback: list[dict[str, str]] | None = None,
    ) -> AgentEnvelope:
        self._require_execution()
        system, user = translation_prompt(
            self.project,
            segment,
            self.glossary,
            self._character(segment.speaker),
            self.context_pack,
            anchors,
            refusal_retry,
            retry_feedback,
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
        anchors: list[dict[str, Any]],
        *,
        refusal_retry: bool = False,
    ) -> ReviewVerdictEnvelope:
        self._require_execution()
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
        anchors: list[dict[str, Any]],
        *,
        refusal_retry: bool = False,
        retry_feedback: list[dict[str, str]] | None = None,
    ) -> AgentEnvelope:
        self._require_execution()
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
            retry_feedback,
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
        anchors: list[dict[str, Any]],
    ) -> AgentEnvelope | None:
        previous = self._route_attempts(result, provider.name, "translate")
        ordinal = self._next_ordinal(result, provider.name, "translate") - 1
        semantic_calls = self._semantic_attempts_used(previous)
        refusal_retried = any(item.status == "refused" for item in previous)
        attempt_limit = self._translation_attempt_limit(provider.name)
        retry_stage = "translate-grace-retry"
        feedback: list[dict[str, str]] = []
        if previous and previous[-1].status == "refused":
            retry_stage = "translate-refusal-retry"
        elif previous and previous[-1].status == "success":
            rejection = next((
                item for item in reversed(result.attempts)
                if item.stage == "integrity" and item.ordinal == previous[-1].ordinal
                and item.candidate_generation == previous[-1].candidate_generation
            ), None)
            if rejection is not None:
                retry_stage = "translate-integrity-retry"
                feedback = [
                    {"code": code, "detail": "Previously recorded deterministic validation failure"}
                    for code in rejection.issues
                ]
        elif previous and previous[-1].retryable is None and previous[-1].operational is False:
            retry_stage = "translate-status-retry"
        allowed, _ = self._route_eligibility(result, provider.name, "translate")
        if not allowed:
            return None
        while True:
            self._require_execution()
            ordinal += 1
            self._wait_for_provider(provider.name)
            started = time.monotonic()
            stage = "translate" if ordinal == 1 else retry_stage
            try:
                # The refusal note belongs only to a retry after a refusal, not a timeout or bad output.
                envelope = self._call_translation(
                    provider,
                    segment,
                    anchors,
                    refusal_retry=retry_stage == "translate-refusal-retry" and ordinal > 1,
                    retry_feedback=feedback if retry_stage == "translate-integrity-retry" else None,
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
                        operational=False,
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
                        usage=self._failure_usage(exc),
                        operational=self._operational_failure(exc),
                        retryable=retryable,
                    ),
                )
                if self._operational_failure(exc) is True:
                    return None
                semantic_calls += 1
                if retryable and (
                    (attempt_limit is not None and semantic_calls < attempt_limit)
                    or (
                        attempt_limit is None
                        and self._can_grace_retry(code, semantic_calls)
                    )
                ):
                    self._schedule_provider_retry(
                        provider.name, code, ordinal, exc
                    )
                    retry_stage = "translate-grace-retry"
                    continue
                return None
            semantic_calls += 1
            if envelope.status is AgentStatus.REFUSED:
                if attempt_limit is not None and semantic_calls < attempt_limit:
                    retry_stage = "translate-refusal-retry"
                    continue
                if (
                    attempt_limit is None
                    and self.config.retry_refusals_once
                    and not refusal_retried
                ):
                    refusal_retried = True
                    retry_stage = "translate-refusal-retry"
                    continue
                return envelope
            if envelope.status is not AgentStatus.SUCCESS:
                if attempt_limit is not None and semantic_calls < attempt_limit:
                    retry_stage = "translate-status-retry"
                    continue
                return envelope
            integrity = validate_integrity(
                segment,
                envelope.text,
                self.glossary,
                extra_patterns=self.config.extra_protected_patterns,
                max_length_ratio=self.config.max_length_ratio,
            )
            language_risk = target_language_review_reasons(
                envelope.text, self.project.target_language, segment.metadata
            )
            if not integrity and not language_risk:
                return envelope
            if language_risk:
                result.metadata["target_language_risk"] = language_risk
            self._record(
                result,
                AttemptRecord(
                    "integrity",
                    "deterministic-validator",
                    "rejected",
                    issues=[item.code for item in integrity] + language_risk,
                    ordinal=ordinal,
                    issue_code=FailureCode.INTEGRITY_INVALID.value,
                    candidate_generation=result.candidate_generation,
                ),
            )
            if (
                attempt_limit is not None
                and semantic_calls < attempt_limit
            ) or (
                attempt_limit is None
                and self._can_grace_retry(
                    FailureCode.INTEGRITY_INVALID.value, semantic_calls
                )
            ):
                retry_stage = "translate-integrity-retry"
                feedback = retry_feedback(integrity) + [
                    {
                        "code": item,
                        "detail": "Candidate does not satisfy the locked target language",
                    }
                    for item in language_risk
                ]
                continue
            return None

    def _judge_with_refusal_retry(
        self,
        result: TranslationResult,
        provider: ModelProvider,
        segment: Segment,
        candidate: str,
        anchors: list[dict[str, Any]],
        *,
        confirmation: bool = False,
        stage_prefix: str = "review",
    ) -> ReviewVerdictEnvelope | None:
        limit = None
        if stage_prefix == "review":
            limit = review_call_limit(
                provider.name, self.config.reviewer_chain,
                self.config.review_attempt_limits, model=self._provider_model(provider),
                tier_limit=self.config.strong_max_calls,
            )
        elif stage_prefix == "review-triage":
            limit = self.config.triage_max_calls
        pending: list[AttemptRecord] = []
        ordinal_base = max((
            item.ordinal for item in self._route_attempts(result, provider.name, stage_prefix)
            if item.operational is not True and item.candidate_generation != result.candidate_generation
        ), default=0)
        for item in self._route_attempts(result, provider.name, stage_prefix):
            if item.candidate_generation != result.candidate_generation:
                continue
            if item.status in {"approved", "rewrite"}:
                pending.clear()
                ordinal_base = max(ordinal_base, item.ordinal)
            else:
                pending.append(item)
        ordinal = self._semantic_attempts_used(pending, ordinal_base=ordinal_base)
        refusal_retried = any(item.status == "refused" for item in pending)
        retry_stage = f"{stage_prefix}-grace-retry"
        if pending and pending[-1].status == "refused":
            retry_stage = f"{stage_prefix}-refusal-retry"
        allowed, _ = self._route_eligibility(result, provider.name, stage_prefix)
        if not allowed:
            return None
        while True:
            self._require_execution()
            if limit is not None and self._semantic_attempts_used(
                self._route_attempts(result, provider.name, stage_prefix)
            ) >= limit:
                return None
            ordinal += 1
            self._wait_for_provider(provider.name)
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
                    refusal_retry=ordinal > 1 and retry_stage == f"{stage_prefix}-refusal-retry",
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
                        ordinal=self._next_ordinal(result, provider.name, stage_prefix),
                        model=self._provider_model(provider),
                        candidate_generation=result.candidate_generation,
                        usage=self._usage(review.raw),
                        operational=False,
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
                        ordinal=self._next_ordinal(result, provider.name, stage_prefix),
                        model=self._provider_model(provider),
                        issue_code=code,
                        candidate_generation=result.candidate_generation,
                        usage=self._failure_usage(exc),
                        retryable=retryable,
                        operational=self._operational_failure(exc),
                    ),
                )
                if self._operational_failure(exc) is True:
                    return None
                if retryable and (limit is not None or self._can_grace_retry(code, ordinal)):
                    self._schedule_provider_retry(
                        provider.name, code, ordinal, exc
                    )
                    retry_stage = f"{stage_prefix}-grace-retry"
                    continue
                return None
            if review.verdict is ReviewVerdict.REFUSED:
                if limit is not None or (self.config.retry_refusals_once and not refusal_retried):
                    refusal_retried = True
                    retry_stage = f"{stage_prefix}-refusal-retry"
                    continue
                return review
            if review.verdict is ReviewVerdict.ERROR:
                if limit is not None and stage_prefix == "review":
                    retry_stage = f"{stage_prefix}-grace-retry"
                    continue
                return review
            if stage_prefix == "review" and review.verdict in {ReviewVerdict.OK, ReviewVerdict.REWRITE}:
                result.metadata["continuation_review"] = {
                    "action": "rewrite" if review.verdict is ReviewVerdict.REWRITE else "approved",
                    "provider": provider.name,
                    "model": self._provider_model(provider),
                    "provider_fingerprint": stable_hash(provider.fingerprint()),
                    "candidate_generation": result.candidate_generation,
                    "candidate_digest": result.candidate_digest,
                    "issues": list(review.issues),
                }
            return review

    def _rewrite_with_refusal_retry(
        self,
        result: TranslationResult,
        provider: ModelProvider,
        segment: Segment,
        candidate: str,
        issue_codes: list[str],
        anchors: list[dict[str, Any]],
    ) -> AgentEnvelope | None:
        previous = self._route_attempts(result, provider.name, "review-rewrite")
        ordinal = self._next_ordinal(result, provider.name, "review-rewrite") - 1
        ordinal_base = max((
            item.ordinal for item in previous
            if item.operational is not True and item.candidate_generation != result.candidate_generation
        ), default=0)
        retry_ordinal = self._semantic_attempts_used(
            [item for item in previous if item.candidate_generation == result.candidate_generation],
            ordinal_base=ordinal_base,
        )
        refusal_retried = any(
            item.status == "refused" and item.candidate_generation == result.candidate_generation
            for item in previous
        )
        retry_stage = "review-rewrite-grace-retry"
        feedback: list[dict[str, str]] = []
        current = [
            item for item in previous if item.candidate_generation == result.candidate_generation
        ]
        if current and current[-1].status == "refused":
            retry_stage = "review-rewrite-refusal-retry"
        elif current and current[-1].status == "success":
            rejection = next((
                item for item in reversed(result.attempts)
                if item.stage == "integrity" and item.ordinal == current[-1].ordinal
                and item.candidate_generation == current[-1].candidate_generation
            ), None)
            if rejection is not None:
                retry_stage = "review-rewrite-integrity-retry"
                feedback = [
                    {"code": code, "detail": "Previously recorded deterministic validation failure"}
                    for code in rejection.issues
                ]
        allowed, _ = self._route_eligibility(result, provider.name, "review-rewrite")
        if not allowed:
            return None
        while True:
            self._require_execution()
            ordinal += 1
            retry_ordinal += 1
            self._wait_for_provider(provider.name)
            started = time.monotonic()
            stage = "review-rewrite" if retry_ordinal == 1 else retry_stage
            try:
                rewrite = self._call_review_rewrite(
                    provider,
                    segment,
                    candidate,
                    issue_codes,
                    anchors,
                    # Each signal belongs only to the retry that directly follows its cause.
                    refusal_retry=ordinal > 1 and retry_stage == "review-rewrite-refusal-retry",
                    retry_feedback=(
                        feedback if ordinal > 1 and retry_stage == "review-rewrite-integrity-retry" else None
                    ),
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
                        operational=False,
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
                        usage=self._failure_usage(exc),
                        retryable=retryable,
                        operational=self._operational_failure(exc),
                    ),
                )
                if self._operational_failure(exc) is True:
                    return None
                if retryable and self._can_grace_retry(code, retry_ordinal):
                    self._schedule_provider_retry(
                        provider.name, code, ordinal, exc
                    )
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
            if self._can_grace_retry(FailureCode.INTEGRITY_INVALID.value, retry_ordinal):
                retry_stage = "review-rewrite-integrity-retry"
                feedback = retry_feedback(integrity)
                continue
            return None

    def _review_with_refusal_retry(
        self,
        result: TranslationResult,
        provider: ModelProvider,
        segment: Segment,
        candidate: str,
        anchors: list[dict[str, Any]],
    ) -> ReviewEnvelope | None:
        working = candidate
        revised = False
        limit = review_call_limit(
            provider.name, self.config.reviewer_chain, self.config.review_attempt_limits,
            model=self._provider_model(provider), tier_limit=self.config.strong_max_calls,
        ) if (provider.name in self.config.review_attempt_limits or
              (self.config.reviewer_chain and provider.name == self.config.reviewer_chain[0])) else None
        max_rounds = limit if limit is not None else self.config.review_max_rounds
        for round_index in range(max_rounds):
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
                    result.metadata.pop("continuation_review", None)
                    return ReviewEnvelope(ReviewDecision.ERROR, text=working)
                legacy_candidate_changed = verdict.text != working
                revised = revised or legacy_candidate_changed
                working = verdict.text
                self._set_candidate(result, working)
                if legacy_candidate_changed:
                    result.metadata["continuation_review"].update({
                        "action": "confirm" if verdict.verdict is ReviewVerdict.OK else "rewrite",
                        "candidate_generation": result.candidate_generation,
                        "candidate_digest": result.candidate_digest,
                    })
            language_risk = target_language_review_reasons(
                working, self.project.target_language, segment.metadata
            )
            if language_risk:
                result.metadata["target_language_risk"] = language_risk
                result.metadata["continuation_review"].update({
                    "action": "rewrite",
                    "issues": rewrite_issue_codes(verdict.issues, language_risk=True),
                })
            if verdict.verdict is ReviewVerdict.OK and not language_risk:
                if legacy_candidate_changed:
                    if round_index + 1 >= max_rounds:
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
            if round_index + 1 >= max_rounds or (
                limit is not None and len(self._route_attempts(result, provider.name, "review")) >= limit
            ):
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
            rewrite_issues = rewrite_issue_codes(
                verdict.issues, language_risk=bool(language_risk)
            )
            rewriters = self.config.rewriter_chain or [provider.name]
            for rewriter_name in rewriters:
                rewriter = self.registry.get(rewriter_name)
                rewrite = self._rewrite_with_refusal_retry(
                    result,
                    rewriter,
                    segment,
                    working,
                    rewrite_issues,
                    anchors,
                )
                if rewrite is not None and rewrite.status is AgentStatus.SUCCESS:
                    break
            if rewrite is None or rewrite.status is not AgentStatus.SUCCESS:
                return ReviewEnvelope(ReviewDecision.ERROR, text=working)
            revised = revised or rewrite.text != working
            working = rewrite.text
            self._set_candidate(result, working)
            result.metadata["continuation_review"] = {
                "action": "confirm",
                "provider": provider.name,
                "model": self._provider_model(provider),
                "provider_fingerprint": stable_hash(provider.fingerprint()),
                "candidate_generation": result.candidate_generation,
                "candidate_digest": result.candidate_digest,
                "issues": rewrite_issues,
            }
        return ReviewEnvelope(ReviewDecision.ERROR, text=working)

    def _strong_review_reasons(
        self, segment: Segment, candidate: str, translator: str = ""
    ) -> list[str]:
        reasons = strong_review_selection_reasons(
            segment,
            sample_percent=self.config.strong_review_sample_percent,
            high_risk_categories=self.config.strong_review_high_risk_categories,
        )
        primary = self.config.translator_chain[:1]
        reasons.extend(
            provenance_review_reasons(
                segment.id,
                self.context_pack.reference_cues if self.context_pack else (),
                non_primary_translation=bool(
                    translator and primary and translator not in {primary[0], "existing-candidate"}
                ),
            )
        )
        reasons.extend(
            dialogue_review_reasons(
                segment.id,
                self.context_pack.dialogue_contexts if self.context_pack else None,
            )
        )
        reasons.extend(
            target_language_review_reasons(
                candidate, self.project.target_language, segment.metadata
            )
        )
        return list(dict.fromkeys(reasons))

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
        result.metadata.pop("strong_review_reasons", None)
        result.metadata["review_category"] = review_category(segment)

    def _triage_requires_strong_review(
        self,
        result: TranslationResult,
        segment: Segment,
        candidate: str,
        anchors: list[dict[str, Any]],
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
        anchors: list[dict[str, Any]],
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
        reasons = self._strong_review_reasons(segment, candidate, result.translator)
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
        anchors: list[dict[str, Any]],
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
            language_risk = target_language_review_reasons(
                candidate, self.project.target_language, segment.metadata
            )
            if language_risk:
                result.metadata["target_language_risk"] = language_risk
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
        self._require_execution()
        if not self.config.translator_chain:
            raise ValueError("E_TRANSLATION_ROUTE_REQUIRED: configure a translation role before translating")
        anchors = self._anchors(segment)
        cache_key = self._cache_key(segment, anchors)
        pending_result = None
        if not force:
            cached = self.store.get(cache_key)
            if (cached is not None and cached.status == "failed"
                    and cached.source == segment.source and cached.text
                    and cached.candidate_digest == stable_hash(cached.text)
                    and not validate_integrity(segment, cached.text, self.glossary,
                        extra_patterns=self.config.extra_protected_patterns,
                        max_length_ratio=self.config.max_length_ratio)):
                pending_result = cached
            if cached is not None and not self._cached_result_is_usable(
                segment, cached
            ):
                cached = None
            if cached is None:
                cached = self.store.get(self._legacy_cache_key(segment, anchors))
                if cached is not None and not self._cached_result_is_usable(
                    segment, cached
                ):
                    cached = None
                if cached is not None:
                    self.store.put(cache_key, cached)
            if cached:
                self._remember_result(segment, cached)
                return cached

        result = pending_result or TranslationResult(
            segment_id=segment.id,
            source=segment.source,
            metadata=segment.metadata.copy(),
        )
        candidate: str | None = pending_result.text if pending_result else None
        if pending_result is None and self._translation_preflight(result, segment):
            for translator_name in self.config.translator_chain:
                translator = self.registry.get(translator_name)
                envelope = self._translate_with_refusal_retry(
                    result, translator, segment, anchors
                )
                if envelope is None or envelope.status is not AgentStatus.SUCCESS:
                    continue
                result.translator = translator_name
                candidate = envelope.text
                self._set_candidate(result, candidate)
                break

        if candidate is None:
            if not result.errors:
                result.errors.append("All translation routes were exhausted")
        elif not self.config.require_review:
            self._accept_translation_candidate(result, segment, candidate)
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
                self._accept_after_review_refusal(result, segment, candidate)

        if result.status in {"translated", "reviewed", "accepted_unreviewed"}:
            self.store.put(cache_key, result)
            self._remember_result(segment, result)
            return result
        if self.config.preserve_source_on_failure or (
            candidate is not None
            and self.config.review_refusal_policy == "source_preserve"
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
        self._require_execution()

        if not self.config.require_review:
            raise ValueError("Existing candidates require an enabled independent review route")
        anchors = self._anchors(segment)
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
                    rewrite_issue_codes(integrity=integrity),
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
        if self._accept_after_review_refusal(result, segment, candidate):
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
        self._require_execution()
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
        results: list[TranslationResult] = []
        for segment in items:
            result = self.review_existing_one(segment, candidates[segment.id]["text"])
            results.append(result)
            completed = len(results)
            self._emit_progress(
                "review",
                completed_records=completed,
                review_completed=sum(item.status == "reviewed" for item in results),
                failed_records=sum(item.status == "failed" for item in results),
                source_preserved=sum(item.status == "source_preserved" for item in results),
                attempts=completed,
            )
        return results

    def _accept_after_review_refusal(
        self, result: TranslationResult, segment: Segment, candidate: str
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
        language_risk = target_language_review_reasons(
            candidate, self.project.target_language, segment.metadata
        )
        if language_risk:
            result.metadata["target_language_risk"] = language_risk
            return False
        result.text = candidate
        result.status = "accepted_unreviewed"
        result.review_outcome = ReviewDecision.REFUSED.value
        result.acceptance_authority = "user_policy"
        result.resolution_code = FailureCode.USER_ACCEPTED_AFTER_REVIEW_REFUSAL.value
        return True

    def _run_deferred_stage(
        self, indexes: list[int], operation: Callable[[int], T], *, stage: str,
        review_tier: str = "strong",
    ) -> Iterable[tuple[int, T]]:
        def observed(index: int) -> T:
            value = operation(index)
            self._progress_attempt(stage)
            return value

        workers = self.config.max_workers
        if stage.startswith("review"):
            workers = review_worker_limit({key: getattr(self.config, key) for key in REVIEW_SETTING_FIELDS}, workers, tier=review_tier)
        if workers <= 1 or len(indexes) <= 1:
            for index in indexes:
                yield index, observed(index)
            return
        workers = min(workers, len(indexes))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # Submit one bounded cohort at a time instead of allocating a future
            # for every target in a potentially very large inventory.
            for offset in range(0, len(indexes), workers):
                cohort = indexes[offset:offset + workers]
                futures = {pool.submit(observed, index): index for index in cohort}
                for future in as_completed(futures):
                    yield futures[future], future.result()

    def _legacy_deferred_batch_key(self, items: list[Segment]) -> str:
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
    def _deferred_batch_key(cache_keys: list[str]) -> str:
        return stable_hash(
            {
                "schema": "parr0t.deferred-batch-cache.v3",
                "ordered_segment_cache_keys": cache_keys,
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
        self._progress_total = len(items)
        self._progress_attempts = 0
        anchors: dict[int, list[dict[str, Any]]] = {
            index: self._anchors(segment) for index, segment in enumerate(items)
        }
        cache_keys: dict[int, str] = {
            index: self._cache_key(segment, anchors[index])
            for index, segment in enumerate(items)
        }
        batch_key = self._deferred_batch_key(
            [cache_keys[index] for index in range(len(items))]
        )
        legacy_batch_key = self._legacy_deferred_batch_key(items)
        state_path = self.work_dir / ".game-l10n" / "deferred" / f"{batch_key}.json"
        if force:
            state_path.unlink(missing_ok=True)
            self.store.clear_staged_candidates(batch_key)
        state: dict[str, Any] = {}
        if not force:
            state_candidates = [(state_path, batch_key)]
            legacy_state_path = (
                self.work_dir
                / ".game-l10n"
                / "deferred"
                / f"{legacy_batch_key}.json"
            )
            if legacy_state_path != state_path:
                state_candidates.append((legacy_state_path, legacy_batch_key))
            for candidate_path, expected_key in state_candidates:
                if not candidate_path.exists():
                    continue
                try:
                    loaded = json.loads(candidate_path.read_text(encoding="utf-8"))
                    if (
                        loaded.get("version") == 2
                        and loaded.get("batch_key") == expected_key
                    ):
                        state = loaded
                        break
                except (OSError, json.JSONDecodeError, AttributeError):
                    continue

        completed_translators = set(state.get("completed_translators", []))
        completed_reviewers = set(state.get("completed_reviewers", []))
        saved_results = state.get("results", {}) if isinstance(state.get("results", {}), dict) else {}
        saved_candidates = (
            state.get("candidates", {}) if isinstance(state.get("candidates", {}), dict) else {}
        )
        staged_candidates = self.store.get_staged_candidates(batch_key)

        results: list[TranslationResult | None] = [None] * len(items)
        candidates: dict[int, str] = {}
        pending_translation: list[int] = []
        preflight_failures: set[int] = set()

        for index, segment in enumerate(items):
            cached = self.store.get(cache_keys[index]) if not force else None
            if cached is not None and not self._cached_result_is_usable(
                segment, cached
            ):
                cached = None
            if cached is None and not force:
                legacy_cache_key = stable_hash(
                    {
                        "deferred_batch": legacy_batch_key,
                        "segment_index": index,
                        "segment_id": segment.id,
                    }
                )
                cached = self.store.get(legacy_cache_key)
                if cached is not None and not self._cached_result_is_usable(
                    segment, cached
                ):
                    cached = None
                if cached is not None:
                    self.store.put(cache_keys[index], cached)
            if cached:
                results[index] = cached
                self._remember_result(segment, cached)
                continue
            saved_result = saved_results.get(str(index))
            if isinstance(saved_result, dict):
                try:
                    partial = self._result_from_state(saved_result)
                    if partial.segment_id == segment.id and partial.source == segment.source:
                        results[index] = partial
                        self._remember_result(segment, partial)
                except (TypeError, ValueError):
                    results[index] = None
            if results[index] is None:
                results[index] = TranslationResult(
                    segment_id=segment.id,
                    source=segment.source,
                    metadata=segment.metadata.copy(),
                )
            staged_candidate = staged_candidates.get(index)
            if staged_candidate is not None:
                staged_result, staged_text = staged_candidate
                if (
                    staged_result.segment_id == segment.id
                    and staged_result.source == segment.source
                    and staged_result.candidate_digest == stable_hash(staged_text)
                ):
                    results[index] = staged_result
                    candidates[index] = staged_text
                    continue
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
                assert results[index] is not None
                if self._translation_preflight(results[index], segment):
                    pending_translation.append(index)
                else:
                    preflight_failures.add(index)

        cached_count = sum(item is not None and item.from_cache for item in results)
        self._emit_progress(
            "translation",
            cached_records=cached_count,
            translation_completed=len(candidates) + cached_count,
            fallback_pending=len(pending_translation),
            attempts=0,
        )

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
            stage = "translation" if route_index == 0 else "translation_fallback"
            for index, candidate in self._run_deferred_stage(
                pending_translation, translate_at, stage=stage
            ):
                result = results[index]
                assert result is not None
                if candidate is None:
                    next_pending.append(index)
                    continue
                result.translator = translator_name
                self._set_candidate(result, candidate)
                candidates[index] = candidate
                self.store.put_staged_candidate(
                    batch_key, index, result, candidate
                )
            pending_translation = sorted(next_pending)
            self._emit_progress(
                stage,
                translation_completed=len(candidates) + cached_count,
                fallback_pending=len(pending_translation),
                attempts=self._progress_attempts,
            )
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
            acceptance_failures: set[int] = set()
            for index in pending_review:
                result = results[index]
                assert result is not None
                if self._accept_translation_candidate(
                    result, items[index], candidates[index]
                ):
                    self.store.put(cache_keys[index], result)
                    self._remember_result(items[index], result)
                else:
                    acceptance_failures.add(index)
            pending_review = []
        else:
            acceptance_failures = set()
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
                            self._anchors(segment)
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
                            self._run_deferred_stage(
                                triage_pending, triage_at, stage="review", review_tier="triage"
                            )
                        )
                    for index, outcome in triaged_items:
                        if self.config.use_style_memory:
                            self._progress_attempt("review")
                        result = results[index]
                        assert result is not None
                        if outcome == "unresolved":
                            next_triage_pending.append(index)
                        elif outcome == "strong":
                            strong_pending.append(index)
                        else:
                            self.store.put(cache_keys[index], result)
                            self._remember_result(items[index], result)
                    triage_pending = sorted(next_triage_pending)
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
                    review_anchors = (
                        self._anchors(segment)
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
                        self._run_deferred_stage(
                            pending_review,
                            review_at,
                            stage=("review" if route_index == 0 else "review_fallback"),
                        )
                    )
                for index, reviewed in reviewed_items:
                    if self.config.use_style_memory:
                        self._progress_attempt(
                            "review" if route_index == 0 else "review_fallback"
                        )
                    result = results[index]
                    assert result is not None
                    if reviewed is None:
                        next_pending.append(index)
                        continue
                    reviewed_text, review_outcome = reviewed
                    self._set_candidate(result, reviewed_text)
                    candidates[index] = reviewed_text
                    language_risk = target_language_review_reasons(
                        reviewed_text,
                        self.project.target_language,
                        items[index].metadata,
                    )
                    if language_risk:
                        result.metadata["target_language_risk"] = language_risk
                        next_pending.append(index)
                        continue
                    result.text = reviewed_text
                    result.status = "reviewed"
                    result.reviewer = reviewer_name
                    result.review_outcome = review_outcome
                    result.acceptance_authority = "reviewer"
                    result.metadata["review_tier"] = "strong"
                    self.store.put(cache_keys[index], result)
                    self._remember_result(items[index], result)
                pending_review = sorted(next_pending)
                completed_review = sum(
                    result is not None and result.status == "reviewed"
                    for result in results
                )
                self._emit_progress(
                    "review" if route_index == 0 else "review_fallback",
                    review_completed=completed_review,
                    fallback_pending=len(pending_review),
                    attempts=self._progress_attempts,
                )
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

        failed_translation = (
            set(pending_translation) | preflight_failures | acceptance_failures
        )
        failed_review = set(pending_review)
        for index in sorted(failed_translation | failed_review):
            result = results[index]
            assert result is not None
            if index in failed_translation:
                if index not in preflight_failures:
                    result.errors.append("All translation routes were exhausted")
            else:
                result.errors.append("All review routes were exhausted")
            accepted_unreviewed = (
                index in failed_review
                and index in candidates
                and self._accept_after_review_refusal(
                    result, items[index], candidates[index]
                )
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
        self.store.clear_staged_candidates(batch_key)
        final_results = [result for result in results if result is not None]
        self._emit_progress(
            "finalizing",
            completed_records=len(final_results),
            translation_completed=len(candidates) + cached_count,
            review_completed=sum(item.status == "reviewed" for item in final_results),
            fallback_pending=0,
            failed_records=sum(item.status == "failed" for item in final_results),
            source_preserved=sum(
                item.status == "source_preserved" for item in final_results
            ),
            cached_records=cached_count,
            attempts=self._progress_attempts,
        )
        return final_results

    def translate_many(self, segments: Iterable[Segment], *, force: bool = False) -> list[TranslationResult]:
        self._require_execution()
        if not self.config.translator_chain:
            raise ValueError("E_TRANSLATION_ROUTE_REQUIRED: configure a translation role before translating")
        items = list(segments)
        if not items:
            return []
        if self.config.defer_fallbacks_until_primary_complete:
            return self._translate_many_deferred(items, force=force)
        self._progress_total = len(items)
        self._progress_attempts = 0

        completed = 0
        translated = 0
        reviewed = 0
        failed = 0
        source_preserved = 0
        cached = 0
        attempts = 0
        completed_lock = threading.Lock()

        def translate_observed(item: Segment) -> TranslationResult:
            nonlocal completed, translated, reviewed, failed
            nonlocal source_preserved, cached, attempts
            result = self.translate_one(item, force=force)
            with completed_lock:
                completed += 1
                translated += bool(result.translator)
                reviewed += result.status == "reviewed"
                failed += result.status == "failed"
                source_preserved += result.status == "source_preserved"
                cached += result.from_cache
                attempts += len(result.attempts)
                current = completed
                translated_count = translated
                reviewed_count = reviewed
                failed_count = failed
                preserved_count = source_preserved
                cached_count = cached
                attempt_count = attempts
            self._emit_progress(
                "review" if self.config.require_review else "translation",
                completed_records=current,
                translation_completed=translated_count,
                review_completed=reviewed_count,
                failed_records=failed_count,
                source_preserved=preserved_count,
                cached_records=cached_count,
                attempts=attempt_count,
            )
            return result
        # Style anchors are causal: later lines should see previously approved lines in script order.
        if self.config.max_workers <= 1 or self.config.use_style_memory:
            return [translate_observed(item) for item in items]
        workers = min(self.config.max_workers, len(items))
        results: list[TranslationResult] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for offset in range(0, len(items), workers):
                results.extend(pool.map(translate_observed, items[offset:offset + workers]))
        return results

    @staticmethod
    def _attempt_role(stage: str) -> str:
        for role in ("translate", "review-rewrite", "review-triage"):
            if stage == role or stage.startswith(f"{role}-"):
                return role
        if stage in {"review", "review-confirm", "review-grace-retry", "review-refusal-retry"}:
            return "review"
        return ""

    def _route_attempts(
        self, result: TranslationResult, provider_name: str, role: str,
    ) -> list[AttemptRecord]:
        model = self._provider_model(self.registry.get(provider_name))
        return [
            item for item in result.attempts
            if item.provider == provider_name and item.model == model
            and item.status != "queued" and self._attempt_role(item.stage) == role
        ]

    @staticmethod
    def _semantic_attempts_used(attempts: list[AttemptRecord], *, ordinal_base: int = 0) -> int:
        """Honor compact call counters without charging proven operational events."""
        count = 0
        maximum = ordinal_base
        operational_ordinals = set()
        for item in attempts:
            if item.operational is True:
                if item.ordinal > ordinal_base:
                    operational_ordinals.add(item.ordinal)
            else:
                count += 1
                maximum = max(maximum, item.ordinal)
        operational_count = sum(value < maximum for value in operational_ordinals)
        return max(count, maximum - ordinal_base - operational_count)

    def _next_ordinal(self, result: TranslationResult, provider_name: str, role: str) -> int:
        attempts = self._route_attempts(result, provider_name, role)
        return max(len(attempts), max((item.ordinal for item in attempts), default=0)) + 1

    def _route_eligibility(
        self, result: TranslationResult, provider_name: str, role: str,
        *, next_candidate: bool = False,
    ) -> tuple[bool, str]:
        """Use captured identity/evidence, never inferred provider availability."""
        provider = self.registry.get(provider_name)
        model = self._provider_model(provider)
        fingerprint = stable_hash(provider.fingerprint())
        history = [
            item for item in result.attempts
            if item.provider == provider_name and item.status != "queued"
            and self._attempt_role(item.stage)
        ]
        if any(not item.model and model and not item.provider_fingerprint for item in history):
            return False, "historical_route_binding_unknown"
        exact = [item for item in history if item.model == model]
        if any(
            item.operational is None or not item.provider_fingerprint
            or isinstance(item.ordinal, bool) or item.ordinal < 1
            for item in exact
        ):
            return False, "historical_failure_evidence_unknown"
        current_transport = [
            item for item in exact if item.provider_fingerprint == fingerprint
        ]
        if current_transport and current_transport[-1].operational is True:
            return False, "operational_route_unchanged"
        attempts = self._route_attempts(result, provider_name, role)
        semantic = [item for item in attempts if item.operational is False]
        semantic_maximum = max((item.ordinal for item in semantic), default=0)
        later_operational = {item.ordinal for item in attempts
                             if item.operational is True and item.ordinal > semantic_maximum}
        if later_operational and max(later_operational) - semantic_maximum != len(later_operational):
            return False, "historical_failure_evidence_unknown"
        if role == "translate":
            limit = self._translation_attempt_limit(provider_name)
        elif role == "review":
            limit = review_call_limit(
                provider_name, self.config.reviewer_chain,
                self.config.review_attempt_limits, model=model,
                tier_limit=self.config.strong_max_calls,
            )
        elif role == "review-triage":
            limit = self.config.triage_max_calls
        else:
            limit = None
        if limit is not None and self._semantic_attempts_used(attempts) >= limit:
            return False, "semantic_attempt_limit_consumed"
        ordinal_base = max((
            item.ordinal for item in semantic
            if role != "translate" and item.candidate_generation != result.candidate_generation
        ), default=0)
        current = [
            item for item in semantic
            if role == "translate" or item.candidate_generation == result.candidate_generation
        ]
        if next_candidate:
            current = []
        if role == "review":
            rounds = [item for item in semantic if item.status in {"approved", "rewrite"}]
            round_limit = (
                limit if limit is not None and (
                    provider_name in self.config.review_attempt_limits
                    or self.config.reviewer_chain[:1] == [provider_name]
                ) else self.config.review_max_rounds
            )
            if len(rounds) >= round_limit:
                return False, "semantic_review_rounds_consumed"
            state = result.metadata.get("continuation_review", {})
            confirming = (
                isinstance(state, dict) and state.get("action") == "confirm"
                and state.get("provider") == provider_name and state.get("model") == model
                and state.get("candidate_digest") == result.candidate_digest
                and state.get("candidate_generation") == result.candidate_generation
            )
            if current and current[-1].status in {"approved", "rewrite"}:
                if not confirming or current[-1].stage == "review-confirm":
                    return False, "candidate_judgment_consumed"
                return True, ""
            completed = max((
                index for index, item in enumerate(current)
                if item.status in {"approved", "rewrite"}
            ), default=-1)
            if completed >= 0:
                ordinal_base = max(ordinal_base, current[completed].ordinal)
            current = current[completed + 1:]
        if not current:
            return True, ""
        last = current[-1]
        if last.status == "error" and last.retryable is False:
            return False, "non_retryable_failure_requires_repair"
        if last.status == "error" and last.retryable is None and (
            limit is None or role in {"review-triage", "review-rewrite"}
        ):
            return False, "semantic_attempt_limit_consumed"
        if role != "translate" and last.status in {"approved", "rewrite"}:
            return False, "candidate_stage_consumed"
        if limit is not None:
            return True, ""
        code = last.issue_code
        # Integrity rejection is a separate deterministic ledger entry, not a new call.
        if last.status == "success":
            position = result.attempts.index(last)
            integrity = next((
                item for item in result.attempts[position + 1:]
                if item.stage == "integrity" and item.ordinal == last.ordinal
                and item.candidate_generation == last.candidate_generation
            ), None)
            if integrity is None:
                return False, "candidate_lineage_repair_required"
            code = integrity.issue_code
        if last.status == "refused":
            allowed = self.config.retry_refusals_once and sum(
                item.status == "refused" for item in current
            ) < 2
        else:
            current_calls = [
                item for item in attempts
                if item.ordinal > ordinal_base
                and (role == "translate" or item.candidate_generation == result.candidate_generation)
            ]
            allowed = self._can_grace_retry(code, self._semantic_attempts_used(current_calls, ordinal_base=ordinal_base))
        return (True, "") if allowed else (False, "semantic_attempt_limit_consumed")

    def _continuation_route(
        self, result: TranslationResult, chain: list[str], role: str,
        *, action: str | None = None,
    ) -> dict[str, Any]:
        issue = "configured_routes_consumed"
        for name in chain:
            eligible, code = self._route_eligibility(result, name, role)
            if not eligible:
                if code in {
                    "historical_route_binding_unknown", "historical_failure_evidence_unknown",
                    "operational_route_unchanged", "candidate_lineage_repair_required",
                }:
                    issue = code
                continue
            provider = self.registry.get(name)
            return {
                "eligible": True,
                "next_action": action or role,
                "route_binding": {
                    "provider": name,
                    "model": self._provider_model(provider),
                    "provider_fingerprint": stable_hash(provider.fingerprint()),
                    "ordinal": self._next_ordinal(result, name, role),
                    "candidate_generation": result.candidate_generation,
                    "candidate_digest": result.candidate_digest,
                },
                "issue_code": "",
            }
        return {
            "eligible": False, "next_action": "repair",
            "route_binding": None, "issue_code": issue,
        }

    def continuation_status(
        self, segment: Segment, prior_result: TranslationResult,
    ) -> dict[str, Any]:
        """Describe the next authorized configured stage without calls or writes.

        A changed captured provider fingerprint proves a configuration change; mere
        passage of time, retryable flags, or missing legacy evidence prove no repair.
        The caller retains authoritative job pause/stop and route-consent controls.
        """
        def status(action: str, issue: str = "", *, eligible: bool = False) -> dict[str, Any]:
            return {
                "eligible": eligible, "next_action": action,
                "route_binding": None, "issue_code": issue,
            }

        result = prior_result
        if result.segment_id != segment.id or result.source != segment.source:
            return status("repair", "source_binding_mismatch")
        if result.status in {"translated", "reviewed", "accepted_unreviewed"}:
            return status("complete")
        if preflight_constraint_issues(segment, extra_patterns=self.config.extra_protected_patterns):
            return status("repair", "source_constraints_require_repair")
        if not result.candidate_digest:
            if result.candidate_generation or any(
                item.status == "success"
                and self._attempt_role(item.stage) in {"translate", "review-rewrite"}
                and not any(
                    rejection.stage == "integrity" and rejection.ordinal == item.ordinal
                    and rejection.candidate_generation == item.candidate_generation
                    for rejection in result.attempts[index + 1:]
                )
                for index, item in enumerate(result.attempts)
            ):
                return status("repair", "candidate_lineage_repair_required")
            if not self.config.translator_chain:
                return status("repair", "translation_route_required")
            return self._continuation_route(result, self.config.translator_chain, "translate")
        if (
            result.candidate_generation < 1
            or stable_hash(result.text) != result.candidate_digest
        ):
            return status("repair", "candidate_lineage_repair_required")
        integrity = validate_integrity(
            segment, result.text, self.glossary,
            extra_patterns=self.config.extra_protected_patterns,
            max_length_ratio=self.config.max_length_ratio,
        )
        state = result.metadata.get("continuation_review", {})
        state_bound = (
            isinstance(state, dict)
            and state.get("candidate_generation") == result.candidate_generation
            and state.get("candidate_digest") == result.candidate_digest
            and state.get("action") in {"rewrite", "confirm", "approved"}
            and any(
                item.provider == state.get("provider")
                and item.model == state.get("model")
                and item.provider_fingerprint == state.get("provider_fingerprint")
                and item.operational is False and item.status in {"approved", "rewrite"}
                and self._attempt_role(item.stage) == "review"
                and item.candidate_generation <= result.candidate_generation
                for item in result.attempts
            )
        )
        if state and not state_bound:
            return status("repair", "review_lineage_repair_required")
        if integrity:
            return self._continuation_route(
                result, self.config.rewriter_chain or self.config.reviewer_chain,
                "review-rewrite",
            )
        if not self.config.require_review:
            if target_language_review_reasons(result.text, self.project.target_language, segment.metadata):
                return status("repair", "target_language_repair_required")
            return status("accept", eligible=True)
        if state_bound and state.get("action") == "approved":
            name = state.get("provider")
            if name in self.config.reviewer_chain:
                provider = self.registry.get(name)
                if (
                    state.get("model") == self._provider_model(provider)
                    and state.get("provider_fingerprint") == stable_hash(provider.fingerprint())
                    and not target_language_review_reasons(
                        result.text, self.project.target_language, segment.metadata,
                    )
                ):
                    return status("accept-review", eligible=True)
        if state_bound and state.get("action") == "rewrite":
            issues = state.get("issues")
            if not isinstance(issues, list) or not issues or any(
                item not in REVIEW_ISSUE_CODES for item in issues
            ):
                return status("repair", "review_issue_evidence_unknown")
            name = state.get("provider")
            if name in self.config.reviewer_chain:
                reviewer = self.registry.get(name)
                can_confirm, _ = self._route_eligibility(
                    result, name, "review", next_candidate=True,
                )
                if state.get("model") == self._provider_model(reviewer) and can_confirm:
                    rewrite = self._continuation_route(
                        result, self.config.rewriter_chain or [name], "review-rewrite",
                    )
                    if rewrite["eligible"]:
                        return rewrite
        if state_bound and state.get("action") == "confirm":
            name = state.get("provider")
            if name in self.config.reviewer_chain:
                provider = self.registry.get(name)
                if state.get("model") == self._provider_model(provider):
                    confirmation = self._continuation_route(
                        result, [name], "review", action="review-confirm",
                    )
                    if confirmation["eligible"]:
                        return confirmation
        if not state_bound and (
            result.metadata.get("review_tier") != "strong_pending"
            or result.metadata.get("strong_review_reasons") == ["triage_unresolved"]
        ):
            triage = self._continuation_route(
                result, self.config.review_triager_chain, "review-triage",
            )
            if triage["eligible"]:
                return triage
        return self._continuation_route(result, self.config.reviewer_chain, "review")

    def continue_many(
        self,
        segments: Iterable[Segment],
        prior_results: dict[str, TranslationResult] | Iterable[TranslationResult],
    ) -> list[TranslationResult]:
        """Continue unfinished targets without replaying locked peers or old routes."""
        self._require_execution()
        items = list(segments)
        if len({item.id for item in items}) != len(items):
            raise ValueError("Continuation requires unique source target IDs")
        if isinstance(prior_results, dict):
            previous = prior_results
        else:
            values = list(prior_results)
            previous = {item.segment_id: item for item in values}
            if len(previous) != len(values):
                raise ValueError("Continuation requires unique prior result IDs")
        if any(key != value.segment_id for key, value in previous.items()):
            raise ValueError("Continuation prior result keys must match segment IDs")
        results: list[TranslationResult] = []
        for segment in items:
            prior = previous.get(segment.id)
            if prior is not None and prior.source != segment.source:
                raise ValueError(f"Continuation source does not match inventory: {segment.id}")
            if prior is not None and prior.status in {"translated", "reviewed", "accepted_unreviewed"}:
                results.append(prior)
                self._remember_result(segment, prior)
                continue
            result = self._result_from_state(prior.to_dict()) if prior is not None else TranslationResult(
                segment.id, segment.source, metadata=segment.metadata.copy(),
            )
            anchors = self._anchors(segment)
            while True:
                continuation = self.continuation_status(segment, result)
                if not continuation["eligible"]:
                    result.metadata["continuation"] = continuation
                    break
                action = continuation["next_action"]
                binding = continuation["route_binding"]
                if action == "accept":
                    self._accept_translation_candidate(result, segment, result.text)
                    break
                if action == "accept-review":
                    state = result.metadata["continuation_review"]
                    result.status = "reviewed"
                    result.reviewer = state["provider"]
                    result.review_outcome = ReviewDecision.APPROVED.value
                    result.acceptance_authority = "reviewer"
                    result.metadata["review_tier"] = "strong"
                    break
                self._require_execution()
                provider = self.registry.get(binding["provider"])
                before = len(result.attempts)
                if action == "translate":
                    envelope = self._translate_with_refusal_retry(result, provider, segment, anchors)
                    if envelope is not None and envelope.status is AgentStatus.SUCCESS:
                        result.translator = provider.name
                        self._set_candidate(result, envelope.text)
                elif action == "review-triage":
                    self._triage_with_provider(result, provider.name, segment, result.text, anchors)
                elif action == "review-rewrite":
                    state = result.metadata.get("continuation_review", {})
                    integrity = validate_integrity(
                        segment, result.text, self.glossary,
                        extra_patterns=self.config.extra_protected_patterns,
                        max_length_ratio=self.config.max_length_ratio,
                    )
                    issues = rewrite_issue_codes(
                        state.get("issues", []), integrity=integrity,
                    )
                    envelope = self._rewrite_with_refusal_retry(
                        result, provider, segment, result.text, issues, anchors,
                    )
                    if envelope is not None and envelope.status is AgentStatus.SUCCESS:
                        self._set_candidate(result, envelope.text)
                        if state.get("provider"):
                            result.metadata["continuation_review"] = {
                                **state, "action": "confirm",
                                "candidate_generation": result.candidate_generation,
                                "candidate_digest": result.candidate_digest,
                            }
                else:
                    candidate = result.text
                    verdict = self._judge_with_refusal_retry(
                        result, provider, segment, candidate, anchors,
                        confirmation=action == "review-confirm",
                    )
                    if verdict is not None and verdict.verdict in {ReviewVerdict.OK, ReviewVerdict.REWRITE}:
                        if verdict.text and verdict.text != candidate:
                            if validate_integrity(
                                segment, verdict.text, self.glossary,
                                extra_patterns=self.config.extra_protected_patterns,
                                max_length_ratio=self.config.max_length_ratio,
                            ):
                                result.metadata.pop("continuation_review", None)
                            else:
                                self._set_candidate(result, verdict.text)
                                result.metadata["continuation_review"].update({
                                    "action": "confirm",
                                    "candidate_generation": result.candidate_generation,
                                    "candidate_digest": result.candidate_digest,
                                })
                        elif verdict.verdict is ReviewVerdict.OK and target_language_review_reasons(
                            result.text, self.project.target_language, segment.metadata,
                        ):
                            result.metadata["continuation_review"]["action"] = "rewrite"
                            result.metadata["continuation_review"]["issues"] = rewrite_issue_codes(
                                verdict.issues, language_risk=True,
                            )
                if result.status in {"translated", "reviewed", "accepted_unreviewed"}:
                    break
                if len(result.attempts) == before:
                    result.metadata["continuation"] = {
                        "eligible": False, "next_action": "repair",
                        "route_binding": None, "issue_code": "stage_did_not_advance",
                    }
                    break
            if result.status in {"translated", "reviewed", "accepted_unreviewed"}:
                result.metadata.pop("continuation", None)
                self.store.put(self._cache_key(segment, anchors), result)
                self._remember_result(segment, result)
            results.append(result)
        return results
