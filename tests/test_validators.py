from __future__ import annotations

import unittest

from game_l10n.models import Segment
from game_l10n.validators import validate_integrity


class ValidatorTests(unittest.TestCase):
    def test_value_slot_rejects_repeated_fixed_prefix(self) -> None:
        segment = Segment(
            "fixture-state",
            "SOURCE_STATE",
            metadata={
                "ui_slot_role": "state_value",
                "ui_state_semantics": "enabled",
                "ui_fixed_target_prefix": "TARGET_LABEL: ",
            },
        )

        issues = validate_integrity(segment, "TARGET_LABEL: TARGET_ENABLED", [])
        self.assertIn("ui-composition-duplication", {item.code for item in issues})

    def test_value_slot_accepts_only_the_value_payload(self) -> None:
        segment = Segment(
            "fixture-state",
            "SOURCE_STATE",
            metadata={
                "ui_slot_role": "state_value",
                "ui_state_semantics": "enabled",
                "ui_fixed_target_prefix": "TARGET_LABEL: ",
            },
        )

        issues = validate_integrity(segment, "TARGET_ENABLED", [])
        self.assertNotIn("ui-composition-duplication", {item.code for item in issues})

    def test_explicit_character_limit_is_enforced(self) -> None:
        segment = Segment(
            "fixture-label",
            "SOURCE",
            metadata={"ui_slot_role": "label", "ui_max_chars": 5},
        )

        issues = validate_integrity(segment, "TARGET", [])
        self.assertIn("ui-length-limit", {item.code for item in issues})


if __name__ == "__main__":
    unittest.main()
