"""Pipeline-standard's own project contract exercised in an isolated fixture.

This checks local integration, not natural-language or remote model quality.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/pipeline/scripts"))
import model_config as models
import run_state as runs
import workflow_metrics as metrics


class SelfHostTests(unittest.TestCase):
    def test_configure_start_validate_pause_resume_complete(self):
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            (project / "PIPELINE.md").write_text((ROOT / "PIPELINE.md").read_text())
            document, revision = models.read_document(project / "pipeline.models.json")
            configured = models.prepare_update(document, "codex", "tester", {"model": "inherit"})
            models.save_project(project, configured, revision)
            (project / "src").mkdir()
            (project / "src/example.txt").write_text("self-host input")
            state = runs.start(project, {"stage": "verification", "pending": ["local-test"]})
            run_id, owner = state["run_id"], state["owner_token"]
            paths_before = sorted(str(p) for p in project.rglob("*"))
            self.assertEqual(runs.status(project)["state"]["run_id"], run_id)
            self.assertEqual(paths_before, sorted(str(p) for p in project.rglob("*")))
            with self.assertRaises(ValueError):
                models.save_project(project, configured, models.read_document(project / "pipeline.models.json")[1])
            query = subprocess.run([sys.executable, str(ROOT / "skills/pipeline/scripts/resolve-models.py"),
                                    "--runtime", "codex", "--project", str(project), "--personal", str(project / "no-personal.json")],
                                   capture_output=True, text=True)
            self.assertEqual(query.returncode, 0, query.stderr)
            self.assertEqual(json.loads(query.stdout)["roles"]["tester"]["preference"], {"model": "inherit"})
            fingerprint = metrics.fingerprint(project, ["src"], "isolated-local-fixture")
            output = project / "tmp/pipeline/check.log"
            output.write_text("verified fixture contents")
            proof = dict(command="fixture-check", exit_code=0, fingerprint=fingerprint, finished_at="test-clock",
                         output_path=str(output), output_sha256=hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertTrue(metrics.reusable(proof, fingerprint, "fixture-check", True))
            runs.finish(project, run_id, owner, "paused", executors_stopped=True, stop_evidence="fixture has no spawned executors")
            self.assertFalse((project / "tmp/pipeline/.active").exists())
            resumed = runs.resume(project, run_id, owner, executors_stopped=True, stop_evidence="fixture has no spawned executors")
            self.assertEqual(resumed["owner_token"], owner)
            runs.update(project, run_id, owner, {"pending": [], "stage": "complete"})
            runs.finish(project, run_id, owner, "completed", executors_stopped=True, stop_evidence="fixture has no spawned executors")
            self.assertEqual(runs.status(project, run_id)["state"]["status"], "completed")
            self.assertTrue(output.exists())
            self.assertFalse((project / ".pipeline.models.lock").exists())

    def test_legacy_run_is_preserved_in_selfhost_upgrade(self):
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            base = project / "tmp/pipeline"
            base.mkdir(parents=True)
            (base / ".active").write_text("legacy active")
            (base / "state.md").write_text("status: running")
            self.assertEqual(runs.status(project)["active"], "unrecognized")
            with self.assertRaises(ValueError):
                runs.start(project)
            self.assertEqual((base / ".active").read_text(), "legacy active")
            self.assertEqual((base / "state.md").read_text(), "status: running")


if __name__ == "__main__":
    unittest.main()
