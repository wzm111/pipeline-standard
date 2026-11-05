"""Advisory limits apply before actual dispatch without changing ordinary roles."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"
sys.path.insert(0, str(SCRIPTS))
import expert_advice as X
import role_dispatch as D
import run_checks as C
import run_state as R


class ExpertIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "input").write_text("initial")
        state = R.start(self.root)
        self.args = self.root, state["run_id"], state["owner_token"]
        self.receipt = self.root / f"tmp/pipeline/runs/{state['run_id']}/receipt.json"
        self.receipt.parent.mkdir(parents=True, exist_ok=True)
        R._atomic(self.receipt, {**{k: state[k] for k in ["schema_version", "run_id", "owner_token"]},
                                 "project_root": str(self.root)})

    def evidence(self, name):
        refs = []
        for n in range(2):
            (self.root / "input").write_text(f"{name}-{n}")
            result = C.execute_check(*self.args, f"{name}-{n}", argv=[sys.executable, "-B", "-c", "raise SystemExit(1)"],
                                     scope=["input"], coverage=[name], write_paths=[])
            refs.append(result["report"])
        return refs

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "pipeline_cli.py"), *args,
                                 "--project", str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn(self.args[2], result.stdout)
        return json.loads(result.stdout)

    def prepare(self, case):
        refs = self.evidence(case)
        value = X.prepare(*self.args, case, "repeated_failure", refs, "minimal synthetic brief", local_fix_attempted=True)
        self.assertTrue(value["eligible"], value)

    def test_prepared_case_is_not_a_dispatch_and_budget_rejects_before_writes(self):
        self.prepare("first")
        self.assertEqual(X.summary(R.status(self.root)["state"])["attempts"], 0)
        first = D.begin(*self.args, "scope_guardian", advice_case="first", native_parameters={"fork_turns": "none"})
        self.assertEqual(first["status"], "dispatching")
        before = R.status(self.root)["state"]
        with self.assertRaisesRegex(ValueError, "case_attempt_used"):
            D.begin(*self.args, "planner", advice_case="first")
        self.assertEqual(before, R.status(self.root)["state"])
        D.complete(*self.args, first["id"], outcome="failed", executors_stopped=True, stop_evidence="fixture host rejected")
        with self.assertRaisesRegex(ValueError, "case_attempt_used"):
            D.begin(*self.args, "scope_guardian", advice_case="first")
        self.prepare("second")
        D.begin(*self.args, "planner", advice_case="second")
        self.assertEqual(X.summary(R.status(self.root)["state"])["remaining"], 0)
        D.begin(*self.args, "tester")  # Independent acceptance role is unchanged.

    def test_role_and_context_rejections_do_not_consume_an_attempt(self):
        self.prepare("first")
        before = R.status(self.root)["state"]
        for role, parameters in [("developer", {}), ("scope_guardian", {"fork_turns": "all"})]:
            with self.assertRaises(ValueError):
                D.begin(*self.args, role, advice_case="first", native_parameters=parameters)
            self.assertEqual(before, R.status(self.root)["state"])

    def test_cli_preparation_and_status_do_not_echo_or_store_brief(self):
        refs = self.evidence("first")
        brief = "PRIVATE-SYNTHETIC-DIAGNOSIS"
        payload = {"case_id": "first", "trigger": "repeated_failure", "evidence": refs,
                   "brief": brief, "local_fix_attempted": True}
        result = self.cli("expert-prepare", "--receipt", str(self.receipt), "--json", json.dumps(payload))
        self.assertTrue(result["eligible"])
        self.assertNotIn(brief, json.dumps(result))
        self.assertNotIn(brief, json.dumps(R.status(self.root)["state"]))
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        status = self.cli("expert-status")
        self.assertEqual(status["attempts"], 0)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()})


if __name__ == "__main__":
    unittest.main()
