from __future__ import annotations

import time
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import ProviderError
from .models import (
    AgentEnvelope,
    AgentStatus,
    AttemptRecord,
    CharacterProfile,
    ContextPack,
    GlossaryEntry,
    ProjectContext,
    ReviewDecision,
    ReviewEnvelope,
    Segment,
    TranslationResult,
)
from .prompts import PROMPT_VERSION, REVIEW_SCHEMA, TRANSLATION_SCHEMA, review_prompt, translation_prompt
from .providers import ModelProvider, ProviderRegistry
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
                    self.config.translator_chain + self.config.reviewer_chain
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

    def _record(self, result: TranslationResult, record: AttemptRecord) -> None:
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
            }
        )

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

    def _call_review(
        self,
        provider: ModelProvider,
        segment: Segment,
        candidate: str,
        anchors: list[dict[str, str]],
        *,
        refusal_retry: bool = False,
    ) -> ReviewEnvelope:
        system, user = review_prompt(
            self.project,
            segment,
            candidate,
            self.glossary,
            self._character(segment.speaker),
            self.context_pack,
            anchors,
            refusal_retry,
        )
        return self._review_envelope(
            provider.generate_json(
                system=system,
                user=user,
                schema=REVIEW_SCHEMA,
                schema_name="game_translation_review",
            )
        )

    def _translate_with_refusal_retry(
        self,
        result: TranslationResult,
        provider: ModelProvider,
        segment: Segment,
        anchors: list[dict[str, str]],
    ) -> AgentEnvelope | None:
        tries = 2 if self.config.retry_refusals_once else 1
        for retry_index in range(tries):
            started = time.monotonic()
            stage = "translate" if retry_index == 0 else "translate-refusal-retry"
            try:
                envelope = self._call_translation(
                    provider, segment, anchors, refusal_retry=retry_index > 0
                )
                duration = int((time.monotonic() - started) * 1000)
                self._record(
                    result,
                    AttemptRecord(stage, provider.name, envelope.status.value, envelope.reason, duration_ms=duration),
                )
            except Exception as exc:
                duration = int((time.monotonic() - started) * 1000)
                self._record(result, AttemptRecord(stage, provider.name, "error", str(exc), duration_ms=duration))
                return None
            if envelope.status is AgentStatus.REFUSED and retry_index + 1 < tries:
                continue
            return envelope
        return None

    def _review_with_refusal_retry(
        self,
        result: TranslationResult,
        provider: ModelProvider,
        segment: Segment,
        candidate: str,
        anchors: list[dict[str, str]],
    ) -> ReviewEnvelope | None:
        tries = 2 if self.config.retry_refusals_once else 1
        for retry_index in range(tries):
            started = time.monotonic()
            stage = "review" if retry_index == 0 else "review-refusal-retry"
            try:
                review = self._call_review(
                    provider,
                    segment,
                    candidate,
                    anchors,
                    refusal_retry=retry_index > 0,
                )
                duration = int((time.monotonic() - started) * 1000)
                self._record(
                    result,
                    AttemptRecord(
                        stage,
                        provider.name,
                        review.decision.value,
                        review.reason,
                        review.issues,
                        duration,
                    ),
                )
            except Exception as exc:
                duration = int((time.monotonic() - started) * 1000)
                self._record(result, AttemptRecord(stage, provider.name, "error", str(exc), duration_ms=duration))
                return None
            if review.decision is ReviewDecision.REFUSED and retry_index + 1 < tries:
                continue
            return review
        return None

    def translate_one(self, segment: Segment, *, force: bool = False) -> TranslationResult:
        anchors = self._anchors(segment.speaker)
        cache_key = self._cache_key(segment, anchors)
        if not force:
            cached = self.store.get(cache_key)
            if cached:
                self._remember(segment, cached.text)
                return cached

        result = TranslationResult(
            segment_id=segment.id,
            source=segment.source,
            metadata=segment.metadata.copy(),
        )
        for translator_name in self.config.translator_chain:
            translator = self.registry.get(translator_name)
            envelope = self._translate_with_refusal_retry(result, translator, segment, anchors)
            if envelope is None or envelope.status is not AgentStatus.SUCCESS:
                continue

            integrity = validate_integrity(
                segment,
                envelope.text,
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
                        issues=[f"{item.code}: {item.message}" for item in integrity],
                    ),
                )
                continue

            result.translator = translator_name
            if not self.config.require_review:
                result.text = envelope.text
                result.status = "translated"
                self.store.put(cache_key, result)
                self._remember(segment, result.text)
                return result

            for reviewer_name in self.config.reviewer_chain:
                reviewer = self.registry.get(reviewer_name)
                review = self._review_with_refusal_retry(
                    result, reviewer, segment, envelope.text, anchors
                )
                if review is None or review.decision in {ReviewDecision.REFUSED, ReviewDecision.ERROR}:
                    continue
                if not review.meaning_preserved or not review.text.strip():
                    self._record(
                        result,
                        AttemptRecord(
                            "review-gate",
                            reviewer_name,
                            "rejected",
                            "Reviewer did not attest that meaning was preserved",
                        ),
                    )
                    continue
                reviewed_text = review.text
                integrity = validate_integrity(
                    segment,
                    reviewed_text,
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
                            issues=[f"{item.code}: {item.message}" for item in integrity],
                        ),
                    )
                    continue
                result.text = reviewed_text
                result.status = "reviewed"
                result.reviewer = reviewer_name
                self.store.put(cache_key, result)
                self._remember(segment, result.text)
                return result

        result.errors.append("All translator/reviewer routes were exhausted")
        self.audit.append({"segment_id": result.segment_id, "stage": "complete", "status": "failed"})
        return result

    def translate_many(self, segments: Iterable[Segment], *, force: bool = False) -> list[TranslationResult]:
        items = list(segments)
        # Style anchors are causal: later lines should see previously approved lines in script order.
        if self.config.max_workers <= 1 or self.config.use_style_memory:
            return [self.translate_one(item, force=force) for item in items]
        with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
            return list(pool.map(lambda item: self.translate_one(item, force=force), items))
