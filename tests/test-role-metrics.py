"""Frozen-clock role accounting in isolated projects, without host agents."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import role_metrics as M
import run_state as R


class RoleMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.call(0, R.start, self.root)

    def at(self, seconds):
        return (datetime(2026, 10, 27, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()

    def call(self, seconds, function, *args, **kwargs):
        with patch.object(R, "_now", return_value=self.at(seconds)):
            return function(*args, **kwargs)

    def event(self, seconds, event, executor="dev-1", role="developer", owner=None):
        return self.call(seconds, M.record_role_event, self.root, self.state["run_id"],
                         self.state["owner_token"] if owner is None else owner, role, executor, event)

    def summary(self, seconds, explicit=False):
        return self.call(seconds, M.summary, self.root, self.state["run_id"] if explicit else None)

    def finish(self, seconds):
        return self.call(seconds, R.finish, self.root, self.state["run_id"], self.state["owner_token"], "completed",
                         executors_stopped=True, stop_evidence="fixture has no host executors")

    def files(self):
        return {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.rglob("*") if path.is_file()}

    def test_preparation_work_wait_reuse_and_stop_preserve_phase_history(self):
        for seconds, event in ((0, "created"), (0, "preparing"), (5, "working"), (15, "waiting"),
                               (20, "stopped"), (30, "reused"), (30, "working"), (40, "stopped")):
            self.event(seconds, event)
        result = self.summary(100)
        role = result["roles"]["developer"]
        self.assertEqual(role["created_count"], 1)
        self.assertEqual(role["reused_count"], 1)
        self.assertEqual(role["phase_seconds"], {"preparing": 5, "working": 20, "waiting": 5})
        self.assertEqual(role["observed_wall_seconds"], 30)
        self.assertEqual(result["observed_wall_seconds"], 30)
        self.assertEqual(result["coverage"], "complete")
        self.assertEqual(R.status(self.root)["state"]["history"], self.state["history"])

    def test_parallel_actors_union_each_phase_and_overall_without_summing(self):
        self.event(0, "created", "one")
        self.event(0, "working", "one")
        self.event(5, "created", "two")
        self.event(5, "working", "two")
        self.event(10, "waiting", "one")
        self.event(20, "stopped", "one")
        self.event(20, "stopped", "two")
        result = self.summary(30)
        role = result["roles"]["developer"]
        self.assertEqual(role["created_count"], 2)
        self.assertEqual(role["phase_seconds"], {"preparing": None, "working": 20, "waiting": 10})
        self.assertEqual(role["observed_wall_seconds"], 20)
        self.assertEqual(result["observed_wall_seconds"], 20)
        self.assertEqual(sum(value for value in role["phase_seconds"].values() if value is not None), 30)

    def test_running_recovery_excludes_unbounded_old_phase_and_keeps_new_work(self):
        self.event(0, "created")
        self.event(0, "preparing")
        self.event(5, "working")
        for resume_at in (100, 200):
            self.call(resume_at, R.resume, self.root, self.state["run_id"], self.state["owner_token"],
                      executors_stopped=True, stop_evidence="isolated fixture has no actual executor")
            self.event(resume_at, "reused")
            self.event(resume_at, "working")
            self.event(resume_at + 10, "stopped")
        result = self.summary(220)
        self.assertEqual(result["roles"]["developer"]["phase_seconds"], {"preparing": 5, "working": 20, "waiting": None})
        self.assertEqual(result["observed_wall_seconds"], 25)
        self.assertEqual(result["coverage"], "partial")

    def test_running_recovery_without_new_role_event_does_not_extend_open_tail(self):
        self.event(0, "created")
        self.event(0, "working")
        self.call(100, R.resume, self.root, self.state["run_id"], self.state["owner_token"],
                  executors_stopped=True, stop_evidence="no fixture executor")
        result = self.summary(110)
        self.assertIsNone(result["roles"]["developer"]["phase_seconds"]["working"])
        self.assertIsNone(result["observed_wall_seconds"])
        self.assertNotEqual(result["coverage"], "complete")

    def test_parallel_roles_also_share_one_observed_wall_union(self):
        self.event(0, "created", "dev", "developer")
        self.event(0, "working", "dev", "developer")
        self.event(5, "created", "qa", "tester")
        self.event(5, "working", "qa", "tester")
        self.event(10, "stopped", "dev", "developer")
        self.event(15, "stopped", "qa", "tester")
        result = self.summary(20)
        self.assertEqual(result["roles"]["developer"]["observed_wall_seconds"], 10)
        self.assertEqual(result["roles"]["tester"]["observed_wall_seconds"], 10)
        self.assertEqual(result["observed_wall_seconds"], 15)

    def test_first_reused_marks_unknown_creation_and_never_invents_created(self):
        self.event(5, "reused")
        self.event(5, "preparing")
        self.event(10, "working")
        self.event(20, "stopped")
        result = self.summary(30)
        role = result["roles"]["developer"]
        self.assertEqual(role["created_count"], 0)
        self.assertEqual(role["reused_count"], 1)
        self.assertEqual(role["creation_history"], "unknown")
        self.assertEqual(role["coverage"], "partial")
        self.assertEqual(result["coverage"], "partial")
        self.assertNotIn("created", [entry["event"] for entry in R.status(self.root)["state"]["role_events"]])

    def test_created_to_first_phase_gap_is_unclassified_not_fake_preparation(self):
        self.event(0, "created")
        self.event(5, "working")
        self.event(10, "stopped")
        role = self.summary(20)["roles"]["developer"]
        self.assertEqual(role["phase_seconds"]["preparing"], None)
        self.assertEqual(role["phase_seconds"]["working"], 5)
        self.assertEqual(role["unclassified_gap_count"], 1)
        self.assertEqual(role["coverage"], "partial")

    def test_repeat_phase_does_not_change_events_timestamp_or_write(self):
        self.event(0, "created")
        first = self.event(2, "working")
        before = self.files()
        repeated = self.event(10, "working")
        self.assertEqual(first, repeated)
        self.assertEqual(before, self.files())
        self.assertEqual(self.summary(20)["roles"]["developer"]["phase_seconds"]["working"], 18)

    def test_running_open_phase_uses_read_time_but_finished_tail_stays_unknown(self):
        self.event(0, "created")
        self.event(0, "preparing")
        self.event(5, "working")
        self.assertEqual(self.summary(15)["roles"]["developer"]["phase_seconds"]["working"], 10)
        self.finish(20)
        first, later = self.summary(30, True), self.summary(100, True)
        self.assertEqual(first["roles"], later["roles"])
        self.assertEqual(first["roles"]["developer"]["phase_seconds"], {"preparing": 5, "working": None, "waiting": None})
        self.assertEqual(first["roles"]["developer"]["unclosed_phase_count"], 1)
        self.assertEqual(first["coverage"], "partial")
        self.assertEqual(first["observed_wall_seconds"], 5)

    def test_resuming_run_does_not_extend_unclosed_old_role_phase_across_pause(self):
        self.event(0, "created")
        self.event(0, "preparing")
        self.event(5, "working")
        self.call(20, R.finish, self.root, self.state["run_id"], self.state["owner_token"], "paused",
                  executors_stopped=True, stop_evidence="fixture has no executors")
        self.call(100, R.resume, self.root, self.state["run_id"], self.state["owner_token"],
                  executors_stopped=True, stop_evidence="fixture recovery confirmed")
        self.event(100, "reused")
        self.event(100, "working")
        self.event(110, "stopped")
        result = self.summary(120)
        role = result["roles"]["developer"]
        self.assertEqual(role["phase_seconds"], {"preparing": 5, "working": 10, "waiting": None})
        self.assertEqual(role["unclosed_phase_count"], 1)
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["observed_wall_seconds"], 15)

    def test_lifecycle_role_identity_labels_and_clock_errors_do_not_mutate(self):
        for event in ("preparing", "working", "waiting", "stopped"):
            with self.assertRaises(ValueError):
                self.event(0, event)
        self.event(1, "created")
        before = self.files()
        for role, executor, event in (("developer", "dev-1", "created"), ("tester", "dev-1", "working"),
                                      ("unknown", "x", "created"), ("developer", "../bad", "created"),
                                      ("developer", "", "created"), ("developer", "x", "unknown")):
            with self.subTest(role=role, executor=executor, event=event), self.assertRaises(ValueError):
                self.event(2, event, executor, role)
        with self.assertRaises(ValueError):
            self.event(0, "working")
        self.assertEqual(before, self.files())
        self.event(3, "stopped")
        before = self.files()
        for event in ("preparing", "working", "waiting", "stopped", "created"):
            with self.assertRaises(ValueError):
                self.event(4, event)
        self.assertEqual(before, self.files())
        self.event(5, "reused")
        self.event(6, "working")

    def test_unknown_owner_and_no_active_cannot_record_and_no_token_leaks(self):
        before = self.files()
        with self.assertRaises(ValueError):
            self.event(0, "created", owner="wrong")
        self.assertEqual(before, self.files())
        self.event(0, "created")
        self.event(0, "working")
        marker = self.root / "tmp/pipeline/.active"
        active = json.loads(marker.read_text())
        marker.write_text(json.dumps(dict(active, owner_token="wrong-owner")))
        result = self.summary(10)
        self.assertEqual(result["coverage"], "unknown")
        self.assertEqual(result["reason"], "active_owner_mismatch")
        marker.unlink()
        with self.assertRaises(ValueError):
            self.event(20, "stopped")
        result = self.summary(30, True)
        self.assertEqual(result["roles"]["developer"]["phase_seconds"]["working"], None)
        self.assertNotIn("owner_token", json.dumps(result))
        self.assertNotIn(self.state["owner_token"], json.dumps(result))

    def test_summary_is_read_only_for_legacy_empty_and_recorded_states(self):
        before = self.files()
        result = self.summary(5)
        self.assertEqual(result["coverage"], "unknown")
        self.assertIsNone(result["observed_wall_seconds"])
        self.assertEqual(before, self.files())
        self.event(5, "reused")
        self.event(10, "working")
        before = self.files()
        self.summary(20)
        self.assertEqual(before, self.files())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(self.call(20, M.summary, root)["coverage"], "unknown")
            self.assertEqual(list(root.iterdir()), [])

    def test_old_missing_or_bad_role_history_stays_unknown(self):
        path = self.root / "tmp/pipeline/runs" / f'{self.state["run_id"]}.json'
        state = json.loads(path.read_text())
        state.pop("created_at")
        path.write_text(json.dumps(state))
        self.assertIsNone(self.summary(20)["observed_wall_seconds"])
        self.event(20, "created")
        self.event(20, "working")
        self.assertEqual(self.summary(30)["coverage"], "partial")
        state = json.loads(path.read_text())
        state["role_events"] = [{"role": "developer"}]
        path.write_text(json.dumps(state))
        before = self.files()
        self.assertEqual(self.summary(30)["reason"], "invalid_role_history")
        with self.assertRaises(ValueError):
            self.event(40, "working")
        self.assertEqual(before, self.files())

    def test_failed_atomic_write_keeps_prior_events_and_stage_history(self):
        self.event(0, "created")
        before = R.status(self.root)["state"]
        with patch.object(R, "_atomic", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.event(5, "working")
        self.assertEqual(R.status(self.root)["state"], before)
        self.assertFalse((self.root / ".pipeline.models.lock").exists())


if __name__ == "__main__":
    unittest.main()
