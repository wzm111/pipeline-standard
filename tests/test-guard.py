import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "hooks/pipeline-guard.sh"


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "tmp/pipeline").mkdir(parents=True)
        (self.root / "tmp/pipeline/.active").write_text("active")
        (self.root / "src").mkdir()
        self.contract("allow-write", "src/\nREADME.md\ntmp/")

    def contract(self, mode, entries):
        (self.root / "PIPELINE.md").write_text(f"<!-- pipeline-guard:{mode}\n{entries}\n-->\n")

    def check_path(self, path):
        payload = {"cwd": str(self.root), "tool_name": "Edit", "tool_input": {"file_path": str(path)}}
        return subprocess.run(["bash", str(SCRIPT)], input=json.dumps(payload), capture_output=True, text=True).returncode

    def test_exact_file_directory_and_sibling(self):
        self.assertEqual(self.check_path(self.root / "README.md"), 0)
        self.assertEqual(self.check_path(self.root / "src/a.py"), 0)
        self.assertEqual(self.check_path(self.root / "src2/a.py"), 2)

    def test_traversal_and_external_symlink(self):
        self.assertEqual(self.check_path("src/../secret.txt"), 2)
        self.assertEqual(self.check_path("src/../../outside.txt"), 2)
        with tempfile.TemporaryDirectory() as outside:
            (self.root / "src/linked").symlink_to(outside, target_is_directory=True)
            self.assertEqual(self.check_path("src/linked/file.txt"), 2)

    def test_internal_symlink_uses_actual_target(self):
        (self.root / "alias").symlink_to(self.root / "src", target_is_directory=True)
        self.assertEqual(self.check_path("alias/file.txt"), 0)

    def test_missing_contract_or_guard_fails_closed_when_active(self):
        (self.root / "PIPELINE.md").unlink()
        self.assertEqual(self.check_path("src/a.py"), 2)
        (self.root / "PIPELINE.md").write_text("no guard")
        self.assertEqual(self.check_path("src/a.py"), 2)

    def test_no_active_is_normal_session(self):
        (self.root / "tmp/pipeline/.active").unlink()
        self.assertEqual(self.check_path("other/file.txt"), 0)

    def test_deny_matches_exact_file(self):
        self.contract("deny-write", "secret.txt\nprivate/")
        self.assertEqual(self.check_path("secret.txt"), 2)
        self.assertEqual(self.check_path("private/a"), 2)
        self.assertEqual(self.check_path("src/a"), 0)

    def test_deny_directory_and_file_symlink_aliases(self):
        (self.root / "real-private").mkdir()
        (self.root / "private").symlink_to(self.root / "real-private", target_is_directory=True)
        (self.root / "other-alias").symlink_to(self.root / "real-private", target_is_directory=True)
        self.contract("deny-write", "private/")
        for path in ("private/secret", "real-private/secret", "other-alias/secret"):
            self.assertEqual(self.check_path(path), 2)
        (self.root / "real.txt").write_text("secret")
        (self.root / "secret.txt").symlink_to(self.root / "real.txt")
        self.contract("deny-write", "secret.txt")
        self.assertEqual(self.check_path("real.txt"), 2)
        self.assertEqual(self.check_path("secret.txt"), 2)

    def test_deny_lexical_subpath_even_if_link_points_to_allowed_dir(self):
        (self.root / "private").mkdir()
        (self.root / "private/link").symlink_to(self.root / "src", target_is_directory=True)
        self.contract("deny-write", "private/")
        self.assertEqual(self.check_path("private/link/file"), 2)
        self.assertEqual(self.check_path("src/file"), 0)


if __name__ == "__main__":
    unittest.main()
