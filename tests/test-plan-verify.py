"""Real CLI verify pipeline: strict environment, evidence and original closeout."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"
sys.path.insert(0, str(SCRIPTS))
import role_dispatch as D
import run_evidence as E
import run_state as R
import run_tasks as T


class VerifyCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "PIPELINE.md").write_text("isolated authorized fixture")
        (self.root / "input").write_text("stable")
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        self.started = self.cli("start")
        self.receipt = self.started["receipt"]
        state = R.status(self.root)["state"]
        self.run, self.owner = state["run_id"], state["owner_token"]
        self.check = {"argv": [sys.executable, "-B", "-c",
                     "from pathlib import Path; p=Path('counter'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"],
                      "scope": ["input"], "coverage": ["unit"], "write_paths": []}
        self.plan = self.cli("plan", "--receipt", self.receipt, "--name", "plan",
                             "--json", json.dumps({"checks": {"unit": self.check}}))["plan"]

    def cli(self, *args, expected=0, env=None):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "pipeline_cli.py"), *args],
                                 cwd=self.root, env=self.env if env is None else env,
                                 capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def verify(self, name, *args, **kwargs):
        return self.cli("verify", "--receipt", self.receipt, "--plan", self.plan,
                        "--name", name, *args, **kwargs)

    def finish_args(self):
        return ("--finish", "--direct", "--stopped", "--stop-evidence", "fixture host executors observed stopped")

    def test_execution_qa_and_seal_one_process_without_environment_replay(self):
        result = self.verify("final", *self.finish_args())
        self.assertEqual(result["gate"], "PASS")
        self.assertFalse(result["results"][0]["reused"])
        self.assertTrue(result["completion"]["delivery_sealed"])
        self.assertTrue(result["completion"]["coverage_complete"])
        self.assertEqual(result["completion"]["status"], "completed")
        self.assertIsNone(R.status(self.root)["active"])
        self.assertNotIn(self.owner, json.dumps(result))

    def test_default_keeps_run_active_and_valid_evidence_is_not_executed_again(self):
        first = self.verify("first")
        second = self.verify("second")
        self.assertEqual(first["gate"], "PASS")
        self.assertTrue(second["results"][0]["reused"])
        self.assertEqual((self.root / "counter").read_text(), "1")
        self.assertEqual(R.status(self.root)["state"]["status"], "running")
        self.assertNotIn("completion", second)
        self.assertFalse((self.root / "tmp/pipeline/delivery/latest.json").exists())

    def test_real_env_including_shlvl_input_and_log_changes_force_execution(self):
        self.verify("first")
        changed = {**self.env, "SHLVL": str(int(self.env.get("SHLVL", "0")) + 1)}
        qa = self.cli("qa", "--receipt", self.receipt, "--plan", self.plan,
                      "--name", "stale-env", env=changed, expected=1)
        self.assertEqual(qa["check_failures"]["unit"], "command_or_environment_changed")
        second = self.verify("new-env", env=changed)
        self.assertFalse(second["results"][0]["reused"])
        self.assertEqual(second["gate"], "PASS")
        (self.root / "input").write_text("changed")
        stale = self.cli("qa", "--receipt", self.receipt, "--plan", self.plan,
                         "--name", "stale-input", env=changed, expected=1)
        self.assertEqual(stale["gate"], "CONCERNS")
        third = self.verify("new-input", env=changed)
        self.assertFalse(third["results"][0]["reused"])
        proof = E.read_report(self.root, self.run, third["results"][0]["report"], kind="check")["payload"]
        (self.root / proof["log_path"]).write_text("tampered")
        fourth = self.verify("new-log", env=changed)
        self.assertFalse(fourth["results"][0]["reused"])
        self.assertEqual((self.root / "counter").read_text(), "4")

    def test_later_failure_prevents_old_pass_reuse_and_stops_heavy_followup(self):
        self.verify("pass")
        proof = E.read_report(self.root, self.run, R.status(self.root)["state"]["artifacts"][1], kind="check")["payload"]
        (self.root / "failed.log").write_text("observed failure")
        E.record_check(self.root, self.run, self.owner, "newer-failure", command=proof["command"],
                       scope=proof["scope"], environment=proof["environment"], exit_code=1,
                       log_path="failed.log", coverage=["other"],
                       started_fingerprint=E.fingerprint(self.root, proof["scope"]))
        stale = self.cli("qa", "--receipt", self.receipt, "--plan", self.plan,
                         "--name", "failure-qa", expected=1)
        self.assertEqual(stale["gate"], "CONCERNS")
        result = self.verify("repair")
        self.assertFalse(result["results"][0]["reused"])
        self.assertEqual((self.root / "counter").read_text(), "2")
        failed = {**self.check, "argv": [sys.executable, "-B", "-c", "raise SystemExit(1)"]}
        heavy = {**self.check, "coverage": ["heavy"], "argv": [sys.executable, "-B", "-c",
                 "from pathlib import Path; Path('heavy-ran').touch()"]}
        self.plan = self.cli("plan", "--receipt", self.receipt, "--name", "failure-plan",
                             "--json", json.dumps({"checks": {"first": failed, "heavy": heavy}}))["plan"]
        stopped = self.verify("fail-fast", *self.finish_args(), expected=1)
        self.assertEqual(stopped["skipped_checks"], ["heavy"])
        self.assertEqual(stopped["check_failures"]["heavy"], "not_executed")
        self.assertFalse((self.root / "heavy-ran").exists())
        self.assertEqual(R.status(self.root)["state"]["status"], "running")

    def test_missing_stopping_evidence_is_rejected_before_commands(self):
        for index, args in enumerate((("--finish",), ("--finish", "--stopped"),
                                      ("--direct",), ("--stopped",), ("--stop-evidence", "observed"))):
            with self.subTest(args=args):
                self.verify("invalid" + str(index), *args, expected=2)
                self.assertFalse((self.root / "counter").exists())

    def test_existing_task_role_wait_and_release_guards_remain(self):
        for kind in ("task", "role", "wait", "release"):
            with self.subTest(kind=kind):
                state = R.status(self.root)["state"]
                path = self.root / "tmp/pipeline/runs" / (self.run + ".json")
                original = dict(state)
                if kind == "task":
                    T.start(self.root, self.run, self.owner, "active", "goal", ["observe"])
                elif kind == "role":
                    D.begin(self.root, self.run, self.owner, "developer")
                elif kind == "wait":
                    state["continuity_wait"] = {"status": "waiting"}
                    R._atomic(path, state)
                else:
                    state["release_plan"] = {"closeout": None, "phase": "pending"}
                    R._atomic(path, state)
                args = self.finish_args()
                if kind == "role":
                    args = tuple(item for item in args if item != "--direct")
                self.verify("blocked-" + kind, *args, expected=2)
                self.assertEqual(R.status(self.root)["state"]["status"], "running")
                self.assertIsNotNone(R.status(self.root)["active"])
                current = R.status(self.root)["state"]
                for key in ("tasks", "role_dispatches", "role_events", "role_execution", "continuity_wait", "release_plan"):
                    if key in original:
                        current[key] = original[key]
                    else:
                        current.pop(key, None)
                R._atomic(path, current)

    def test_observed_delegation_cannot_use_direct_finish(self):
        item = D.begin(self.root, self.run, self.owner, "developer")
        D.complete(self.root, self.run, self.owner, item["id"], executors_stopped=True,
                   stop_evidence="host rejected creation", outcome="failed")
        result = self.verify("delegated", *self.finish_args(), expected=2)
        self.assertIn("cannot be relabeled direct", result["message"])
        self.assertEqual(R.status(self.root)["state"]["role_execution"], "delegated")


if __name__ == "__main__":
    unittest.main()
