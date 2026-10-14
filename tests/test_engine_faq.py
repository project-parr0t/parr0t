from __future__ import annotations

import unittest

from scripts.check_engine_faq import validate_engine_faq_text


class EngineFaqTests(unittest.TestCase):
    def test_empty_reviewed_runbook_is_valid(self) -> None:
        text = """<!-- parr0t-engine-faq:v1 -->
# Engine exception runbook

_No SDK-external engine exceptions are currently documented._
"""
        self.assertEqual(validate_engine_faq_text(text), [])

    def test_manual_exception_entry_is_valid(self) -> None:
        text = """<!-- parr0t-engine-faq:v1 -->
# Engine exception runbook

## External format revision is unsupported

- **Trigger:** The public parser reports `UNSUPPORTED_FORMAT`.
- **Automation boundary:** Public metadata does not identify a compatible external parser.
- **Stop condition:** Stop before extracting or rebuilding any payload.
- **Required input or tool:** A user-approved parser release that documents the format revision.
- **Procedure:** Replace only the private workspace parser and rerun read-only discovery.
- **Resume verification:** The parser identifies the revision and the synthetic round trip passes.
- **Do not:** Guess a format revision or mutate the source data.
"""
        self.assertEqual(validate_engine_faq_text(text), [])

    def test_common_question_shape_is_not_an_engine_runbook(self) -> None:
        text = """<!-- parr0t-engine-faq:v1 -->
# Engine exception runbook

## Why does extraction fail?

- **Symptom:** Extraction failed.
- **Resolution:** Try another tool.
"""
        errors = validate_engine_faq_text(text)
        self.assertTrue(any("Automation boundary" in item for item in errors))
        self.assertTrue(any("Resume verification" in item for item in errors))

    def test_entry_requires_explicit_prohibition(self) -> None:
        text = """<!-- parr0t-engine-faq:v1 -->
# Engine exception runbook

## External decision required

- **Trigger:** The adapter reports `DECISION_REQUIRED`.
- **Automation boundary:** Two public formats are indistinguishable statically.
- **Stop condition:** Stop before applying output.
- **Required input or tool:** User-provided runtime evidence.
- **Procedure:** Record the evidence and select the matching public format.
- **Resume verification:** The selected parser passes the synthetic round trip.
"""
        errors = validate_engine_faq_text(text)
        self.assertTrue(any("Do not" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
