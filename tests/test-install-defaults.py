"""Installation initializes local defaults once; upgrades preserve selections."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstalledDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = self.home / ".config/pipeline-standard/models.json"

    def install(self):
        return subprocess.run(["bash", str(ROOT / "install.sh"), "--codex"], capture_output=True, text=True,
                              env=dict(os.environ, PIPELINE_INSTALL_HOME=str(self.home)))

    def test_first_install_has_defaults_without_project_configuration(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.config.read_text())
        for runtime in ("codex", "claude"):
            self.assertEqual(len(data["runtimes"][runtime]), 5)
            self.assertTrue(all(c == {"model": "inherit"} for c in data["runtimes"][runtime].values()))
        resolved = subprocess.run(["python3", str(self.home / ".codex/skills/pipeline/scripts/resolve-models.py"),
                                   "--runtime", "codex", "--project", str(self.home), "--personal", str(self.config)],
                                  capture_output=True, text=True)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertTrue(all(r["source"] == "personal" for r in json.loads(resolved.stdout)["roles"].values()))
        self.assertFalse((self.home / "pipeline.models.json").exists())

    def test_upgrade_preserves_user_choice_and_unknown_extension_bytes(self):
        self.assertEqual(self.install().returncode, 0)
        data = json.loads(self.config.read_text())
        data["runtimes"]["codex"]["developer"] = {"model": "retired-or-private-model", "effort": "high"}
        data["custom"] = {"keep": True}
        self.config.write_text(json.dumps(data))
        before = self.config.read_bytes()
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(before, self.config.read_bytes())

    def test_corrupt_or_unknown_version_preserved_and_install_reports_failure(self):
        self.config.parent.mkdir(parents=True)
        for raw in ("{broken", '{"schema_version": 99}'):
            self.config.write_text(raw)
            self.assertNotEqual(self.install().returncode, 0)
            self.assertEqual(self.config.read_text(), raw)

    def test_symlink_configuration_is_not_overwritten(self):
        self.config.parent.mkdir(parents=True)
        other = self.home / "other.json"
        other.write_text("unchanged")
        self.config.symlink_to(other)
        self.assertNotEqual(self.install().returncode, 0)
        self.assertEqual(other.read_text(), "unchanged")


if __name__ == "__main__":
    unittest.main()
