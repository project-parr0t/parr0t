from __future__ import annotations

import unittest
from pathlib import Path

from scripts.check_repo_privacy import content_violations


class RepoPrivacyTests(unittest.TestCase):
    def test_faq_rejects_literal_game_language_content(self) -> None:
        text = "SOURCE_" + chr(0x3042)
        violations = content_violations(Path("docs/faq.md"), text)
        self.assertEqual(len(violations), 1)

    def test_faq_rejects_unmarked_resource_paths(self) -> None:
        resource_path = "/".join(("project-data", "assets", "sample.png"))
        violations = content_violations(Path("docs/faq.md"), resource_path)
        self.assertEqual(len(violations), 1)

    def test_faq_accepts_clearly_synthetic_resource_paths(self) -> None:
        resource_path = "/".join(("fixture", "assets", "synthetic-image.png"))
        violations = content_violations(Path("docs/faq.md"), resource_path)
        self.assertEqual(violations, [])

    def test_core_module_is_not_subject_to_literal_fixture_rules(self) -> None:
        violations = content_violations(Path("src/game_l10n/providers.py"), chr(0x3042))
        self.assertEqual(violations, [])

    def test_all_sdk_documents_are_provenance_protected(self) -> None:
        violations = content_violations(Path("docs/engine-notes.md"), chr(0x3042))
        self.assertEqual(len(violations), 1)


if __name__ == "__main__":
    unittest.main()
