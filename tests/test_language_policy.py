from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.errors import ConfigurationError
from game_l10n.language_policy import (
    language_binding,
    record_target_language_policy,
    require_target_language_policy,
)


class TargetLanguagePolicyTests(unittest.TestCase):
    def _config(self, root: Path, target: str = "ko") -> dict:
        root.mkdir(parents=True, exist_ok=True)
        config = {
            "project": {
                "source_language": "ja",
                "target_language": target,
            }
        }
        (root / "game-l10n.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        return config

    def test_missing_policy_defaults_to_locked_korean(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            work = Path(directory) / "work"
            config = self._config(work)
            policy = require_target_language_policy(work, config)
            binding = language_binding(work, config)

            self.assertEqual(policy["target_language"], "ko")
            self.assertEqual(binding["target_language"], "ko")
            self.assertEqual(binding["policy_revision"], 0)

    def test_direct_config_change_cannot_change_target_language(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            work = Path(directory) / "work"
            config = self._config(work, target="en")
            with self.assertRaisesRegex(ConfigurationError, "TARGET_LANGUAGE_DRIFT"):
                require_target_language_policy(work, config)

    def test_explicit_user_policy_changes_config_and_lock_together(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            work = Path(directory) / "work"
            self._config(work)
            with self.assertRaisesRegex(ConfigurationError, "AUTHORIZATION_REQUIRED"):
                record_target_language_policy(
                    work, target_language="en", user_requested=False
                )

            recorded = record_target_language_policy(
                work, target_language="en", user_requested=True
            )
            config = json.loads(
                (work / "game-l10n.json").read_text(encoding="utf-8")
            )
            self.assertEqual(recorded["target_language"], "en")
            self.assertEqual(config["project"]["target_language"], "en")
            self.assertEqual(
                require_target_language_policy(work, config)["target_language"],
                "en",
            )


if __name__ == "__main__":
    unittest.main()
