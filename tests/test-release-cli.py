"""Thin release CLI exercises original gates with isolated real child commands."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"
sys.path.insert(0, str(SCRIPTS))
import run_state as R
import run_evidence as E


class ReleaseCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "PIPELINE.md").write_text("isolated authorized commands")
        (self.root / "source").write_text("v1")
        state = self.call("pipeline_cli.py", "start", "--review-risk", "low", "--review-reason", "fixture scoped change")
        self.receipt = state["receipt"]
        self.call("pipeline_cli.py", "policy-ack", "--receipt", self.receipt,
                  "--revision", state["policy"]["revision"])
        self.run_id = state["run_id"]

    def call(self, script, *args, expected=0):
        result = subprocess.run([sys.executable, str(SCRIPTS / script), *args], cwd=self.root,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def cli(self, action, declaration=None, expected=0):
        args = [action, "--receipt", self.receipt]
        if declaration is not None:
            args += ["--json", json.dumps(declaration)]
        return self.call("release_checks.py", *args, expected=expected)

    def phase(self, name, phase, **extra):
        return {"name": name, "phase": phase, "argv": [sys.executable, "-c", "print('fixture')"],
                "scope": ["source"], "timeout": 10, **extra}

    def ready(self):
        self.cli("prepare", {})
        self.cli("execute", self.phase("preflight", "preflight"))
        self.cli("execute", self.phase("compat", "compatibility"))

    def build(self):
        return self.cli("execute", self.phase("build", "build", artifact_paths=["artifact"],
                        argv=[sys.executable, "-c", "from pathlib import Path; Path('artifact').write_text('v1')"]))

    def accepted(self):
        self.ready()
        self.build()
        self.cli("execute", self.phase("activate", "activate"))
        self.cli("execute", self.phase("smoke", "smoke", coverage=["release_identity"]))
        return self.cli("closeout", {})

    def test_lifecycle_uses_same_candidate_and_read_only_safe_status(self):
        self.ready()
        self.build()
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        status = self.cli("status")
        self.assertTrue(status["candidate_id"])
        self.assertEqual(before, {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()})
        private = R.status(self.root, self.run_id)["state"]["owner_token"]
        self.assertNotIn(private, json.dumps(status))
        self.assertFalse({"argv", "environment", "release_plan", "owner_token"} & set(status))
        self.cli("closeout", {}, expected=2)
        self.cli("execute", self.phase("activate", "activate"))
        self.cli("execute", self.phase("smoke", "smoke", coverage=["release_identity"]))
        self.assertTrue(self.cli("status")["activated"])
        first = self.cli("closeout", {})
        self.assertFalse(first["reused"])
        self.assertTrue(self.cli("closeout", {})["reused"])
        finished = self.call("pipeline_cli.py", "finish", "--receipt", self.receipt, "--stopped",
                             "--direct", "--stop-evidence", "isolated fixture children exited")
        self.assertEqual(finished["status"], "completed")

    def test_unknown_fields_and_flags_never_start_child(self):
        declaration = self.phase("bad", "preflight", argv=[sys.executable, "-c", "from pathlib import Path; Path('executed').touch()"])
        self.cli("execute", {**declaration, "retry": True}, expected=2)
        result = subprocess.run([sys.executable, str(SCRIPTS / "release_checks.py"), "execute",
                                 "--receipt", self.receipt, "--json", json.dumps(declaration), "--retry"],
                                cwd=self.root, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.root / "executed").exists())
        self.assertNotIn("release_plan", R.status(self.root, self.run_id)["state"])

    def test_drift_and_original_prechecks_reject_activation_before_child(self):
        self.cli("execute", self.phase("early", "activate"), expected=2)
        self.ready()
        self.build()
        (self.root / "artifact").write_text("tampered")
        self.cli("execute", self.phase("changed", "activate", argv=[sys.executable, "-c", "from pathlib import Path; Path('activated').touch()"]), expected=2)
        self.assertIsNone(self.cli("status")["candidate_id"])
        self.assertFalse((self.root / "activated").exists())

    def test_private_receipt_permissions_remain_required(self):
        Path(self.receipt).chmod(0o644)
        self.cli("prepare", {}, expected=2)
        self.assertNotIn("release_plan", R.status(self.root, self.run_id)["state"])

    def test_forged_and_stale_release_packets_cannot_grant_pass(self):
        report = self.accepted()
        packet = E.read_report(self.root, self.run_id, report["report"], kind="qa")["payload"]
        for name, forged in (("candidate", {**packet, "candidate_id": "forged"}),
                             ("required", {**packet, "required_checks": []})):
            self.call("pipeline_cli.py", "report", "--receipt", self.receipt, "--kind", "qa",
                      "--name", name, "--json", json.dumps(forged), expected=2)
        (self.root / "source").write_text("v2")
        self.call("pipeline_cli.py", "report", "--receipt", self.receipt, "--kind", "qa",
                  "--name", "stale", "--json", json.dumps(packet), expected=2)
        self.cli("closeout", {}, expected=2)

    def test_missing_source_plan_is_not_erased_by_closeout(self):
        self.accepted()
        self.call("pipeline_cli.py", "plan", "--receipt", self.receipt, "--name", "source-plan",
                  "--json", json.dumps({"checks": {"source-test": {
                      "argv": [sys.executable, "-c", "print('source-test')"], "scope": ["source"],
                      "coverage": ["source-required"], "timeout": 10}}}))
        self.cli("closeout", {}, expected=2)

    def test_source_plan_can_join_release_without_erasing_its_requirements(self):
        plan = self.call("pipeline_cli.py", "plan", "--receipt", self.receipt, "--name", "source-plan",
                  "--json", json.dumps({"checks": {"source-test": {
                      "argv": [sys.executable, "-c", "print('source-test')"], "scope": ["source"],
                      "coverage": ["source-required"], "timeout": 10}}}))
        self.call("pipeline_cli.py", "run", "--receipt", self.receipt, "--plan", plan["plan"],
                  "--id", "source-test", "--name", "source-r1")
        report = self.accepted()
        packet = E.read_report(self.root, self.run_id, report["report"], kind="qa")["payload"]
        self.assertIn("source-required", packet["required_checks"])
        self.assertEqual(packet["machine_validation"]["mode"], "release")

    def test_current_environment_and_original_log_still_invalidate_closeout(self):
        self.accepted()
        with patch.dict(os.environ, {"PIPELINE_RELEASE_FIXTURE_ENV": "changed"}):
            self.cli("closeout", {}, expected=2)
        self.assertTrue(self.cli("closeout", {})["reused"])
        state = R.status(self.root, self.run_id)["state"]
        build = E.read_report(self.root, self.run_id, state["release_plan"]["candidate"]["check"]["path"], kind="check")["payload"]
        (self.root / build["log_path"]).write_text("changed log")
        self.cli("closeout", {}, expected=2)

    def test_latest_failure_is_not_erased_by_closeout(self):
        self.accepted()
        self.cli("execute", self.phase("smoke-failed", "smoke", coverage=["release_identity"],
                 argv=[sys.executable, "-c", "raise SystemExit(1)"]), expected=1)
        self.cli("closeout", {}, expected=2)

    def test_source_business_concerns_cannot_be_replaced_with_release_pass(self):
        self.accepted()
        self.call("pipeline_cli.py", "report", "--receipt", self.receipt, "--kind", "qa",
                  "--name", "business-concerns", "--json", json.dumps({"gate": "CONCERNS", "summary": "business acceptance remains unresolved"}))
        self.cli("closeout", {}, expected=2)

    def test_prior_task_release_is_not_current_task_acceptance(self):
        self.accepted()
        self.call("pipeline_cli.py", "task-start", "--review-risk", "low", "--review-reason", "fixture scoped change", "--receipt", self.receipt,
                  "--task-id", "new-task", "--goal", "new fixture task", "--accept", "current evidence")
        self.cli("closeout", {}, expected=2)


if __name__ == "__main__":
    unittest.main()
