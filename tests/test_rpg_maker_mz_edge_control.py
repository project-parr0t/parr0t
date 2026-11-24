from __future__ import annotations

import unittest

from game_l10n.rpg_maker_mz.edge_control import restore_missing_edge_control


class EdgeControlTests(unittest.TestCase):
    def test_repairs_only_one_source_edge_and_preserves_protected_operands(self):
        for source, candidate, expected in (
            (r"\> SOURCE \V[2] %1", r"TARGET \V[2] %1", r"\>TARGET \V[2] %1"),
            (r"SOURCE \V[2] %1\^", r"TARGET \V[2] %1", r"TARGET \V[2] %1\^"),
            (r"SOURCE \V[2] %1\^", r"TARGET \V[2] %1\^", r"TARGET \V[2] %1\^"),
        ):
            with self.subTest(source=source):
                self.assertEqual(restore_missing_edge_control(source, candidate), expected)

    def test_rejects_multiple_positions_internal_controls_and_altered_variables(self):
        for source, candidate in (
            (r"\.SOURCE\.", r"\.TARGET"),
            (r"SOURCE\. CONTINUES", "TARGET"),
            (r"\>SOURCE \V[2]", r"TARGET \V[3]"),
            (r"\>SOURCE \V[2]\^", r"TARGET \V[2]"),
            (r"SOURCE \\", "TARGET"),
        ):
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, "E_MZ_EDGE_REPAIR_NOT_UNAMBIGUOUS"):
                    restore_missing_edge_control(source, candidate)
