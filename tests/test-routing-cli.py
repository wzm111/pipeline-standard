"""End-to-end CLI routing, with fixture model identities rather than live calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts/resolve-models.py"


class RoutingCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "pipeline.models.json"
        self.personal = self.root / "personal.json"
        self.host = self.root / "host.json"
        self.project.write_text(json.dumps({"schema_version": 1, "runtimes": {"codex": {"tester": {
            "model": "base", "effort": "high", "task_routes": {"ui_validation": {"model": "light", "effort": "medium"}}}}}}))
        self.host.write_text(json.dumps({"agent_spawn": True, "model_override": True, "effort_override": True,
                                         "models_complete": True, "models": {"base": {"permission": True, "efforts": ["high"]},
                                         "light": {"permission": True, "efforts": ["medium"]}}}))

    def run_cli(self, *args, runtime="codex"):
        return subprocess.run([sys.executable, str(SCRIPT), "--runtime", runtime, "--project", str(self.root),
                               "--personal", str(self.personal), *args], capture_output=True, text=True,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))

    def test_legacy_output_does_not_implicitly_enable_routing(self):
        row = json.loads(self.run_cli("--role", "tester").stdout)
        self.assertNotIn("routing", row)
        self.assertEqual(row["preference"]["model"], "base")
        self.assertIn("task_routes", row["preference"])

    def test_route_context_is_auditable_and_selected_model_is_preflighted(self):
        before = self.project.read_bytes()
        result = self.run_cli("--role", "tester", "--task-kind", "ui_validation", "--risk", "low", "--complexity", "simple",
                              "--host-evidence", str(self.host), "--compact")
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(result.stdout)
        self.assertEqual(row["preference"], {"model": "light", "effort": "medium"})
        self.assertEqual(row["source"], "project")
        self.assertTrue(row["routing"]["applied"])
        self.assertEqual(row["launch_plan"]["parameters"]["model"], "light")
        self.assertFalse(row["launch_plan"]["capability"]["verified"])
        self.assertEqual(self.project.read_bytes(), before)

    def test_high_or_unknown_context_keeps_base(self):
        for flags in ([], ["--risk", "high", "--complexity", "simple"], ["--risk", "low", "--complexity", "complex"]):
            result = self.run_cli("--role", "tester", "--task-kind", "ui_validation", *flags)
            self.assertEqual(result.returncode, 0, result.stderr)
            row = json.loads(result.stdout)
            self.assertFalse(row["routing"]["applied"])
            self.assertEqual(row["preference"]["model"], "base")

    def test_invalid_task_context_and_mode_combinations_fail(self):
        for flags in (["--task-kind", "ui_validation"], ["--role", "tester", "--risk", "low"],
                      ["--role", "tester", "--task-kind", "scoped_fix"],
                      ["--read", "--role", "tester", "--task-kind", "ui_validation"],
                      ["--role", "tester", "--task-kind", "not-real"]):
            result = self.run_cli(*flags)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(result.stdout)
            self.assertNotIn("Traceback", result.stderr)

    def test_routed_model_failure_is_not_hidden_by_available_base(self):
        host = json.loads(self.host.read_text())
        host["models"].pop("light")
        self.host.write_text(json.dumps(host))
        result = self.run_cli("--role", "tester", "--task-kind", "ui_validation", "--risk", "low", "--complexity", "simple",
                              "--host-evidence", str(self.host))
        self.assertEqual(result.returncode, 1)
        self.assertIn("unavailable", result.stderr)

    def test_temporary_fixed_override_suppresses_lower_routes(self):
        result = self.run_cli("--role", "tester", "--task-kind", "ui_validation", "--risk", "low", "--complexity", "simple",
                              "--temporary", '{"tester":{"model":"inherit"}}')
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(result.stdout)
        self.assertEqual(row["source"], "temporary")
        self.assertEqual(row["preference"], {"model": "inherit"})
        self.assertFalse(row["routing"]["applied"])

    def test_claude_routes_remain_runtime_specific(self):
        self.project.write_text(json.dumps({"schema_version": 1, "runtimes": {"claude": {"tester": {
            "model": "claude-base", "task_routes": {"ui_validation": {"model": "claude-light", "effort": "medium"}}}}}}))
        self.host.write_text(json.dumps({"agent_spawn": True, "model_override": True, "effort_override": True,
                                         "role_frontmatter": True, "role_inherit": True,
                                         "models": {"claude-light": {"permission": True, "efforts": ["medium"]}}}))
        result = self.run_cli("--role", "tester", "--task-kind", "ui_validation", "--risk", "low", "--complexity", "simple",
                              "--host-evidence", str(self.host), runtime="claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(result.stdout)
        self.assertEqual(row["launch_plan"]["parameters"], {"model": "claude-light", "effort": "medium"})
        self.assertEqual(row["launch_plan"]["application"], "claude_role_frontmatter")


if __name__ == "__main__":
    unittest.main()
