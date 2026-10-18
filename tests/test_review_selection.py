from __future__ import annotations

import unittest

from game_l10n.models import Segment
from game_l10n.review_selection import (
    review_category,
    strong_review_selection_reasons,
)


class ReviewSelectionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
