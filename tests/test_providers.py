from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from game_l10n.providers import CompatibleChatProvider, OpenAIResponsesProvider


class CompatibleProviderTests(unittest.TestCase):
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
