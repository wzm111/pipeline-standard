"""Coordinator decisions reach actual dispatch, task and compact progress entrypoints."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"
sys.path.insert(0, str(SCRIPTS))
import role_dispatch as D
import run_policy as P
import run_progress as G
import run_state as R
import run_tasks as T
import work_allocation as W


class AllocationIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "PIPELINE.md").write_text("isolated fixture contract")

    def start(self):
        state = R.start(self.root, managed=True)
        self.args = self.root, state["run_id"], state["owner_token"]
        P.acknowledge(*self.args, P.snapshot(self.root)["revision"])
        return state

    def cli(self, *args, expected=0):
        value = subprocess.run([sys.executable, "-B", str(SCRIPTS / "pipeline_cli.py"),
                                *args, "--project", str(self.root)], text=True, capture_output=True)
        self.assertEqual(value.returncode, expected, value.stdout + value.stderr)
        return json.loads(value.stdout)

    def assignment(self, name="core", executor="dev-1", path="src/core"):
        return {"id": name, "executor_id": executor, "write_paths": [path], "depends_on": []}

    def test_new_managed_run_requires_decision_before_developer_request(self):
        self.start()
        self.assertEqual(G.snapshot(self.root)["allocation"]["status"], "pending")
        before = R.status(self.root)["state"]
        with self.assertRaisesRegex(ValueError, "allocation"):
            D.begin(*self.args, "developer")
        self.assertEqual(before, R.status(self.root)["state"])
        W.decide(*self.args, "serial", "shared_write_paths", [self.assignment()])
        planned = G.snapshot(self.root)
        self.assertEqual(planned["allocation"]["items"][0]["status"], "planned")
        self.assertEqual(planned["current"]["roles"], [])
        ticket = D.begin(*self.args, "developer", work_item="core")
        self.assertEqual(G.snapshot(self.root)["allocation"]["items"][0]["status"], "requested")
        with self.assertRaisesRegex(ValueError, "planned executor"):
            D.accepted(*self.args, ticket["id"], "wrong-alias")
        D.accepted(*self.args, ticket["id"], "dev-1", host_executor_id="fixture/native-1")
        accepted = G.snapshot(self.root)
        self.assertEqual(accepted["allocation"]["items"][0]["status"], "host_accepted")
        self.assertNotEqual(planned["milestone_id"], accepted["milestone_id"])
        self.assertNotIn(self.args[2], json.dumps(accepted))
        self.assertNotIn("fixture/native-1", json.dumps(accepted["allocation"]))
        D.complete(*self.args, ticket["id"], executors_stopped=True, stop_evidence="fixture native returned")
        value = G.snapshot(self.root)
        self.assertEqual(value["allocation"]["items"][0]["status"], "completed")
        self.assertIn("已返回", G.render(value))

    def test_task_start_reassesses_once_and_does_not_restart_live_roles(self):
        self.start()
        W.decide(*self.args, "serial", "shared_write_paths", [self.assignment()])
        ticket = D.begin(*self.args, "developer", work_item="core")
        D.accepted(*self.args, ticket["id"], "dev-1")
        T.start(*self.args, "new-goal", "technical goal", ["technical acceptance"], require_managed=True)
        state = R.status(self.root)["state"]
        self.assertEqual(W.summary(state)["status"], "pending")
        self.assertEqual(state["role_dispatches"][0]["status"], "working")
        W.decide(*self.args, "direct", "small_scope", [])
        T.start(*self.args, "new-goal", "technical goal", ["technical acceptance"], require_managed=True)
        self.assertEqual(W.summary(R.status(self.root)["state"])["status"], "decided")

    def test_real_cli_scope_change_idempotency_and_read_only_projection(self):
        state = self.cli("start")
        receipt = state["receipt"]
        self.cli("policy-ack", "--receipt", receipt, "--revision", state["policy"]["revision"])
        payload = json.dumps({"mode": "direct", "reason": "small_scope", "assignments": []})
        self.cli("allocation-decide", "--receipt", receipt, "--json", payload)
        self.cli("allocation-review", "--receipt", receipt, "--change-id", "scope-2")
        self.assertEqual(self.cli("allocation-status")["status"], "pending")
        self.cli("allocation-decide", "--receipt", receipt, "--json", payload)
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.cli("allocation-review", "--receipt", receipt, "--change-id", "scope-2")
        self.assertEqual(self.cli("allocation-status")["status"], "decided")
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_legacy_dispatch_has_no_fabricated_plan(self):
        state = R.start(self.root)
        args = self.root, state["run_id"], state["owner_token"]
        ticket = D.begin(*args, "developer")
        self.assertNotIn("allocation_revision", ticket)
        self.assertEqual(G.snapshot(self.root)["allocation"]["status"], "unknown")

    def test_planned_alias_reserves_serial_work_before_native_creation(self):
        self.start()
        items = [self.assignment(), self.assignment("next", path="src/core")]
        W.decide(*self.args, "serial", "shared_write_paths", items)
        first = D.begin(*self.args, "developer", work_item="core")
        before = R.status(self.root)["state"]
        with self.assertRaisesRegex(ValueError, "outstanding dispatch"):
            D.begin(*self.args, "developer", work_item="next")
        self.assertEqual(before, R.status(self.root)["state"])
        D.accepted(*self.args, first["id"], "dev-1")
        D.complete(*self.args, first["id"], executors_stopped=True, stop_evidence="fixture returned")
        with self.assertRaisesRegex(ValueError, "explicitly request reuse"):
            D.begin(*self.args, "developer", work_item="next")
        reused = D.begin(*self.args, "developer", executor_id="dev-1", work_item="next")
        self.assertEqual(reused["mode"], "reuse")

    def test_prior_task_planned_alias_remains_reserved_while_live(self):
        self.start()
        W.decide(*self.args, "serial", "shared_write_paths", [self.assignment()])
        D.begin(*self.args, "developer", work_item="core")
        T.start(*self.args, "task-2", "technical goal", ["technical acceptance"], require_managed=True)
        W.decide(*self.args, "serial", "shared_write_paths", [self.assignment("next", path="src/other")])
        before = R.status(self.root)["state"]
        with self.assertRaisesRegex(ValueError, "outstanding dispatch"):
            D.begin(*self.args, "developer", work_item="next")
        self.assertEqual(before, R.status(self.root)["state"])


if __name__ == "__main__":
    unittest.main()
