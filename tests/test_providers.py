from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from game_l10n.errors import ProviderFailure
from game_l10n.providers import CompatibleChatProvider, OpenAIResponsesProvider


class CompatibleProviderTests(unittest.TestCase):
    def test_malformed_response_preserves_provider_usage(self) -> None:
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

        self.assertEqual(raised.exception.code, "malformed_response")
        self.assertEqual(
            raised.exception.usage,
            {"input_tokens": 321, "output_tokens": 17, "total_tokens": 338},
        )

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


if __name__ == "__main__":
    unittest.main()
