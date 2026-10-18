from pathlib import Path
import subprocess
import tempfile
import unittest

def private_paths(root):
    result = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=root, capture_output=True, text=True, check=True)
    denied = {"wolf-json", "batches", "audit"}
    return [path for path in result.stdout.split("\0") if any(part.casefold() in denied for part in Path(path).parts)]

class PublicTreeTests(unittest.TestCase):
    def test_sdk_has_no_engine_workspace_artifacts(self):
        self.assertEqual(private_paths(Path(__file__).resolve().parents[1]), [])
    def test_engine_private_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root/"wolf-json").mkdir()
            (root/"wolf-json/fixture.txt").write_text("fixture")
            self.assertEqual(private_paths(root), ["wolf-json/fixture.txt"])
