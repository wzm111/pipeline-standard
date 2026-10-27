"""Exercise the real CLI in isolated projects, never invoke models."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts/resolve-models.py"


class RoleHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "pipeline.models.json"
        self.host = self.root / "host.json"
        self.host.write_text(json.dumps({"agent_spawn": True, "model_override": True, "effort_override": True,
                                         "models": {"test-model": {"permission": True, "efforts": ["medium"]}}}))
        self.project.write_text(json.dumps({"schema_version": 1, "runtimes": {"codex": {
            "tester": {"model": "test-model", "effort": "medium"}}}}))

    def run_cli(self, *args, runtime="codex"):
        return subprocess.run([sys.executable, str(SCRIPT), "--runtime", runtime, "--project", str(self.root),
                               "--personal", str(self.root / "no-personal.json"), *args],
                              capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))

    def test_role_compact_preserves_provenance_and_legacy_output(self):
        original = self.project.read_bytes()
        full = self.run_cli()
        selected = self.run_cli("--role", "tester", "--compact")
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        data = json.loads(selected.stdout)
        self.assertEqual({k: data[k] for k in ("source", "path", "preference")}, json.loads(full.stdout)["roles"]["tester"])
        self.assertEqual(data["role"], "tester")
        self.assertNotIn("roles", data)
        self.assertEqual(len(selected.stdout.splitlines()), 1)
        self.assertLess(len(selected.stdout), len(full.stdout))
        self.assertEqual(self.project.read_bytes(), original)
        self.assertFalse((self.root / "tmp").exists())

    def test_codex_plan_uses_verified_preflight_not_actual_model_claim(self):
        result = self.run_cli("--role", "tester", "--host-evidence", str(self.host))
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)["launch_plan"]
        self.assertEqual(plan["parameters"], {"model": "test-model", "reasoning_effort": "medium", "fork_turns": "none"})
        self.assertFalse(plan["capability"]["verified"])
        self.assertFalse(plan["fallback"])
        self.assertNotIn("actual_model", plan)

    def test_unknown_permission_and_unavailable_model_do_not_silently_fallback(self):
        for host in ({"agent_spawn": True, "model_override": True},
                     {"agent_spawn": True, "model_override": True, "models": {}, "models_complete": True}):
            self.host.write_text(json.dumps(host))
            result = self.run_cli("--role", "tester", "--host-evidence", str(self.host))
            self.assertEqual(result.returncode, 1)
            self.assertFalse(result.stdout)
            self.assertNotIn("Traceback", result.stderr)

    def test_explicit_fallback_is_visible(self):
        doc = json.loads(self.project.read_text())
        doc["runtimes"]["codex"]["tester"]["fallback"] = "inherit"
        self.project.write_text(json.dumps(doc))
        self.host.write_text('{"agent_spawn": true}')
        result = self.run_cli("--role", "tester", "--host-evidence", str(self.host))
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)["launch_plan"]
        self.assertTrue(plan["fallback"])
        self.assertEqual(plan["parameters"], {})

    def test_claude_plan_preserves_explicit_inherit_semantics(self):
        self.host.write_text('{"agent_spawn":true,"role_frontmatter":true,"role_inherit":true}')
        result = self.run_cli("--role", "tester", "--host-evidence", str(self.host), runtime="claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)["launch_plan"]
        self.assertEqual(plan["application"], "claude_role_frontmatter")
        self.assertEqual(plan["parameters"], {"model": "inherit", "effort": None})

    def test_bad_host_inputs_and_conflicting_modes_fail_without_mutation(self):
        original = self.project.read_bytes()
        for content in ("[]", "null", "{broken"):
            self.host.write_text(content)
            self.assertEqual(self.run_cli("--role", "tester", "--host-evidence", str(self.host)).returncode, 1)
        for extra in (("--host-evidence", str(self.host)), ("--read", "--role", "tester")):
            result = self.run_cli(*extra)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.project.read_bytes(), original)

    def test_role_filter_does_not_hide_corrupt_config(self):
        self.project.write_text("{broken")
        result = self.run_cli("--role", "planner", "--compact")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(result.stdout)


if __name__ == "__main__":
    unittest.main()
