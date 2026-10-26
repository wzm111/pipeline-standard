"""Resolver tests run entirely in temporary directories, without model calls."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("models", Path(__file__).resolve().parents[1] / "skills/pipeline/scripts/resolve-models.py")
MODELS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODELS)


class ModelConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.personal = Path(self.temp.name) / "personal.json"
        self.project = Path(self.temp.name) / "project.json"

    def save(self, path, runtimes):
        path.write_text(json.dumps({"schema_version": 1, "runtimes": runtimes}), encoding="utf-8")

    def test_missing_preserves_runtime_default(self):
        self.assertEqual(MODELS.resolve("codex", self.personal, self.project)["roles"], {})
        self.assertFalse(self.project.exists())

    def test_project_replaces_entire_role_without_stale_effort(self):
        self.save(self.personal, {"codex": {"developer": {"model": "old", "effort": "high"}, "tester": {"model": "test"}}})
        self.save(self.project, {"codex": {"developer": {"model": "new"}}})
        result = MODELS.resolve("codex", self.personal, self.project)["roles"]
        self.assertEqual(result["developer"]["preference"], {"model": "new"})
        self.assertEqual(result["tester"]["source"], "personal")

    def test_inherit_masks_personal_and_providers_are_isolated(self):
        self.save(self.personal, {"codex": {"tester": {"model": "custom"}}})
        self.save(self.project, {"codex": {"tester": {"model": "inherit"}}, "claude": {"tester": {"model": "claude-choice"}}})
        self.assertEqual(MODELS.resolve("codex", self.personal, self.project)["roles"]["tester"]["preference"]["model"], "inherit")
        self.assertEqual(MODELS.resolve("claude", self.personal, self.project)["roles"]["tester"]["preference"]["model"], "claude-choice")

    def test_invalid_config_is_not_overwritten(self):
        for content in ('{broken', '{"schema_version": 2}', '{"schema_version": true}'):
            self.project.write_text(content)
            with self.assertRaises(ValueError):
                MODELS.resolve("codex", self.personal, self.project)
            self.assertEqual(self.project.read_text(), content)

    def test_invalid_role_settings(self):
        for config in ({"model": ""}, {"model": "inherit", "effort": "high"}, {"model": "m", "fallback": "silent"}, {"model": "m", "effort": None}):
            self.save(self.project, {"codex": {"developer": config}})
            with self.assertRaises(ValueError):
                MODELS.resolve("codex", self.personal, self.project)


if __name__ == "__main__":
    unittest.main()
