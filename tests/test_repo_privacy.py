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

    def test_unrelated_sdk_document_is_not_subject_to_protected_fixture_rules(self) -> None:
        violations = content_violations(Path("docs/provider-protocol.md"), chr(0x3042))
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
