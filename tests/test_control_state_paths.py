"""Malformed admission paths must not reset control or lifecycle state."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from game_l10n.control_plane import (
    CONTROL_ENTRY_PATH,
    CONTROL_STATE_PATH,
    require_control_plane_entry,
    require_control_plane_ready,
)
from game_l10n.errors import ConfigurationError
from game_l10n.framework_session import FRAMEWORK_SESSION_PATH, require_localization_mode


class ControlStatePathTests(unittest.TestCase):
    cases = (
        (CONTROL_STATE_PATH, require_control_plane_ready, "E_CONTROL_PLANE_STATE"),
        (CONTROL_ENTRY_PATH, require_control_plane_entry, "E_CONTROL_PLANE_ENTRY_STATE"),
        (FRAMEWORK_SESSION_PATH, require_localization_mode, "E_FRAMEWORK_SESSION_INVALID"),
    )

    def test_state_directory_never_grants_admission(self) -> None:
        for relative, admit, issue in self.cases:
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / relative).mkdir(parents=True)
                with self.assertRaisesRegex(ConfigurationError, issue):
                    admit(root)

    def test_dangling_link_never_grants_admission(self) -> None:
        for relative, admit, issue in self.cases:
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                link = root / relative
                link.parent.mkdir(parents=True)
                target = root / "synthetic-missing-state"
                if os.name == "nt":
                    # Junctions need no Windows symlink privilege. Both APIs
                    # see ENOENT when following one whose target disappeared.
                    import _winapi

                    target.mkdir()
                    _winapi.CreateJunction(str(target), str(link))
                    target.rmdir()
                else:
                    link.symlink_to(target)
                with self.assertRaisesRegex(ConfigurationError, issue):
                    admit(root)

    def test_denied_state_lookup_never_grants_admission(self) -> None:
        original = Path.lstat
        for relative, admit, issue in self.cases:
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state_path = root / relative

                def denied(path: Path, *args, **kwargs):
                    if path == state_path:
                        raise PermissionError("SYNTHETIC_PRIVATE_PATH")
                    return original(path, *args, **kwargs)

                with patch.object(Path, "lstat", denied):
                    with self.assertRaisesRegex(ConfigurationError, issue) as caught:
                        admit(root)
                self.assertNotIn("SYNTHETIC_PRIVATE_PATH", str(caught.exception))

    def test_state_disappearing_after_lookup_never_grants_admission(self) -> None:
        original = Path.read_text
        for relative, admit, issue in self.cases:
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state_path = root / relative
                state_path.parent.mkdir(parents=True)
                state_path.write_text("{}", encoding="utf-8")

                def disappeared(path: Path, *args, **kwargs):
                    if path == state_path:
                        state_path.unlink()
                    return original(path, *args, **kwargs)

                with patch.object(Path, "read_text", disappeared):
                    with self.assertRaisesRegex(ConfigurationError, issue):
                        admit(root)


if __name__ == "__main__":
    unittest.main()
