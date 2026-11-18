from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from game_l10n.errors import ProviderFailure
from game_l10n.models import ProjectContext, Segment
from game_l10n.orchestrator import LocalizationOrchestrator, OrchestratorConfig
from game_l10n.providers import ProviderRegistry, ScriptedProvider


class ProviderFailureTests(unittest.TestCase):
    def test_invalid_retry_delays_cannot_escape_failure_boundary(self) -> None:
        for delay in (None, float("inf"), float("-inf"), float("nan"), True, False, "invalid", 10**400):
            with self.subTest(delay=delay):
                failure = ProviderFailure("rate_limited", retryable=True, retry_after_seconds=delay)
                self.assertIsNone(failure.retry_after_seconds)
                self.assertEqual(failure.code, "rate_limited")
                self.assertTrue(failure.retryable)
        for delay, expected in ((-3, 0.0), (0, 0.0), (2.5, 2.5), ("3", 3.0)):
            with self.subTest(delay=delay):
                failure = ProviderFailure("rate_limited", retryable=True, retry_after_seconds=delay)
                self.assertEqual(failure.retry_after_seconds, expected)

    def test_invalid_schema_preserves_real_usage_but_not_boolean_counts(self) -> None:
        parse_translation = LocalizationOrchestrator._translation_envelope
        parse_review = LocalizationOrchestrator._review_envelope
        parse_verdict = LocalizationOrchestrator._review_verdict_envelope
        cases = (
            (parse_translation, {"status": "unknown"}),
            (parse_translation, {"status": "success", "text": []}),
            (parse_review, {"decision": "unknown"}),
            (parse_review, {"decision": "approved", "issues": "invalid"}),
            (parse_review, {"decision": "approved", "text": []}),
            (parse_verdict, {"verdict": "unknown"}),
            (parse_verdict, {"verdict": "ok", "issues": "invalid"}),
            (parse_verdict, {"verdict": "rewrite", "issues": ["unknown"]}),
            (parse_verdict, {"verdict": "rewrite", "issues": []}),
            (parse_verdict, {"verdict": "ok", "issues": ["meaning"]}),
        )
        for parse, payload in cases:
            with self.subTest(parse=parse.__name__, payload=payload):
                payload = {**payload, "_usage": {"input_tokens": 7, "output_tokens": True, "total_tokens": -1}}
                with self.assertRaises(ProviderFailure) as raised:
                    parse(payload)
                self.assertEqual(raised.exception.code, "malformed_response")
                self.assertEqual(raised.exception.usage, {"input_tokens": 7})

    def test_fallback_attempt_ledger_keeps_usage_for_schema_rejection(self) -> None:
        primary = ScriptedProvider("primary", lambda **_: {"status": "invalid", "_usage": {"input_tokens": 7}})
        fallback = ScriptedProvider("fallback", lambda **_: {"status": "success", "text": "\ub300\uc0c1", "_usage": {"output_tokens": True}})
        for deferred in (False, True):
            with self.subTest(deferred=deferred), tempfile.TemporaryDirectory() as directory:
                orchestrator = LocalizationOrchestrator(
                    registry=ProviderRegistry({"primary": primary, "fallback": fallback}),
                    config=OrchestratorConfig(
                        ["primary", "fallback"], [], require_review=False,
                        translator_retries={"primary": 0, "fallback": 0},
                        defer_fallbacks_until_primary_complete=deferred,
                    ),
                    project=ProjectContext("SYNTHETIC", "en", "ko"),
                    work_dir=Path(directory),
                )
                segment = Segment("fixture", "SOURCE")
                result = orchestrator.translate_many([segment])[0] if deferred else orchestrator.translate_one(segment)
                self.assertEqual(result.text, "\ub300\uc0c1")
                self.assertEqual(result.status, "translated")
                self.assertEqual(result.attempts[0].issue_code, "malformed_response")
                self.assertEqual(result.attempts[0].usage, {"input_tokens": 7})
                self.assertEqual(result.attempts[1].usage, {})


    def test_confirmed_operational_failures_advance_once_with_original_usage(self) -> None:
        for code in (
            "provider_error", "timeout", "rate_limited",
            "authentication_error", "quota_exhausted",
        ):
            for deferred in (False, True):
                with self.subTest(code=code, deferred=deferred), tempfile.TemporaryDirectory() as directory:
                    def fail(**_):
                        raise ProviderFailure(code, retryable=True, usage={"input_tokens": 9})

                    first = ScriptedProvider("first", fail)
                    successor = ScriptedProvider(
                        "successor", lambda **_: {"status": "success", "text": "TARGET"},
                    )
                    orchestrator = LocalizationOrchestrator(
                        registry=ProviderRegistry({p.name: p for p in (first, successor)}),
                        config=OrchestratorConfig(
                            ["first", "successor"], [], require_review=False,
                            translator_retries={"first": 5}, grace_retries=3,
                            defer_fallbacks_until_primary_complete=deferred,
                        ),
                        project=ProjectContext("SYNTHETIC", "src", "dst"), work_dir=Path(directory),
                    )
                    result = orchestrator.translate_many([Segment("one", "SOURCE")])[0]
                    self.assertEqual((first.call_count, successor.call_count), (1, 1))
                    self.assertEqual(result.status, "translated")
                    attempt = result.attempts[0]
                    self.assertEqual(attempt.issue_code, code)
                    self.assertEqual(attempt.usage, {"input_tokens": 9})
                    self.assertIs(attempt.operational, True)
                    self.assertTrue(attempt.provider_fingerprint)

    def test_malformed_semantic_response_keeps_configured_retry_count(self) -> None:
        def malformed(**_):
            raise ProviderFailure("malformed_response", retryable=True)

        with tempfile.TemporaryDirectory() as directory:
            first = ScriptedProvider("first", malformed)
            successor = ScriptedProvider(
                "successor", lambda **_: {"status": "success", "text": "TARGET"},
            )
            result = LocalizationOrchestrator(
                registry=ProviderRegistry({p.name: p for p in (first, successor)}),
                config=OrchestratorConfig(
                    ["first", "successor"], [], require_review=False,
                    translator_retries={"first": 3},
                ),
                project=ProjectContext("SYNTHETIC", "src", "dst"), work_dir=Path(directory),
            ).translate_one(Segment("one", "SOURCE"))
            self.assertEqual(first.call_count, 4)
            self.assertEqual(successor.call_count, 1)
            self.assertEqual([a.ordinal for a in result.attempts[:4]], [1, 2, 3, 4])
            self.assertTrue(all(a.operational is False for a in result.attempts[:4]))


if __name__ == "__main__":
    unittest.main()
