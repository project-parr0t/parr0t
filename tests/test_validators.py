from __future__ import annotations

import hashlib
import unittest

from game_l10n.models import GlossaryEntry, Segment
from game_l10n.store import stable_hash
from game_l10n.validators import protected_tokens, validate_integrity


def optional_slot_fixture(
    source: str = "SOURCE_OPTIONAL", *, target_id: str = "fixture-slot"
) -> Segment:
    metadata = {
        "ui_slot_role": "template_fragment",
        "ui_composed_with_ids": ["fixture-label"],
        "ui_render_pattern": "{prefix}{value}",
        "ui_fixed_target_prefix": "TARGET_LABEL",
    }
    source_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    composition = {
        "target_id": target_id, "source_sha256": source_digest,
        **metadata, "ui_fixed_target_suffix": "",
    }
    metadata["ui_empty_target"] = {
        "schema": "parr0t.ui-empty-target/v1",
        "reason_code": "optional_grammatical_slot",
        "source_sha256": source_digest,
        "composition_sha256": stable_hash(composition),
        "evidence_sha256": hashlib.sha256(b"SYNTHETIC_STATIC_BINDING").hexdigest(),
    }
    return Segment(target_id, source, metadata=metadata)


class ValidatorTests(unittest.TestCase):
    def test_exact_whitespace_source_is_preserved_but_not_rewritten(self) -> None:
        segment = Segment("fixture-whitespace", "  \t")
        self.assertEqual(validate_integrity(segment, "  \t", []), [])
        self.assertEqual(
            [item.code for item in validate_integrity(segment, " ", [])],
            ["empty"],
        )

    def test_empty_stays_rejected_by_default_and_boolean_flags(self) -> None:
        for metadata in ({}, {"allow_empty": True}, {"ui_empty_target": True}):
            self.assertEqual(
                [item.code for item in validate_integrity(Segment("fixture", "SOURCE", metadata=metadata), "", [])],
                ["empty"],
            )

    def test_bound_optional_slot_accepts_exact_empty_but_not_whitespace(self) -> None:
        segment = optional_slot_fixture()
        self.assertEqual(validate_integrity(segment, "", []), [])
        self.assertEqual([item.code for item in validate_integrity(segment, " ", [])], ["empty"])

    def test_optional_slot_authority_fails_closed_for_changed_bindings(self) -> None:
        for key, value in (
            ("ui_slot_role", "standalone"),
            ("ui_fixed_target_prefix", ""),
            ("ui_composed_with_ids", []),
            ("ui_render_pattern", "{prefix}"),
        ):
            with self.subTest(key=key):
                segment = optional_slot_fixture()
                segment.metadata[key] = value
                self.assertEqual([item.code for item in validate_integrity(segment, "", [])], ["empty"])
        segment = optional_slot_fixture()
        changed = Segment(segment.id, "SOURCE_CHANGED", metadata=segment.metadata)
        self.assertEqual([item.code for item in validate_integrity(changed, "", [])], ["empty"])
        segment.metadata["ui_empty_target"]["evidence_sha256"] = ""
        self.assertEqual([item.code for item in validate_integrity(segment, "", [])], ["empty"])

    def test_optional_empty_does_not_skip_tokens_or_glossary(self) -> None:
        segment = optional_slot_fixture("SOURCE {count}")
        self.assertIn("protected-token-mismatch", {item.code for item in validate_integrity(segment, "", [])})
        segment = optional_slot_fixture()
        self.assertIn("glossary-missing", {item.code for item in validate_integrity(segment, "", [GlossaryEntry("SOURCE", "TARGET")])})

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

    def test_protected_tokens_ignore_prose_and_count_escapes_once(self) -> None:
        cases = {
            "50% off": {},
            "%s and %1$d at 100%%": {"%s": 1, "%1$d": 1, "%%": 1},
            "\\nWord": {"\\n": 1},
            "\\C[2]Name\\C[0]": {"\\C[2]": 1, "\\C[0]": 1},
            "\\G \\V[1]": {"\\G": 1, "\\V[1]": 1},
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(dict(protected_tokens(text)), expected)

    def test_translated_prose_after_escape_is_not_a_token_mismatch(self) -> None:
        for source, candidate in (("\\nWord", "\\n\ub2e8\uc5b4"), ("50% off", "50% \ud560\uc778")):
            with self.subTest(source=source):
                issues = validate_integrity(Segment("fixture-token", source), candidate, [])
                self.assertNotIn("protected-token-mismatch", {item.code for item in issues})


if __name__ == "__main__":
    unittest.main()
