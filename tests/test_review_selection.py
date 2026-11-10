from __future__ import annotations

import unittest

from game_l10n.models import Segment, ValidationIssue
from game_l10n.review_selection import (
    review_category,
    rewrite_issue_codes,
    strong_review_selection_reasons,
    target_language_review_reasons,
)


class ReviewSelectionTests(unittest.TestCase):
    def test_rewrite_keeps_every_hard_gate_before_bounded_judge_findings(self) -> None:
        integrity = [
            ValidationIssue("glossary-missing", "fixture"),
            ValidationIssue("markup-malformed", "fixture"),
            ValidationIssue("script-intrusion", "fixture"),
            ValidationIssue("ui-composition-duplication", "fixture"),
            ValidationIssue("ui-length-limit", "fixture"),
        ]

        codes = rewrite_issue_codes(
            ["meaning", "voice", "register", "literalism", "reference_loss"],
            integrity,
            language_risk=True,
        )

        self.assertEqual(
            codes,
            [
                "terminology",
                "formatting",
                "addition",
                "slot_composition",
                "length_risk",
                "fluency",
                "meaning",
                "voice",
                "register",
                "literalism",
            ],
        )

    def test_selection_is_text_independent_for_same_id_and_metadata(self) -> None:
        first = Segment("fixture", "SOURCE_A", metadata={"text_type": "ordinary"})
        second = Segment("fixture", "SOURCE_B", metadata={"text_type": "ordinary"})

        self.assertEqual(
            strong_review_selection_reasons(first, sample_percent=17),
            strong_review_selection_reasons(second, sample_percent=17),
        )

    def test_evidence_flag_and_category_select_strong_review(self) -> None:
        segment = Segment(
            "fixture",
            "SOURCE",
            metadata={"review_category": "parody", "reference_cue": True},
        )

        reasons = strong_review_selection_reasons(segment, sample_percent=0)

        self.assertEqual(review_category(segment), "parody")
        self.assertIn("high_risk_category", reasons)
        self.assertIn("explicit_high_risk_flag", reasons)

    def test_invalid_sample_rate_is_rejected(self) -> None:
        for value in (-0.1, 100.1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "between 0 and 100"):
                    strong_review_selection_reasons(
                        Segment("fixture", "SOURCE"), sample_percent=value
                    )

    def test_missing_target_script_routes_long_candidate_to_strong_review(self) -> None:
        self.assertEqual(
            target_language_review_reasons("This is still English", "ko"),
            ["target_script_absent"],
        )
        self.assertEqual(
            target_language_review_reasons("\ud55c\uae00 \ubb38\uc7a5", "ko"), []
        )
        self.assertEqual(target_language_review_reasons("HP", "ko"), [])
        self.assertEqual(
            target_language_review_reasons(
                "Darwin",
                "ko",
                {
                    "target_language_exception": "proper_name",
                    "target_language_exception_evidence_sha256": "a" * 64,
                },
            ),
            [],
        )
        self.assertEqual(
            target_language_review_reasons(
                "Darwin", "ko", {"target_language_exception": "proper_name"}
            ),
            ["target_script_absent"],
        )

    def test_small_target_script_fragment_cannot_mask_english_prose(self) -> None:
        self.assertEqual(
            target_language_review_reasons(
                "This entire sentence is still written in English \uc785\ub2c8\ub2e4", "ko"
            ),
            ["target_script_contamination"],
        )


    def test_provenance_reasons_need_exact_cue_ids_or_fallback_origin(self) -> None:
        from game_l10n.review_selection import provenance_review_reasons

        cues = ({"evidence_line_ids": ["fixture-1"], "scene": "fixture-scene"},)
        self.assertEqual(provenance_review_reasons("fixture-1", cues), ["context_reference_cue"])
        self.assertEqual(provenance_review_reasons("fixture-2", cues), [])
        self.assertEqual(
            provenance_review_reasons("fixture-2", cues, non_primary_translation=True),
            ["fallback_translation"],
        )

if __name__ == "__main__":
    unittest.main()
