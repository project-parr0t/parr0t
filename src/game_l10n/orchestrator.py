from __future__ import annotations

import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

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
    defer_fallbacks_until_primary_complete: bool = False
    preserve_source_on_failure: bool = False


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
        if result.status in {"translated", "reviewed"}:
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
        # The parent/orchestration plane receives categorical control metadata only.
        # Provider explanations and reviewer prose can echo private game dialogue, so
        # they must remain inside the short-lived provider call and never propagate.
        reason = record.reason.casefold()
        if record.status == "refused":
            record.reason = "provider_refusal"
        elif record.status == "error":
            if "quota" in reason or "insufficient" in reason:
                record.reason = "quota_exhausted"
            elif "rate" in reason and "limit" in reason:
                record.reason = "rate_limited"
            elif any(token in reason for token in ("auth", "api key", "unauthorized", "forbidden")):
                record.reason = "authentication_error"
            elif any(token in reason for token in ("timeout", "timed out")):
                record.reason = "timeout"
            elif any(token in reason for token in ("json", "schema", "format")):
                record.reason = "malformed_response"
            else:
                record.reason = "provider_error"
        elif record.status == "queued":
            record.reason = "fallback_queued"
        elif record.status == "rejected":
            record.reason = "validation_rejected"
        else:
            record.reason = ""
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
                self._remember_result(segment, cached)
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
                        issues=[item.code for item in integrity],
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
                            issues=[item.code for item in integrity],
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
                    self.config.translator_chain + self.config.reviewer_chain
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
            "version": 1,
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
                if loaded.get("version") == 1 and loaded.get("batch_key") == batch_key:
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
                candidates[index] = saved_candidate["text"]
                results[index].translator = str(saved_candidate.get("translator", ""))
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
                            issues=[item.code for item in integrity],
                        ),
                    )
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
            for route_index, reviewer_name in enumerate(self.config.reviewer_chain):
                if not pending_review:
                    break
                if reviewer_name in completed_reviewers:
                    continue
                reviewer = self.registry.get(reviewer_name)

                def review_at(index: int) -> str | None:
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
                    if review is None or review.decision in {
                        ReviewDecision.REFUSED,
                        ReviewDecision.ERROR,
                    }:
                        return None
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
                        return None
                    integrity = validate_integrity(
                        segment,
                        review.text,
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
                            ),
                        )
                        return None
                    return review.text

                next_pending = []
                if self.config.use_style_memory:
                    reviewed_items = (
                        (index, review_at(index)) for index in pending_review
                    )
                else:
                    reviewed_items = iter(
                        self._run_deferred_stage(pending_review, review_at)
                    )
                for index, reviewed_text in reviewed_items:
                    result = results[index]
                    assert result is not None
                    if reviewed_text is None:
                        next_pending.append(index)
                        continue
                    result.text = reviewed_text
                    result.status = "reviewed"
                    result.reviewer = reviewer_name
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
            if self.config.preserve_source_on_failure:
                result.text = items[index].source
                result.status = "source_preserved"
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
