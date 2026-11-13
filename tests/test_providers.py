from __future__ import annotations

import io
import http.client
import json
import os
import subprocess
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from game_l10n.errors import ConfigurationError, ProviderFailure
from game_l10n.providers import CommandProvider, CompatibleChatProvider, OpenAIResponsesProvider, ProviderRegistry, _post_json
from game_l10n.provider_policy import CONTROL_OBSERVATION_POLICY


class ProviderTimingTests(unittest.TestCase):
    def test_provider_defaults_and_config_do_not_invent_timeouts(self) -> None:
        entries = [
            {"type": "openai_responses", "name": "responses", "model": "fixture"},
            {"type": "compatible_chat", "name": "chat", "model": "fixture", "base_url": "https://fixture.invalid"},
            {"type": "command", "name": "command", "command": ["fixture-command"]},
        ]
        from pathlib import Path
        for timeout in (None, 0, 300.5):
            with self.subTest(timeout=timeout):
                config = {"provider_preset": "custom", "control_plane": CONTROL_OBSERVATION_POLICY,
                          "providers": [{**item, "timeout": timeout} for item in entries]}
                registry = ProviderRegistry.from_config(config, Path("."))
                for item in entries:
                    self.assertEqual(registry.get(item["name"]).timeout, timeout or None)
        registry = ProviderRegistry.from_config({"provider_preset": "custom", "control_plane": CONTROL_OBSERVATION_POLICY,
                                                 "providers": entries}, Path("."))
        for item in entries:
            self.assertIsNone(registry.get(item["name"]).timeout)
        for timeout in (-1, float("inf"), True):
            with self.subTest(invalid=timeout), self.assertRaises(ConfigurationError):
                CommandProvider("fixture", ["fixture-command"], timeout=timeout)

    def test_http_timeout_is_disabled_unless_explicitly_configured(self) -> None:
        for timeout in (None, 0, 300):
            with self.subTest(timeout=timeout), patch(
                "game_l10n.providers.urllib.request.urlopen", return_value=io.BytesIO(b'{"ok":true}')
            ) as request:
                self.assertEqual(_post_json("https://fixture.invalid", {}, {}, timeout), {"ok": True})
                self.assertEqual(request.call_args.kwargs["timeout"], timeout or None)

    def test_command_deadline_is_opt_in_and_timeout_stays_categorical(self) -> None:
        completed = subprocess.CompletedProcess(["fixture-command"], 0, stdout="{}", stderr="")
        for timeout in (None, 0, 300):
            with self.subTest(timeout=timeout), patch(
                "game_l10n.providers.subprocess.run", return_value=completed
            ) as run:
                provider = CommandProvider("fixture", ["fixture-command"], timeout=timeout)
                self.assertEqual(provider.generate_json(system="fixture", user="fixture", schema={}, schema_name="fixture"), {})
                self.assertEqual(run.call_args.kwargs["timeout"], timeout or None)
        with patch("game_l10n.providers.subprocess.run", side_effect=subprocess.TimeoutExpired(["fixture-command"], 3)):
            with self.assertRaises(ProviderFailure) as raised:
                CommandProvider("fixture", ["fixture-command"], timeout=3).generate_json(
                    system="fixture", user="fixture", schema={}, schema_name="fixture",
                )
        self.assertEqual(raised.exception.code, "timeout")
        self.assertTrue(raised.exception.retryable)


class CompatibleProviderTests(unittest.TestCase):
    def test_output_truncation_preserves_provider_usage(self) -> None:
        def fake_post(_url, _headers, _payload, _timeout):
            return {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": ""},
                    }
                ],
                "usage": {
                    "prompt_tokens": 321,
                    "completion_tokens": 17,
                    "total_tokens": 338,
                },
            }

        provider = CompatibleChatProvider(
            "deepseek",
            "fixture-model",
            api_key_env="FIXTURE_API_KEY",
            base_url="https://fixture.invalid",
            response_format="json_object",
        )
        with patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-key"}), patch(
            "game_l10n.providers._post_json", side_effect=fake_post
        ):
            with self.assertRaises(ProviderFailure) as raised:
                provider.generate_json(
                    system="Translate.",
                    user='{"translation_target":{"source":"S"}}',
                    schema={"type": "object"},
                    schema_name="game_translation",
                )

        self.assertEqual(raised.exception.code, "output_truncated")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(
            raised.exception.usage,
            {"input_tokens": 321, "output_tokens": 17, "total_tokens": 338},
        )

    def test_dynamic_output_budget_grows_after_truncation(self) -> None:
        limits: list[int] = []

        def fake_post(_url, _headers, payload, _timeout):
            limits.append(payload["max_tokens"])
            if len(limits) == 1:
                return {
                    "choices": [
                        {"finish_reason": "length", "message": {"content": ""}}
                    ]
                }
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"status":"success","text":"T","reason":""}'
                        },
                    }
                ]
            }

        provider = CompatibleChatProvider(
            "fixture",
            "fixture-model",
            api_key_env="FIXTURE_API_KEY",
            base_url="https://fixture.invalid",
            response_format="json_object",
            max_tokens_per_source_char=1.0,
            min_output_tokens=16,
            max_output_tokens=128,
        )
        kwargs = {
            "system": "Translate.",
            "user": '{"translation_target":{"source":"SHORT"}}',
            "schema": {"type": "object"},
            "schema_name": "game_translation",
        }
        with patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-key"}), patch(
            "game_l10n.providers._post_json", side_effect=fake_post
        ):
            with self.assertRaises(ProviderFailure) as raised:
                provider.generate_json(**kwargs)
            self.assertEqual(raised.exception.code, "output_truncated")
            self.assertTrue(raised.exception.retryable)
            result = provider.generate_json(**kwargs)

        self.assertEqual(result["status"], "success")
        self.assertEqual(limits, [16, 32])

    def test_dynamic_output_retry_state_is_bounded_and_cleared_at_cap(self) -> None:
        provider = CompatibleChatProvider(
            "fixture",
            "fixture-model",
            api_key_env="FIXTURE_API_KEY",
            base_url="https://fixture.invalid",
            max_tokens_per_source_char=1.0,
            min_output_tokens=16,
            max_output_tokens=32,
        )
        provider._MAX_OUTPUT_RETRY_STATES = 2
        self.assertTrue(provider._prepare_output_retry("one", "translation", 16))
        self.assertTrue(provider._prepare_output_retry("two", "translation", 16))
        self.assertTrue(provider._prepare_output_retry("three", "translation", 16))
        self.assertEqual(len(provider._output_retry_limits), 2)
        self.assertFalse(provider._prepare_output_retry("three", "translation", 32))
        self.assertEqual(len(provider._output_retry_limits), 1)

    def test_retry_after_header_is_preserved_for_rate_limit(self) -> None:
        error = urllib.error.HTTPError(
            "https://fixture.invalid",
            429,
            "rate limited",
            {"Retry-After": "3"},
            io.BytesIO(b"busy"),
        )
        with patch("game_l10n.providers.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(ProviderFailure) as raised:
                _post_json("https://fixture.invalid", {}, {}, 1)
        self.assertEqual(raised.exception.code, "rate_limited")
        self.assertEqual(raised.exception.retry_after_seconds, 3.0)

    def test_retry_after_header_is_preserved_for_retryable_server_error(self) -> None:
        error = urllib.error.HTTPError(
            "https://fixture.invalid",
            503,
            "temporarily unavailable",
            {"Retry-After": "2"},
            io.BytesIO(b"busy"),
        )
        with patch("game_l10n.providers.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(ProviderFailure) as raised:
                _post_json("https://fixture.invalid", {}, {}, 1)
        self.assertEqual(raised.exception.code, "provider_error")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.retry_after_seconds, 2.0)

    def test_plain_review_verdict_uses_tiny_contract_without_json_format(self) -> None:
        captured = {}

        def fake_post(url, headers, payload, timeout):
            captured.update(payload)
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "REWRITE meaning,voice"},
                    }
                ]
            }

        provider = CompatibleChatProvider(
            "fixture-judge",
            "fixture-model",
            api_key_env="FIXTURE_API_KEY",
            base_url="https://fixture.invalid",
            review_verdict_format="plain",
            task_max_output_tokens={"game_translation_review_judge": 24},
        )
        with patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-key"}), patch(
            "game_l10n.providers._post_json", side_effect=fake_post
        ):
            result = provider.generate_review_verdict(
                system="Judge.",
                user='{"translation_target":{"source":"SOURCE"}}',
                schema={"type": "object"},
                schema_name="game_translation_review_judge",
            )

        self.assertEqual(result, {"verdict": "rewrite", "issues": ["meaning", "voice"]})
        self.assertEqual(captured["max_tokens"], 24)
        self.assertNotIn("response_format", captured)

    def test_task_specific_review_cap_and_usage_metadata(self) -> None:
        captured = {}

        def fake_post(url, headers, payload, timeout):
            captured.update(payload)
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"verdict":"ok","issues":[]}'},
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 7, "total_tokens": 127},
            }

        provider = CompatibleChatProvider(
            "fixture-judge",
            "fixture-model",
            api_key_env="FIXTURE_API_KEY",
            base_url="https://fixture.invalid",
            response_format="json_object",
            task_max_output_tokens={"game_translation_review_judge": 64},
        )
        with patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-key"}), patch(
            "game_l10n.providers._post_json", side_effect=fake_post
        ):
            result = provider.generate_json(
                system="Judge.",
                user='{"translation_target":{"source":"SOURCE"}}',
                schema={"type": "object"},
                schema_name="game_translation_review_judge",
            )

        self.assertEqual(captured["max_tokens"], 64)
        self.assertEqual(
            result["_usage"],
            {"input_tokens": 120, "output_tokens": 7, "total_tokens": 127},
        )

    def test_deepseek_cache_usage_is_preserved(self) -> None:
        def fake_post(_url, _headers, _payload, _timeout):
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"status":"success","text":"T","reason":""}'},
                    }
                ],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 7,
                    "total_tokens": 127,
                    "prompt_cache_hit_tokens": 80,
                    "prompt_cache_miss_tokens": 40,
                },
            }

        provider = CompatibleChatProvider(
            "deepseek",
            "fixture-model",
            api_key_env="FIXTURE_API_KEY",
            base_url="https://fixture.invalid",
            response_format="json_object",
        )
        with patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-key"}), patch(
            "game_l10n.providers._post_json", side_effect=fake_post
        ):
            result = provider.generate_json(
                system="Translate.",
                user='{"translation_target":{"source":"S"}}',
                schema={"type": "object"},
                schema_name="game_translation",
            )

        self.assertEqual(result["_usage"]["cached_input_tokens"], 80)
        self.assertEqual(result["_usage"]["cache_miss_input_tokens"], 40)

    def test_task_input_budget_blocks_oversized_request_before_network(self) -> None:
        provider = CompatibleChatProvider(
            "deepseek",
            "fixture-model",
            api_key_env="FIXTURE_API_KEY",
            base_url="https://fixture.invalid",
            response_format="json_object",
            task_max_input_chars={"game_translation": 100},
        )
        with patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-key"}), patch(
            "game_l10n.providers._post_json"
        ) as post:
            with self.assertRaisesRegex(ProviderFailure, "budget_exhausted") as raised:
                provider.generate_json(
                    system="Translate.",
                    user=json.dumps({"translation_target": {"source": "X" * 200}}),
                    schema={"type": "object"},
                    schema_name="game_translation",
                )

        self.assertFalse(raised.exception.retryable)
        post.assert_not_called()

    def test_plain_review_verdict_accepts_effect_quality_codes(self) -> None:
        def fake_post(_url, _headers, _payload, _timeout):
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": "REWRITE function,knowledge_order,length_risk,slot_composition"
                        },
                    }
                ]
            }

        provider = CompatibleChatProvider(
            "fixture-judge",
            "fixture-model",
            api_key_env="FIXTURE_API_KEY",
            base_url="https://fixture.invalid",
            review_verdict_format="plain",
        )
        with patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-key"}), patch(
            "game_l10n.providers._post_json", side_effect=fake_post
        ):
            result = provider.generate_review_verdict(
                system="Judge.",
                user='{"translation_target":{"source":"SOURCE"}}',
                schema={"type": "object"},
                schema_name="game_translation_review_judge",
            )

        self.assertEqual(
            result,
            {
                "verdict": "rewrite",
                "issues": ["function", "knowledge_order", "length_risk", "slot_composition"],
            },
        )

    def test_json_object_mode_uses_deepseek_compatible_format(self) -> None:
        captured = {}

        def fake_post(url, headers, payload, timeout):
            captured.update({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"status":"success","text":"TARGET","reason":""}'
                        },
                    }
                ]
            }

        provider = CompatibleChatProvider(
            "deepseek",
            "deepseek-flash",
            api_key_env="DEEPSEEK_API_KEY",
            base_url="https://api.deepseek.com",
            response_format="json_object",
            temperature=0.0,
            thinking="disabled",
            reasoning_effort="none",
            max_tokens_per_source_char=4.0,
            min_output_tokens=128,
            max_output_tokens=2048,
        )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fixture-key"}), patch(
            "game_l10n.providers._post_json", side_effect=fake_post
        ):
            result = provider.generate_json(
                system="Return JSON.",
                user=json.dumps({"translation_target": {"source": "SOURCE"}}),
                schema={"type": "object"},
                schema_name="fixture_schema",
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(captured["payload"]["response_format"], {"type": "json_object"})
        system_message = captured["payload"]["messages"][0]["content"]
        self.assertTrue(system_message.startswith("Return JSON."))
        self.assertEqual(json.loads(system_message.splitlines()[-1]), {"type": "object"})
        self.assertEqual(captured["payload"]["temperature"], 0.0)
        self.assertEqual(captured["payload"]["thinking"], {"type": "disabled"})
        self.assertEqual(captured["payload"]["reasoning_effort"], "none")
        self.assertEqual(captured["payload"]["max_tokens"], 128)

    def test_context_request_keeps_material_size_and_transmits_schema(self) -> None:
        captured = {}
        context_schema = {
            "type": "object",
            "properties": {"status": {"enum": ["success", "refused", "error"]}},
            "required": ["status"],
            "additionalProperties": False,
        }
        material = json.dumps({"lines": [{"source": "SYNTHETIC_CONTEXT_" * 600}]})

        def fake_post(url, headers, payload, timeout):
            captured.update(payload)
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"status":"success"}'},
                    }
                ]
            }

        provider = CompatibleChatProvider(
            "fixture-context",
            "fixture-model",
            api_key_env="FIXTURE_API_KEY",
            base_url="https://fixture.invalid",
            response_format="json_object",
            max_tokens_per_source_char=4.0,
            min_output_tokens=128,
            max_output_tokens=8192,
        )
        with patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-key"}), patch(
            "game_l10n.providers._post_json", side_effect=fake_post
        ):
            provider.generate_json(
                system="Analyze context.",
                user=material,
                schema=context_schema,
                schema_name="game_functional_context",
            )

        self.assertEqual(captured["max_tokens"], 8192)
        self.assertEqual(captured["messages"][1]["content"], material)
        self.assertEqual(
            json.loads(captured["messages"][0]["content"].splitlines()[-1]),
            context_schema,
        )

    def test_openai_reasoning_effort_is_explicit(self) -> None:
        captured = {}

        def fake_post(url, headers, payload, timeout):
            captured.update({"url": url, "payload": payload})
            return {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"status":"success","text":"TARGET","reason":""}',
                            }
                        ],
                    }
                ],
            }

        provider = OpenAIResponsesProvider(
            "luna",
            "gpt-6-luna",
            reasoning_effort="none",
            max_output_tokens=512,
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-key"}), patch(
            "game_l10n.providers._post_json", side_effect=fake_post
        ):
            provider.generate_json(
                system="Return JSON.",
                user="FIXTURE",
                schema={"type": "object"},
                schema_name="fixture_schema",
            )

        self.assertEqual(captured["payload"]["reasoning"], {"effort": "none"})
        self.assertEqual(captured["payload"]["max_output_tokens"], 512)

    def test_openai_incomplete_output_is_classified_and_usage_preserved(self) -> None:
        provider = OpenAIResponsesProvider("luna", "fixture-model", max_output_tokens=64)

        def fake_post(_url, _headers, _payload, _timeout):
            return {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"input_tokens": 40, "output_tokens": 64, "total_tokens": 104},
            }

        with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-key"}), patch(
            "game_l10n.providers._post_json", side_effect=fake_post
        ):
            with self.assertRaises(ProviderFailure) as raised:
                provider.generate_json(
                    system="Translate.",
                    user="FIXTURE",
                    schema={"type": "object"},
                    schema_name="game_translation",
                )
        self.assertEqual(raised.exception.code, "output_truncated")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.usage["total_tokens"], 104)


class ProviderProtocolTests(unittest.TestCase):
    @staticmethod
    def _providers(*, plain: bool = False):
        verdict_format = "plain" if plain else "json"
        return [
            CompatibleChatProvider(
                "fixture-chat", "fixture-model", api_key_env="FIXTURE_API_KEY",
                base_url="https://fixture.invalid", review_verdict_format=verdict_format,
            ),
            OpenAIResponsesProvider(
                "fixture-responses", "fixture-model", api_key_env="FIXTURE_API_KEY",
                review_verdict_format=verdict_format,
            ),
        ]

    @staticmethod
    def _reply(provider, text, *, usage=None):
        if isinstance(provider, CompatibleChatProvider):
            data = {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}
        else:
            data = {"status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": text}]}
            ]}
        if usage is not None:
            data["usage"] = usage
        return data

    @staticmethod
    def _request(provider, *, plain=False):
        method = provider.generate_review_verdict if plain else provider.generate_json
        return method(system="fixture", user="fixture", schema={"type": "object"},
                      schema_name="game_translation_review_judge" if plain else "game_translation")

    def test_http_requires_utf8_json_object(self) -> None:
        for body in (b"null", b"[]", b"true", b'[["status", "success"]]', b"{", b"\xff"):
            with self.subTest(body=body), patch(
                "game_l10n.providers.urllib.request.urlopen", return_value=io.BytesIO(body)
            ), self.assertRaises(ProviderFailure) as raised:
                _post_json("https://fixture.invalid", {}, {})
            self.assertEqual(raised.exception.code, "malformed_response")
            self.assertTrue(raised.exception.retryable)

    def test_http_connection_and_read_failures_stay_categorical(self) -> None:
        for error in (ConnectionResetError("fixture"), http.client.IncompleteRead(b"fixture")):
            with self.subTest(error=type(error).__name__), patch(
                "game_l10n.providers.urllib.request.urlopen", side_effect=error
            ), self.assertRaises(ProviderFailure) as raised:
                _post_json("https://fixture.invalid", {}, {})
            self.assertEqual(str(raised.exception), "provider_error")
            self.assertTrue(raised.exception.retryable)

    def test_http_payment_required_is_exhausted_quota(self) -> None:
        error = urllib.error.HTTPError("https://fixture.invalid", 402, "fixture", {}, io.BytesIO(b"fixture"))
        with patch("game_l10n.providers.urllib.request.urlopen", side_effect=error), self.assertRaises(ProviderFailure) as raised:
            _post_json("https://fixture.invalid", {}, {})
        self.assertEqual(raised.exception.code, "quota_exhausted")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.status_code, 402)

    def test_nonfinite_retry_after_cannot_create_an_infinite_cooldown(self) -> None:
        for value in ("inf", "Infinity", "NaN"):
            error = urllib.error.HTTPError("https://fixture.invalid", 429, "fixture",
                                           {"Retry-After": value}, io.BytesIO(b"busy"))
            with self.subTest(value=value), patch("game_l10n.providers.urllib.request.urlopen", side_effect=error), self.assertRaises(ProviderFailure) as raised:
                _post_json("https://fixture.invalid", {}, {})
            self.assertIsNone(raised.exception.retry_after_seconds)

    def test_api_model_output_requires_an_object_and_preserves_usage_on_failure(self) -> None:
        for provider in self._providers():
            for text in ("null", "[]", "true", '[["status", "success"]]', "{", ["fixture"]):
                with self.subTest(provider=provider.name, text=text), patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-key"}), patch(
                    "game_l10n.providers._post_json", return_value=self._reply(provider, text, usage={"input_tokens": 7})
                ), self.assertRaises(ProviderFailure) as raised:
                    self._request(provider)
                self.assertEqual(raised.exception.code, "malformed_response")
                self.assertEqual(raised.exception.usage, {"input_tokens": 7})

    def test_malformed_api_envelopes_preserve_usage(self) -> None:
        chat, responses = self._providers()
        cases = [
            (chat, {"choices": value}) for value in (None, [], {}, [None], [{"message": []}])
        ] + [
            (responses, {"output": value}) for value in (None, {}, [None], [{"type": "message", "content": None}],
                                                       [{"type": "message", "content": [None]}])
        ]
        for provider, data in cases:
            data["usage"] = {"input_tokens": 9}
            with self.subTest(provider=provider.name, data=data), patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-key"}), patch(
                "game_l10n.providers._post_json", return_value=data
            ), self.assertRaises(ProviderFailure) as raised:
                self._request(provider)
            self.assertEqual(raised.exception.code, "malformed_response")
            self.assertEqual(raised.exception.usage, {"input_tokens": 9})

    def test_api_usage_is_not_taken_from_model_output(self) -> None:
        for provider in self._providers():
            for usage in (None, {"input_tokens": 8}):
                data = self._reply(provider, '{"status":"success","_usage":{"input_tokens":999}}', usage=usage)
                with self.subTest(provider=provider.name, usage=usage), patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-key"}), patch(
                    "game_l10n.providers._post_json", return_value=data
                ):
                    result = self._request(provider)
                self.assertEqual(result.get("_usage"), {"input_tokens": 8} if usage else None)

    def test_usage_rejects_booleans_and_falls_back_to_valid_aliases(self) -> None:
        usage = {
            "input_tokens": True, "prompt_tokens": 11, "output_tokens": False,
            "completion_tokens": 3, "total_tokens": True,
            "output_tokens_details": {"reasoning_tokens": True},
            "prompt_cache_hit_tokens": -1, "input_tokens_details": {"cached_tokens": 5},
            "prompt_cache_miss_tokens": False,
        }
        for provider in self._providers():
            with self.subTest(provider=provider.name), patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-key"}), patch(
                "game_l10n.providers._post_json", return_value=self._reply(provider, "{}", usage=usage)
            ):
                result = self._request(provider)
            self.assertEqual(result["_usage"], {"input_tokens": 11, "output_tokens": 3, "cached_input_tokens": 5})

    def test_plain_verdict_rejects_extra_issues_or_malformed_separators(self) -> None:
        for provider in self._providers(plain=True):
            for text in ("REWRITE meaning,omission,addition,voice,register", "REWRITE meaning,", "OK ,", "OK\n", ["OK"]):
                with self.subTest(provider=provider.name, text=text), patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-key"}), patch(
                    "game_l10n.providers._post_json", return_value=self._reply(provider, text, usage={"input_tokens": 4})
                ), self.assertRaises(ProviderFailure) as raised:
                    self._request(provider, plain=True)
                self.assertEqual(raised.exception.code, "malformed_response")
                self.assertEqual(raised.exception.usage, {"input_tokens": 4})

    def test_responses_combines_text_parts_but_never_ignores_refusal(self) -> None:
        provider = self._providers()[1]
        data = {"status": "completed", "output": [{"type": "reasoning"}, {
            "type": "message", "content": [
                {"type": "output_text", "text": '{"status":'},
                {"type": "output_text", "text": '"success"}'},
            ]
        }], "usage": {"input_tokens": 6}}
        with patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-key"}), patch(
            "game_l10n.providers._post_json", return_value=data
        ):
            self.assertEqual(self._request(provider)["status"], "success")
            data["output"][1]["content"].append({"type": "refusal", "refusal": "fixture-private-detail"})
            result = self._request(provider)
        self.assertEqual(result["status"], "refused")
        self.assertNotIn("fixture-private-detail", json.dumps(result))
        self.assertEqual(result["_usage"], {"input_tokens": 6})

    def test_responses_failure_categories_match_plain_and_json_paths(self) -> None:
        cases = [
            ({"status": "failed", "error": {"code": "insufficient_quota"}}, "quota_exhausted", False),
            ({"status": "failed", "error": {"code": "invalid_api_key"}}, "authentication_error", False),
            ({"status": "failed", "error": {"code": "rate_limit_exceeded"}}, "rate_limited", True),
            ({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}, "output_truncated", False),
        ]
        for plain in (False, True):
            provider = self._providers(plain=plain)[1]
            for data, code, retryable in cases:
                with self.subTest(plain=plain, code=code), patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-key"}), patch(
                    "game_l10n.providers._post_json", return_value={**data, "usage": {"input_tokens": 2}}
                ), self.assertRaises(ProviderFailure) as raised:
                    self._request(provider, plain=plain)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertEqual(raised.exception.usage, {"input_tokens": 2})

    def test_content_filter_is_a_refusal_in_both_api_formats(self) -> None:
        for plain in (False, True):
            chat, responses = self._providers(plain=plain)
            for provider, data in (
                (chat, {"choices": [{"finish_reason": "content_filter", "message": None}]}),
                (responses, {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}}),
            ):
                with self.subTest(plain=plain, provider=provider.name), patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-key"}), patch(
                    "game_l10n.providers._post_json", return_value=data
                ):
                    result = self._request(provider, plain=plain)
                self.assertEqual(result["verdict" if plain else "status"], "refused")


class CommandProviderProtocolTests(unittest.TestCase):
    def test_command_transport_preserves_inert_input_and_direct_arguments(self) -> None:
        command = ["fixture-command", "--mode", "", "fixture argument"]
        provider = CommandProvider("fixture", command, cwd="fixture-directory")
        command.append("unexpected")
        completed = subprocess.CompletedProcess(command, 0, stdout='{"status":"success"}', stderr="fixture-private-detail")
        with patch("game_l10n.providers.subprocess.run", return_value=completed) as run:
            result = provider.generate_json(system="fixture", user="$(fixture);\ud55c\uae00", schema={}, schema_name="fixture")
        self.assertEqual(result, {"status": "success"})
        self.assertEqual(run.call_args.args[0], command[:-1])
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["cwd"], "fixture-directory")
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["input"], "$(fixture);\ud55c\uae00")
        self.assertEqual(provider.fingerprint()["cwd"], "fixture-directory")

    def test_command_failures_never_export_runner_diagnostics(self) -> None:
        provider = CommandProvider("fixture", ["fixture-command"])
        cases = [
            (subprocess.CompletedProcess([], 1, stdout="", stderr="fixture-private-detail"), "provider_error"),
            (subprocess.CompletedProcess([], 0, stdout="null", stderr=""), "malformed_response"),
            (subprocess.CompletedProcess([], 0, stdout='[["status","success"]]', stderr=""), "malformed_response"),
            (subprocess.CompletedProcess([], 0, stdout="{} {}", stderr=""), "malformed_response"),
        ]
        for completed, code in cases:
            with self.subTest(code=code, stdout=completed.stdout), patch("game_l10n.providers.subprocess.run", return_value=completed), self.assertRaises(ProviderFailure) as raised:
                provider.generate_json(system="fixture", user="fixture", schema={}, schema_name="fixture")
            self.assertEqual(str(raised.exception), code)
            self.assertTrue(raised.exception.retryable)
        for error, code in (
            (FileNotFoundError("fixture-private-detail"), "provider_error"),
            (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "fixture-private-detail"), "malformed_response"),
        ):
            with self.subTest(code=code), patch("game_l10n.providers.subprocess.run", side_effect=error), self.assertRaises(ProviderFailure) as raised:
                provider.generate_json(system="fixture", user="fixture", schema={}, schema_name="fixture")
            self.assertEqual(str(raised.exception), code)

    def test_command_configuration_requires_an_argument_array(self) -> None:
        for command in ("fixture-command", [], [None], [1], [""], ["  "], ["fixture\0command"]):
            config = {"provider_preset": "custom", "control_plane": CONTROL_OBSERVATION_POLICY,
                      "providers": [{"type": "command", "name": "fixture", "command": command}]}
            with self.subTest(command=command), self.assertRaises(ConfigurationError):
                CommandProvider("fixture", command)
            with self.subTest(registry_command=command), self.assertRaises(ConfigurationError):
                ProviderRegistry.from_config(config, Path("."))


class ProviderBudgetConfigurationTests(unittest.TestCase):
    def test_invalid_limits_fail_at_configuration_instead_of_at_request_time(self) -> None:
        for field in ("max_tokens", "min_output_tokens", "max_output_tokens", "max_input_chars"):
            for value in (0, -1, True, 1.5, float("inf"), "invalid"):
                config = {"provider_preset": "custom", "control_plane": CONTROL_OBSERVATION_POLICY,
                          "providers": [{"type": "compatible_chat", "name": "fixture", "model": "fixture",
                                         "base_url": "https://fixture.invalid", field: value}]}
                with self.subTest(field=field, value=value), self.assertRaises(ConfigurationError):
                    ProviderRegistry.from_config(config, Path("."))
        for field in ("task_max_output_tokens", "task_max_input_chars"):
            for value in ({"fixture": 0}, {"fixture": True}, []):
                with self.subTest(field=field, value=value), self.assertRaises(ConfigurationError):
                    CompatibleChatProvider("fixture", "fixture", api_key_env="", base_url="https://fixture.invalid", **{field: value})
        for value in (0, True, float("nan"), float("inf"), -1):
            with self.subTest(ratio=value), self.assertRaises(ConfigurationError):
                CompatibleChatProvider("fixture", "fixture", api_key_env="", base_url="https://fixture.invalid", max_tokens_per_source_char=value)
        with self.assertRaises(ConfigurationError):
            CompatibleChatProvider("fixture", "fixture", api_key_env="", base_url="https://fixture.invalid", min_output_tokens=100, max_output_tokens=10)
        for value in (0, True, 1.5):
            with self.subTest(responses_limit=value), self.assertRaises(ConfigurationError):
                OpenAIResponsesProvider("fixture", "fixture", max_output_tokens=value)

    def test_large_finite_ratio_still_obeys_the_configured_output_cap(self) -> None:
        provider = CompatibleChatProvider("fixture", "fixture", api_key_env="", base_url="https://fixture.invalid",
                                          max_tokens_per_source_char=1e308, min_output_tokens=16, max_output_tokens=128)
        self.assertEqual(provider._request_max_tokens("fixture", "fixture"), 128)


class ProviderEdgeCaseTests(unittest.TestCase):
    def test_nonstandard_json_constants_are_malformed_in_all_transports(self) -> None:
        for text in ('{"value":NaN}', '{"value":Infinity}', '{"value":-Infinity}'):
            with self.subTest(transport="http", text=text), patch(
                "game_l10n.providers.urllib.request.urlopen", return_value=io.BytesIO(text.encode("utf-8"))
            ), self.assertRaises(ProviderFailure) as raised:
                _post_json("https://fixture.invalid", {}, {})
            self.assertEqual(raised.exception.code, "malformed_response")
            for provider in ProviderProtocolTests._providers():
                with self.subTest(transport=provider.name, text=text), patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-key"}), patch(
                    "game_l10n.providers._post_json", return_value=ProviderProtocolTests._reply(provider, text)
                ), self.assertRaises(ProviderFailure) as raised:
                    ProviderProtocolTests._request(provider)
                self.assertEqual(raised.exception.code, "malformed_response")
            with self.subTest(transport="command", text=text), patch(
                "game_l10n.providers.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=text, stderr="")
            ), self.assertRaises(ProviderFailure) as raised:
                CommandProvider("fixture", ["fixture-command"]).generate_json(system="fixture", user="fixture", schema={}, schema_name="fixture")
            self.assertEqual(raised.exception.code, "malformed_response")

    def test_bare_rewrite_is_malformed_without_losing_usage(self) -> None:
        for provider in ProviderProtocolTests._providers(plain=True):
            with self.subTest(provider=provider.name), patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-key"}), patch(
                "game_l10n.providers._post_json", return_value=ProviderProtocolTests._reply(provider, "REWRITE", usage={"input_tokens": 3})
            ), self.assertRaises(ProviderFailure) as raised:
                ProviderProtocolTests._request(provider, plain=True)
            self.assertEqual(raised.exception.code, "malformed_response")
            self.assertEqual(raised.exception.usage, {"input_tokens": 3})

    def test_http_error_category_survives_a_truncated_error_body(self) -> None:
        error = urllib.error.HTTPError("https://fixture.invalid", 503, "fixture", {"Retry-After": "2"}, io.BytesIO())
        with patch("game_l10n.providers.urllib.request.urlopen", side_effect=error), patch.object(
            error, "read", side_effect=http.client.IncompleteRead(b"fixture")
        ), self.assertRaises(ProviderFailure) as raised:
            _post_json("https://fixture.invalid", {}, {})
        self.assertEqual(raised.exception.code, "provider_error")
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.retry_after_seconds, 2)

    def test_command_working_directory_is_validated_before_resolution(self) -> None:
        for cwd in ("", "fixture\0path", 1, b"fixture"):
            config = {"provider_preset": "custom", "control_plane": CONTROL_OBSERVATION_POLICY,
                      "providers": [{"type": "command", "name": "fixture", "command": ["fixture-command"], "cwd": cwd}]}
            with self.subTest(cwd=cwd), self.assertRaises(ConfigurationError):
                CommandProvider("fixture", ["fixture-command"], cwd=cwd)
            with self.subTest(registry_cwd=cwd), self.assertRaises(ConfigurationError):
                ProviderRegistry.from_config(config, Path("fixture-root"))
        config["providers"][0]["cwd"] = "runner"
        provider = ProviderRegistry.from_config(config, Path("fixture-root")).get("fixture")
        self.assertEqual(provider.cwd, str(Path("fixture-root") / "runner"))
        fingerprint = provider.fingerprint()
        fingerprint["command"].append("unexpected")
        self.assertEqual(provider.command, ["fixture-command"])


if __name__ == "__main__":
    unittest.main()
