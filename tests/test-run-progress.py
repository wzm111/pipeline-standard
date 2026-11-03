"""Progress is bounded metadata, not revalidation, health or raw output."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"
sys.path.insert(0, str(SCRIPTS))
import check_plan as P
import run_evidence as E
import run_progress as G
import run_state as R
import run_tasks as T


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "input").write_text("fixture")
        self.state = R.start(self.root)
        self.args = self.root, self.state["run_id"], self.state["owner_token"]

    def plan(self):
        check = {"argv": [sys.executable, "-B", "-c", "print('success')"],
                 "scope": ["input"], "coverage": ["unit"]}
        return P.save_plan(*self.args, "plan", {"quick": {**check, "layer": "quick"}, "final": check})

    def files(self):
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file()}

    def test_no_run_does_not_create_files(self):
        with tempfile.TemporaryDirectory() as empty:
            value = G.snapshot(empty)
            self.assertIsNone(value["counts"]["planned"])
            self.assertIsNone(value["last_result_at"])
            self.assertEqual(list(Path(empty).iterdir()), [])

    def test_steps_use_registered_results_and_do_not_revalidate_or_read_logs(self):
        plan = self.plan()
        P.run_planned(*self.args, plan, "quick", "q1")
        before = self.files()
        with patch.object(E, "fingerprint", side_effect=AssertionError("no source hashing")), \
             patch.object(E, "reusable_check", side_effect=AssertionError("not acceptance")):
            value = G.snapshot(self.root)
        self.assertEqual(self.files(), before)
        self.assertEqual(value["completed"][0]["step"], "quick")
        self.assertEqual(value["remaining"][0]["step"], "final")
        self.assertEqual(value["counts"], {"recorded_success": 1, "remaining": 1, "planned": 2})
        self.assertIsNotNone(value["last_result_at"])
        self.assertIn("not revalidated", value["notice"])

    def test_later_failure_overrides_bound_success_and_does_not_expose_payload(self):
        plan = self.plan()
        result = P.run_planned(*self.args, plan, "quick", "q1")
        payload = E.read_report(self.root, self.args[1], result["report"])["payload"]
        E.write_report(*self.args, "check", "later", {**payload, "exit_code": 1,
                       "command": payload["command"], "stdout": "PRIVATE", "environment": {"PRIVATE": "SECRET"}})
        value = G.snapshot(self.root)
        self.assertEqual(value["completed"], [])
        self.assertEqual(value["remaining"][0]["result"], "recorded_failure")
        self.assertIn("recorded_check_failure", value["blockers"])
        self.assertNotIn("PRIVATE", json.dumps(value))
        self.assertNotIn("SECRET", json.dumps(value))
        self.assertNotIn(self.args[2], json.dumps(value))

    def test_old_task_results_do_not_become_current_task_progress(self):
        plan = self.plan()
        P.run_planned(*self.args, plan, "quick", "q1")
        T.start(*self.args, "new-task", "private goal", ["private acceptance"])
        value = G.snapshot(self.root)
        self.assertEqual(value["task_id"], "new-task")
        self.assertIsNone(value["counts"]["planned"])
        self.assertIsNone(value["last_result_at"])
        self.assertNotIn("private", json.dumps(value))

    def test_state_refresh_is_not_a_new_result_or_milestone(self):
        first = G.snapshot(self.root)
        R.update(*self.args, {"next": "SECRET"})
        second = G.snapshot(self.root)
        self.assertEqual(first["milestone_id"], second["milestone_id"])
        self.assertNotEqual(first["state_updated_at"], second["state_updated_at"])
        self.assertIsNone(second["last_result_at"])

    def test_wait_and_registered_roles_are_not_health_proof(self):
        path = self.root / "tmp/pipeline/runs" / (self.args[1] + ".json")
        state = R.status(self.root)["state"]
        state.update(continuity_wait={"status": "waiting", "action": "SECRET"},
                     stage="PRIVATE goal", running_checks=[{"name": "unit"}],
                     role_dispatches=[{"role": "tester", "status": "working", "owner_token": "SECRET"}])
        R._atomic(path, state)
        value = G.snapshot(self.root)
        self.assertEqual(value["badge"], "🔴[需你处理]")
        self.assertEqual(value["stage"], "unknown")
        self.assertIsNone(value["current"]["checks"][0]["started_at"])
        self.assertNotIn("SECRET", json.dumps(value))
        self.assertNotIn("PRIVATE", json.dumps(value))
        text = G.render(value)
        self.assertIn("unit", text)
        self.assertIn("tester", text)
        self.assertEqual(value["next"], "user_action_then_verify")

    def test_cli_text_has_steps_and_no_receipt_requirement(self):
        self.plan()
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "pipeline_cli.py"), "progress",
                                 "--project", str(self.root), "--format", "text"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for field in ("当前任务", "当前步骤", "已记录成功", "剩余/待核实", "阻塞", "下一步", "[待核实]"):
            self.assertIn(field, result.stdout)

    def test_task_end_is_not_completion_and_later_results_are_excluded(self):
        T.start(*self.args, "local-work", "private goal", ["private acceptance"])
        plan = self.plan()
        P.run_planned(*self.args, plan, "quick", "before-end")
        T.end(*self.args, "local-work")
        value = G.snapshot(self.root)
        self.assertEqual(value["task_execution"]["status"], "execution_ended")
        self.assertEqual(value["delivery"]["status"], "execution_ended_acceptance_pending")
        self.assertEqual(value["delivery"]["acceptance"], "not_revalidated")
        self.assertEqual(value["badge"], "🔎[待核实]")
        self.assertEqual(value["next"], "review_task_acceptance")
        import run_checks as C
        C.execute_check(*self.args, "after-end", argv=[sys.executable, "-c", "raise SystemExit(1)"], scope=["input"])
        later = G.snapshot(self.root)
        self.assertEqual(later["last_result_at"], value["last_result_at"])
        self.assertEqual(later["counts"], value["counts"])
        self.assertIn("执行段已结束", G.render(later))
        self.assertIn("验收/交付待核实", G.render(later))

    def test_external_wait_and_stale_idle_feedback_do_not_claim_health(self):
        import run_continuity as C
        C.wait_for_user(*self.args, reason="private deployment", action="private action", verify="private verify")
        first = G.snapshot(self.root)
        self.assertEqual(first["delivery"]["status"], "external_action_pending")
        self.assertEqual(first["host_feedback"]["status"], "unknown")
        at = R._now()
        idle = G.snapshot(self.root, host_status="idle", host_ended_at=at)
        self.assertEqual(idle["host_feedback"]["status"], "idle")
        self.assertEqual(idle["badge"], "🔴[需你处理]")
        self.assertNotIn("private", json.dumps(idle))
        C.resume_work(*self.args, evidence="user responded; verify still needed")
        stale = G.snapshot(self.root, host_status="idle", host_ended_at=at)
        self.assertEqual(stale["host_feedback"]["status"], "stale")
        self.assertEqual(stale["delivery"]["acceptance"], "not_revalidated")

    def test_idle_feedback_conflicting_with_registered_work_is_not_completion(self):
        path = self.root / "tmp/pipeline/runs" / (self.args[1] + ".json")
        state = R.status(self.root)["state"]
        state["running_checks"] = [{"name": "registered-only"}]
        R._atomic(path, state)
        before = self.files()
        value = G.snapshot(self.root, host_status="idle", host_ended_at=R._now())
        self.assertIn("host_idle_with_registered_work", value["blockers"])
        self.assertEqual(value["badge"], "🔎[待核实]")
        self.assertEqual(value["next"], "reconcile_host_and_records")
        self.assertEqual(before, self.files())
        with self.assertRaises(ValueError):
            G.snapshot(self.root, host_status="idle", host_ended_at="9999-01-01T00:00:00Z")
        with self.assertRaises(ValueError):
            G.snapshot(self.root, host_status="running", host_ended_at=R._now())

    def test_declared_wait_without_action_is_reported_incomplete(self):
        R.update(*self.args, {"activity": "human_wait"})
        value = G.snapshot(self.root)
        self.assertEqual(value["badge"], "🔎[待核实]")
        self.assertIn("human_wait_action_unrecorded", value["blockers"])
        self.assertEqual(value["next"], "reconcile_host_and_records")

    def test_cli_host_feedback_is_explicit_and_read_only(self):
        before = self.files()
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "pipeline_cli.py"), "progress",
                                 "--project", str(self.root), "--host-status", "idle",
                                 "--host-ended-at", R._now()], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["host_feedback"]["status"], "idle")
        self.assertEqual(value["next"], "reconcile_host_and_records")
        self.assertEqual(self.files(), before)

    def test_missing_report_is_unknown_and_output_is_bounded(self):
        self.plan()
        state = R.status(self.root)["state"]
        state["running_checks"] = [{"name": f"check-{i}"} for i in range(25)]
        state["artifacts"] += [f"tmp/pipeline/runs/{self.args[1]}/artifacts/check/missing.json"]
        R._atomic(self.root / "tmp/pipeline/runs" / (self.args[1] + ".json"), state)
        value = G.snapshot(self.root)
        self.assertTrue(value["truncated"])
        self.assertEqual(len(value["current"]["checks"]), 20)
        self.assertEqual(value["unavailable_records"], 1)
        self.assertEqual(value["remaining"][0]["result"], "not_observed")

    def test_concurrent_state_change_rejects_snapshot(self):
        original = R.status
        calls = []
        def read(*args, **kwargs):
            value = original(*args, **kwargs)
            if calls:
                value["state"]["stage"] = "changed"
            calls.append(True)
            return value
        with patch.object(R, "status", side_effect=read), self.assertRaisesRegex(ValueError, "changed"):
            G.snapshot(self.root)


if __name__ == "__main__":
    unittest.main()
