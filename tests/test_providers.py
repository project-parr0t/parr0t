from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from game_l10n.providers import CompatibleChatProvider, OpenAIResponsesProvider


class CompatibleProviderTests(unittest.TestCase):
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

    def test_plain_review_verdict_accepts_effect_quality_codes(self) -> None:
        def fake_post(_url, _headers, _payload, _timeout):
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": "REWRITE literalism,reference_loss,tone_rhythm"
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
                "issues": ["literalism", "reference_loss", "tone_rhythm"],
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
        self.assertEqual(captured["payload"]["temperature"], 0.0)
        self.assertEqual(captured["payload"]["thinking"], {"type": "disabled"})
        self.assertEqual(captured["payload"]["reasoning_effort"], "none")
        self.assertEqual(captured["payload"]["max_tokens"], 128)

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
