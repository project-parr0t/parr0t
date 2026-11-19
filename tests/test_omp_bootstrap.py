import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from game_l10n import omp_bootstrap as bootstrap


class OmpBootstrapTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.agent = Path(tmp) / "agent profile"
        self.entry = self.agent / "extensions" / bootstrap.ENTRY_NAME
        self.disabled = ["extension-module:unrelated", bootstrap.EXTENSION_ID]
        self.scope = None
        self.enterContext(patch.object(bootstrap.shutil, "which", return_value="test-omp"))
        self.enterContext(patch.object(bootstrap, "_omp", side_effect=self.config))

    def config(self, executable, *args):
        self.assertEqual(executable, "test-omp")
        if args == ("path",):
            return str(self.agent)
        if args == ("list", "--scope=global", "--json"):
            return json.dumps({"disabledExtensions": {"value": self.disabled}})
        if args == ("get", "disabledExtensions", "--json"):
            return json.dumps({"value": self.scope if self.scope is not None else self.disabled})
        self.assertEqual((args[:2], args[-1]), (("set", "disabledExtensions"), "--json"))
        self.disabled = json.loads(args[2])
        return ""

    def run_setup(self, *, approved=False, prompt=False):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = bootstrap.run_bootstrap(argparse.Namespace(user_approved=approved, prompt=prompt))
        self.assertEqual(rc, 0)
        return json.loads(output.getvalue())

    def test_noninteractive_needs_consent_then_installs_and_enables_only_parr0t(self):
        with patch.object(bootstrap.sys.stdin, "isatty", return_value=False):
            result = self.run_setup(prompt=True)
        self.assertEqual(result["status"], "consent_required")
        self.assertEqual(result["actions"], ["install", "enable_globally"])
        self.assertFalse(self.agent.exists())
        self.assertIn(bootstrap.EXTENSION_ID, self.disabled)
        result = self.run_setup(approved=True)
        self.assertEqual(result["status"], "configured")
        self.assertEqual(self.disabled, ["extension-module:unrelated"])
        self.assertEqual(self.entry.read_text(), f"export {{ default }} from {json.dumps(Path(result['source']).as_uri())};\n")
        self.assertNotIn("actions", self.run_setup())

    def test_decline_keeps_existing_settings(self):
        with (contextlib.redirect_stderr(io.StringIO()),
              patch.object(bootstrap.sys.stdin, "isatty", return_value=True),
              patch.object(bootstrap.sys.stderr, "isatty", return_value=True),
              patch("builtins.input", return_value="")):
            self.assertEqual(self.run_setup(prompt=True)["status"], "declined")
        self.assertFalse(self.agent.exists())
        self.assertIn(bootstrap.EXTENSION_ID, self.disabled)

    def test_scope_override_remains_and_custom_entry_is_not_replaced(self):
        self.scope = [bootstrap.EXTENSION_ID]
        self.assertEqual(self.run_setup(approved=True)["status"], "disabled_by_host_scope")
        self.assertEqual(self.scope, [bootstrap.EXTENSION_ID])
        self.entry.write_text("// custom entry\n")
        self.assertEqual(self.run_setup(approved=True)["status"], "existing_entry_conflict")
        self.assertEqual(self.entry.read_text(), "// custom entry\n")
