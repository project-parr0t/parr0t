from __future__ import annotations

import unittest

from unity_textasset import _select_source_locale, _token_mismatch, parse_ini, rewrite_ini
from unity_serialized import pack_string, string_at
from unity_layout import FontMetrics, Rect, TextLayout, resolve_rect_size, screen_text


class UnityTextAssetPrototypeTests(unittest.TestCase):
    def test_standalone_string_interface(self) -> None:
        payload = pack_string("SOURCE_{0}")
        self.assertEqual(string_at(payload, 0), ("SOURCE_{0}", len(payload)))

    def test_pure_layout_interface(self) -> None:
        size = resolve_rect_size({"canvas": Rect(), "label": Rect("canvas")},
                                 "label", (100, 100))
        metrics = FontMetrics(1000, 1200, {ord(ch): 500 for ch in "SOURCE"}, 1000)
        result = screen_text("SOURCE", size, metrics, TextLayout(10))
        self.assertFalse(result["conservative_overflow"])
        self.assertEqual(result["missing_codepoints"], [])
        self.assertFalse(result["runtime_verified"])

    def test_parse_and_rewrite_preserve_order_and_line_endings(self) -> None:
        source = "[Fixture]\r\nFirst=SOURCE_ONE\r\nSecond=SOURCE_TWO\r\n"
        entries = parse_ini(source)
        rebuilt, replaced = rewrite_ini(
            source,
            {("Fixture", "First"): "TARGET_ONE", ("Fixture", "Second"): "TARGET_TWO"},
        )
        self.assertEqual([entry.pair for entry in entries], [("Fixture", "First"), ("Fixture", "Second")])
        self.assertEqual(replaced, [("Fixture", "First"), ("Fixture", "Second")])
        self.assertEqual(rebuilt, "[Fixture]\r\nFirst=TARGET_ONE\r\nSecond=TARGET_TWO\r\n")

    def test_duplicate_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "INI_DUPLICATE_KEY"):
            parse_ini("[Fixture]\nKey=A\nKey=B\n")

    def test_source_locale_uses_neutral_reference_overlap(self) -> None:
        tables = {
            "reference-table": parse_ini("[Reference]\n1=SOURCE_ONE\n2=SOURCE_TWO\n"),
            "en": parse_ini("[Fixture]\nOne=SOURCE_ONE\nTwo=SOURCE_TWO\n"),
            "ko": parse_ini("[Fixture]\nOne=TARGET_ONE\nTwo=TARGET_TWO\n"),
        }
        self.assertEqual(_select_source_locale(tables, "ko"), "en")

    def test_placeholder_mismatch_is_rejected(self) -> None:
        self.assertFalse(_token_mismatch("SOURCE {0} <b>%s</b>", "TARGET {0} <b>%s</b>"))
        self.assertTrue(_token_mismatch("SOURCE {0}", "TARGET"))


if __name__ == "__main__":
    unittest.main()
