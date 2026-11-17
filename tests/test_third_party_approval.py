from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from game_l10n.errors import ThirdPartyApprovalError
from game_l10n.third_party_approval import create_third_party_approval, require_third_party_approval
from game_l10n.workspace import initialize_workspace


class ThirdPartyApprovalTests(unittest.TestCase):
    def _workspace(self, root: Path) -> Path:
        sdk = root / "sdk"
        game = root / "fixture-game"
        work = root / "work"
        sdk.mkdir()
        game.mkdir()
        initialize_workspace(work, sdk, git_choice="disabled", game_roots=[game])
        return work

    def _approval(self, work: Path, **overrides: object) -> tuple[Path, dict[str, object]]:
        values: dict[str, object] = {
            "action": "clone",
            "name": "Fixture Tool",
            "publisher": "Fixture Publisher",
            "source": "https://example.invalid/fixture/tool",
            "version": "1.2.3",
            "digest": "git:" + "a" * 40,
            "license_name": "MIT",
            "purpose": "Exercise the synthetic approval contract.",
            "data_scope": "No project or user data is transmitted.",
            "risk_disclosed": True,
            "user_confirmed": True,
        }
        values.update(overrides)
        return create_third_party_approval(work, **values)  # type: ignore[arg-type]

    def test_exact_explicit_approval_is_one_shot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = self._workspace(Path(directory))
            path, _ = self._approval(work)
            identity = {
                "action": "clone",
                "name": "Fixture Tool",
                "publisher": "Fixture Publisher",
                "source": "https://example.invalid/fixture/tool",
                "version": "1.2.3",
                "digest": "git:" + "a" * 40,
            }
            require_third_party_approval(work, path, **identity, consume=True)
            with self.assertRaisesRegex(ThirdPartyApprovalError, "E_THIRD_PARTY_APPROVAL_CONSUMED"):
                require_third_party_approval(work, path, **identity, consume=True)

    def test_missing_consent_or_risk_disclosure_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = self._workspace(Path(directory))
            with self.assertRaisesRegex(ThirdPartyApprovalError, "E_THIRD_PARTY_USER_CONFIRMATION_REQUIRED"):
                self._approval(work, user_confirmed=False)
            with self.assertRaisesRegex(ThirdPartyApprovalError, "E_THIRD_PARTY_RISK_DISCLOSURE_REQUIRED"):
                self._approval(work, risk_disclosed=False)

    def test_mutable_version_and_unpinned_code_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = self._workspace(Path(directory))
            with self.assertRaisesRegex(ThirdPartyApprovalError, "E_THIRD_PARTY_VERSION_NOT_EXACT"):
                self._approval(work, version="latest")
            with self.assertRaisesRegex(ThirdPartyApprovalError, "E_THIRD_PARTY_DIGEST_NOT_IMMUTABLE"):
                self._approval(work, digest="not-applicable")

    def test_identity_or_action_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = self._workspace(Path(directory))
            path, _ = self._approval(work)
            with self.assertRaisesRegex(ThirdPartyApprovalError, "E_THIRD_PARTY_APPROVAL_NAME_MISMATCH"):
                require_third_party_approval(
                    work,
                    path,
                    action="clone",
                    name="Different Tool",
                    publisher="Fixture Publisher",
                    source="https://example.invalid/fixture/tool",
                    version="1.2.3",
                    digest="git:" + "a" * 40,
                )

    def test_workspace_contract_denies_unapproved_third_party_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = self._workspace(Path(directory))
            contract = (work / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("skills/third-party-consent/SKILL.md", contract)
            self.assertIn("explicit user approval", contract)


if __name__ == "__main__":
    unittest.main()
