"""Resource discovery follows real installed links and reports missing resources."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"
sys.path.insert(0, str(SCRIPTS))
import resource_paths as P


class ResourcePathsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def fixture(self, root):
        files = ["skills/pipeline/SKILL.md", "skills/pipeline/scripts/pipeline_cli.py",
                 "skills/pipeline/scripts/release_checks.py", "init-project.sh",
                 "templates/PIPELINE.md", "templates/single-agent-pipeline.md", "hooks/pipeline-guard.sh"]
        files += [f"agents/pipeline-{role}.md" for role in ("developer", "planner", "tester", "releaser", "scope-guardian")]
        for file in files:
            path = root / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(file)
        return root / "skills/pipeline/scripts/pipeline_cli.py"

    def snapshot(self):
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}

    def test_source_distribution_returns_actual_external_resources_without_writes(self):
        runner = self.fixture(self.root / "source")
        project = self.root / "project"
        project.mkdir()
        before = self.snapshot()
        result = P.discover(runner, project=project)
        self.assertEqual(result["unavailable"], [])
        self.assertEqual(result["project_root"], str(project))
        self.assertEqual(Path(result["paths"]["project_template"]), runner.parents[3] / "templates/PIPELINE.md")
        self.assertTrue(all(Path(p).exists() and Path(p).is_absolute() for p in result["paths"].values()))
        self.assertEqual(before, self.snapshot())

    def test_installed_claude_and_codex_links_resolve_original_distribution(self):
        runner = self.fixture(self.root / "distribution")
        for target in (".codex", ".claude"):
            installed = self.root / "home" / target / "skills/pipeline"
            installed.parent.mkdir(parents=True)
            installed.symlink_to(runner.parents[1], target_is_directory=True)
            discovered = P.discover(installed / "scripts/pipeline_cli.py")
            self.assertEqual(discovered, P.discover(runner))

    def test_copied_skill_does_not_invent_external_resources_from_project(self):
        installed = self.root / "home/.codex/skills/pipeline/scripts/pipeline_cli.py"
        installed.parent.mkdir(parents=True)
        installed.write_text("copied runner")
        (installed.parents[1] / "SKILL.md").write_text("copied skill")
        unrelated = self.fixture(self.root / "project")
        result = P.discover(installed, project=unrelated.parents[3])
        self.assertEqual(set(result["paths"]), {"runner", "skill"})
        self.assertIn("project_template", result["unavailable"])
        self.assertIn("developer", result["unavailable"])

    def test_broken_links_are_unavailable_and_existing_installed_role_link_is_resolved(self):
        runner = self.fixture(self.root / "distribution")
        template = runner.parents[3] / "templates/PIPELINE.md"
        template.unlink()
        template.symlink_to(self.root / "missing")
        role = runner.parents[3] / "agents/pipeline-tester.md"
        actual = self.root / "tester.md"
        actual.write_text("actual role")
        role.unlink()
        role.symlink_to(actual)
        result = P.discover(runner)
        self.assertIn("project_template", result["unavailable"])
        self.assertEqual(result["paths"]["tester"], str(actual))
        with self.assertRaises((ValueError, OSError)):
            P.discover(runner, project=self.root / "absent")

    def invoke(self, runner, *args, expected=0):
        result = subprocess.run([sys.executable, str(runner), *args], cwd=self.root,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout) if expected == 0 else result

    def test_cli_compatibility_project_option_and_linked_installation_are_read_only(self):
        runner = SCRIPTS / "pipeline_cli.py"
        before = self.snapshot()
        original = self.invoke(runner, "paths")
        explicit = self.invoke(runner, "paths", "--project", str(self.root))
        self.assertEqual(original["paths"], explicit["paths"])
        self.assertTrue({"skill", "runner", "developer"} <= set(original["paths"]))
        self.assertEqual(explicit["project_root"], str(self.root))
        self.assertEqual(before, self.snapshot())
        installed = self.root / ".codex/skills/pipeline"
        installed.parent.mkdir(parents=True)
        installed.symlink_to(SCRIPTS.parent, target_is_directory=True)
        self.assertEqual(original, self.invoke(installed / "scripts/pipeline_cli.py", "paths"))

    def test_actual_copied_cli_reports_missing_resources_without_fallback(self):
        skill = self.root / "standalone/skills/pipeline"
        shutil.copytree(SCRIPTS, skill / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(SCRIPTS.parent / "SKILL.md", skill / "SKILL.md")
        before = self.snapshot()
        result = self.invoke(skill / "scripts/pipeline_cli.py", "paths", "--project", str(self.root))
        self.assertIn("project_template", result["unavailable"])
        self.assertIn("developer", result["unavailable"])
        self.assertNotIn("init_project", result["paths"])
        self.assertTrue(Path(result["paths"]["release_runner"]).is_file())
        self.assertEqual(before, self.snapshot())

    def test_unknown_options_and_static_preflight_never_execute_project_commands(self):
        runner = SCRIPTS / "pipeline_cli.py"
        before = self.snapshot()
        self.invoke(runner, "paths", "--project", str(self.root), "--unknown", expected=2)
        self.invoke(runner, "paths", "--proj", str(self.root), expected=2)
        command = [sys.executable, "-c", "from pathlib import Path; Path('unexpected').touch()"]
        self.invoke(runner, "preflight", "--project", str(self.root), "--unknown", "--", *command, expected=2)
        result = self.invoke(runner, "preflight", "--project", str(self.root), "--", *command)
        self.assertTrue(result["available"])
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
