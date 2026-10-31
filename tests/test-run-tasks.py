"""Execution task boundaries use isolated runs, never the repository active run."""
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
import run_state as R
import run_tasks as T


class TaskTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.state = R.start(self.root, {"pending": ["real pending"], "next": "real next"})
        self.run, self.owner = self.state["run_id"], self.state["owner_token"]

    def start(self, task="implement", goal="Fix issue", acceptance=None):
        return T.start(self.root, self.run, self.owner, task, goal,
                       ["Observed fixed behavior"] if acceptance is None else acceptance)

    def end(self, task="implement", **kwargs):
        return T.end(self.root, self.run, self.owner, task, **kwargs)

    def snapshot(self):
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(SCRIPTS / "pipeline_cli.py"),
                               *args, "--project", str(self.root)], capture_output=True, text=True)

    def receipt(self, state=None):
        state = self.state if state is None else state
        path = self.root / "private.json"
        R._atomic(path, {**{key: state[key] for key in ("schema_version", "run_id", "owner_token")},
                         "project_root": str(self.root)})
        return str(path)

    def test_unknown_history_read_only_and_no_backfill(self):
        before = self.snapshot()
        result = T.status(self.root)
        self.assertEqual((result["coverage"], result["tasks"]), ("unknown", []))
        self.assertNotIn(self.owner, json.dumps(result))
        self.assertEqual(self.cli("task-status").returncode, 0)
        self.assertEqual(self.snapshot(), before)
        with self.assertRaisesRegex(ValueError, "unknown task"):
            self.end()
        first = self.start()
        self.assertNotEqual(first["started_at"], self.state["created_at"])
        self.assertNotIn("tasks", self.state)
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(T.status(empty)["coverage"], "unknown")
            self.assertEqual(list(Path(empty).iterdir()), [])

    def test_owned_boundaries_idempotency_and_history(self):
        first = self.start()
        before = self.snapshot()
        self.assertEqual(self.start(), first)
        self.assertEqual(self.snapshot(), before)
        ref = E.write_report(self.root, self.run, self.owner, "developer", "first", {"result": "observed"})
        ended = self.end(report_refs=[ref])
        before = self.snapshot()
        self.assertEqual(self.end(report_refs=[ref]), ended)
        self.assertEqual(self.snapshot(), before)
        self.assertGreaterEqual(R._timestamp(ended["ended_at"]), R._timestamp(first["started_at"]))
        second = self.start("publish", "Publish verified change", ["Observe live identity"])
        self.assertEqual(T.status(self.root)["current_task_id"], "publish")
        self.end("publish", outcome="paused")
        tasks = T.status(self.root)["tasks"]
        self.assertEqual(tasks, [ended, {**second, "status": "paused", "ended_at": tasks[1]["ended_at"]}])
        self.assertEqual(self.start(), ended)  # retry cannot reopen an old segment
        before = self.snapshot()
        T.status(self.root, self.run)
        self.assertEqual(self.snapshot(), before)

    def test_switch_overwrite_and_conflicting_end_are_rejected(self):
        self.start()
        before = self.snapshot()
        for action in (lambda: self.start("other"), lambda: self.start(goal="Changed"),
                       lambda: self.start(acceptance=["Different acceptance"])):
            with self.assertRaises(ValueError):
                action()
            self.assertEqual(self.snapshot(), before)
        self.end()
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.end(outcome="aborted")
        self.assertEqual(self.snapshot(), before)

    def test_invalid_inputs_no_owner_leaks_or_silent_acceptance(self):
        for task, goal, acceptance in (("../task", "goal", ["a"]), ("ok", "", ["a"]),
                                       ("ok", "goal", []), ("ok", "goal", "a"),
                                       ("ok", "goal", [None]), ("ok", self.owner, ["a"])):
            with self.assertRaises(ValueError):
                T.start(self.root, self.run, self.owner, task, goal, acceptance)
        self.start()
        for outcome in ("completed", "PASS", None):
            with self.assertRaises(ValueError):
                self.end(outcome=outcome)

    def test_owner_active_marker_and_coordination_lock_are_enforced(self):
        self.start()
        before = self.snapshot()
        for action in (lambda: T.start(self.root, self.run, "wrong", "other", "goal", ["a"]),
                       lambda: T.end(self.root, self.run, "wrong", "implement")):
            with self.assertRaises(ValueError):
                action()
            self.assertEqual(self.snapshot(), before)
        lock = self.root / ".pipeline.models.lock"
        lock.write_text("other owner")
        with self.assertRaises(FileExistsError):
            self.end()
        self.assertEqual(lock.read_text(), "other owner")
        lock.unlink()
        marker = self.root / "tmp/pipeline/.active"
        active = marker.read_bytes()
        marker.write_text(json.dumps({"schema_version": 1, "run_id": "0" * 32, "owner_token": "other"}))
        with self.assertRaises(ValueError):
            self.end()
        marker.write_bytes(active)
        self.assertEqual(self.snapshot().keys(), before.keys())

    def test_report_refs_must_be_registered_safe_and_current_run(self):
        ref = E.write_report(self.root, self.run, self.owner, "qa", "old", {"gate": "CONCERNS"})
        R.finish(self.root, self.run, self.owner, "aborted", executors_stopped=True, stop_evidence="observed")
        second = R.start(self.root)
        self.state, self.run, self.owner = second, second["run_id"], second["owner_token"]
        self.start()
        for refs in ([ref], ["../outside.json"], ["/absolute.json"], [ref, ref], "invalid"):
            with self.assertRaises(ValueError):
                self.end(report_refs=refs)
        current = E.write_report(self.root, self.run, self.owner, "developer", "current", {})
        report = self.root / current
        copy = report.with_suffix(".saved")
        report.rename(copy)
        report.symlink_to(copy)
        with self.assertRaises(ValueError):
            self.end(report_refs=[current])
        self.assertEqual(T.status(self.root)["tasks"][0]["status"], "running")

    def test_finish_requires_explicit_task_end_all_outcomes_legacy_unchanged(self):
        self.start()
        for outcome in sorted(R.FINAL):
            with self.assertRaisesRegex(ValueError, "end the current task"):
                R.finish(self.root, self.run, self.owner, outcome,
                         executors_stopped=True, stop_evidence="observed stopped")
        self.end()
        with self.assertRaisesRegex(ValueError, "all executors stopped"):
            R.finish(self.root, self.run, self.owner, "completed")
        ended = R.finish(self.root, self.run, self.owner, "completed",
                         executors_stopped=True, stop_evidence="observed stopped")
        self.assertEqual(ended["status"], "completed")
        self.assertEqual(T.status(self.root, self.run)["tasks"][0]["status"], "execution_ended")
        old = R.start(self.root)
        self.assertNotIn("tasks", R.finish(self.root, old["run_id"], old["owner_token"], "aborted",
                                          executors_stopped=True, stop_evidence="observed"))

    def test_end_does_not_stop_roles_clear_wait_pending_or_manufacture_qa(self):
        path = self.root / "tmp/pipeline/runs" / (self.run + ".json")
        state = R.status(self.root)["state"]
        state.update(continuity_wait={"status": "waiting"}, qa_rounds=3,
                     role_dispatches=[{"status": "working"}], check_plans={"existing": {}})
        R._atomic(path, state)
        self.start()
        self.end()
        self.start("followup")
        self.end("followup")
        after = R.status(self.root)["state"]
        self.assertEqual({key: value for key, value in after.items() if key not in {"tasks", "updated_at"}},
                         {key: value for key, value in state.items() if key != "updated_at"})
        with self.assertRaisesRegex(ValueError, "role dispatches"):
            R.finish(self.root, self.run, self.owner, "completed",
                     executors_stopped=True, stop_evidence="observed")
        after["role_dispatches"] = []
        R._atomic(path, after)
        with self.assertRaisesRegex(ValueError, "human wait"):
            R.finish(self.root, self.run, self.owner, "completed",
                     executors_stopped=True, stop_evidence="observed")

    def test_latest_failed_check_still_blocks_qa_after_switching_task(self):
        (self.root / "input").write_text("stable")
        check = {"argv": [sys.executable, "-B", "-c", "import sys; sys.exit(1)"],
                 "scope": ["input"], "coverage": ["unit"], "write_paths": []}
        plan = P.save_plan(self.root, self.run, self.owner, "plan", {"unit": check}, ["unit"])
        self.start()
        result = P.run_planned(self.root, self.run, self.owner, plan, "unit", "failed")
        self.assertEqual(result["exit_code"], 1)
        qa = P.qa_from_plan(self.root, self.run, self.owner, plan, "before")
        self.end(report_refs=[qa])
        self.start("next")
        ref = P.qa_from_plan(self.root, self.run, self.owner, plan, "after")
        payload = E.read_report(self.root, self.run, ref, kind="qa")["payload"]
        self.assertEqual(payload["gate"], "CONCERNS")
        self.assertIn("unit", payload["check_failures"])
        blocked = self.cli("finish", "--receipt", self.receipt(), "--stopped", "--stop-evidence", "observed", "--direct")
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertEqual(json.loads(blocked.stdout)["error"], "qa_required_before_finish")

    def test_cli_owned_roundtrip_rejection_and_readonly_status(self):
        receipt = self.receipt()
        args = ("task-start", "--receipt", receipt, "--task-id", "first", "--goal", "Fix issue", "--accept", "Observed")
        first = self.cli(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = self.snapshot()
        self.assertEqual(self.cli(*args).stdout, first.stdout)
        self.assertEqual(self.snapshot(), before)
        refused = self.cli("task-start", "--receipt", receipt, "--task-id", "second", "--goal", "Other", "--accept", "Observed")
        self.assertEqual(refused.returncode, 2, refused.stderr)
        ended = self.cli("task-end", "--receipt", receipt, "--task-id", "first")
        self.assertEqual(ended.returncode, 0, ended.stderr)
        self.assertEqual(json.loads(ended.stdout)["task"]["status"], "execution_ended")
        before = self.snapshot()
        status = self.cli("task-status", "--run-id", self.run)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertNotIn(self.owner, status.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_clock_regression_rejected_without_mutation(self):
        self.start()
        before = self.snapshot()
        with patch.object(T.R, "_now", return_value="2000-01-01T00:00:00+00:00"):
            with self.assertRaisesRegex(ValueError, "clock moved backwards"):
                self.end()
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
