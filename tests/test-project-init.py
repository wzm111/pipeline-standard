import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "init-project.sh"


class InitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_init(self, mode="--claude"):
        return subprocess.run(["bash", str(SCRIPT), mode, str(self.root)], capture_output=True, text=True)

    def test_existing_matcher_and_settings_preserved_idempotent(self):
        (self.root / ".claude").mkdir()
        path = self.root / ".claude/settings.json"
        existing = {"permissions": {"deny": ["Bash(rm *)"]}, "hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "existing-command"}]}]}}
        path.write_text(json.dumps(existing))
        (self.root / "PIPELINE.md").write_text("user contract")
        result = self.run_init()
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(path.read_text())
        self.assertEqual(data["permissions"], existing["permissions"])
        self.assertEqual(data["hooks"]["PreToolUse"][0], existing["hooks"]["PreToolUse"][0])
        self.assertEqual(len(data["hooks"]["PreToolUse"]), 3)
        before = path.read_bytes()
        self.assertEqual(self.run_init().returncode, 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual((self.root / "PIPELINE.md").read_text(), "user contract")

    def test_corrupt_settings_not_overwritten(self):
        (self.root / ".claude").mkdir()
        path = self.root / ".claude/settings.json"
        path.write_text("{bad")
        self.assertNotEqual(self.run_init().returncode, 0)
        self.assertEqual(path.read_text(), "{bad")

    def test_codex_does_not_create_claude_settings(self):
        self.assertEqual(self.run_init("--codex").returncode, 0)
        self.assertFalse((self.root / ".claude").exists())

    def test_codex_ignores_unrelated_claude_symlink(self):
        target = self.root / "shared-claude"
        target.mkdir()
        (target / "settings.json").write_text("{bad but unrelated}")
        (self.root / ".claude").symlink_to(target, target_is_directory=True)
        self.assertEqual(self.run_init("--codex").returncode, 0)
        self.assertEqual((target / "settings.json").read_text(), "{bad but unrelated}")
        self.assertNotEqual(self.run_init("--claude").returncode, 0)

    def test_symlink_control_rejected(self):
        target = self.root / "outside"
        target.write_text("preserve")
        (self.root / ".gitignore").symlink_to(target)
        self.assertNotEqual(self.run_init().returncode, 0)
        self.assertEqual(target.read_text(), "preserve")


if __name__ == "__main__":
    unittest.main()
