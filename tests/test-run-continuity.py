"""Isolated continuity behaviors; never inspect or mutate the repository run."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import run_continuity as C
import run_state as R
import role_metrics


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.start_at = datetime.now(timezone.utc) - timedelta(seconds=100)
        self.clock = patch.object(R, "_now", return_value=self.start_at.isoformat())
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.state = R.start(self.root, {"pending": ["real browser acceptance"],
                                        "next": "finish authorized task", "artifacts": ["qa-failure.json"]})
        self.run = self.state["run_id"]
        self.owner = self.state["owner_token"]
        self.args = {"reason": "browser session missing", "action": "Log in to the target account",
                     "verify": "Rerun real browser acceptance and inspect resulting state"}

    def at(self, seconds):
        value = (self.start_at + timedelta(seconds=seconds)).isoformat()
        self.now.return_value = value
        return value

    def wait(self, owner=None):
        return C.wait_for_user(self.root, self.run, owner or self.owner, **self.args)

    def files(self):
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file()}

    def test_owned_atomic_wait_preserves_identity_evidence_and_is_idempotent(self):
        before = self.files()
        with self.assertRaises(ValueError):
            self.wait("wrong-owner")
        self.assertEqual(self.files(), before)
        self.at(10)
        waiting = self.wait()
        for key in ("run_id", "owner_token", "pending", "artifacts", "next"):
            self.assertEqual(waiting[key], self.state[key])
        self.assertEqual(waiting["activity"], "human_wait")
        before = self.files()
        self.at(15)
        self.assertEqual(self.wait(), waiting)
        self.assertEqual(self.files(), before)
        with self.assertRaises(ValueError):
            C.wait_for_user(self.root, self.run, self.owner, **{**self.args, "verify": "different"})
        with self.assertRaises(ValueError):
            C.wait_for_user(self.root, self.run, self.owner, **{**self.args, "action": self.owner})
        self.assertEqual(self.files(), before)

    def test_live_roles_and_checks_are_not_human_wait(self):
        role_metrics.record_role_event(self.root, self.run, self.owner, "developer", "worker", "created")
        with self.assertRaisesRegex(ValueError, "live checks or roles"):
            self.wait()
        role_metrics.record_role_event(self.root, self.run, self.owner, "developer", "worker", "stopped")
        with R._write_guard(self.root) as base:
            path, state = R._load_owned(base, self.run, self.owner)
            state["running_checks"] = [{"name": "browser"}]
            R._atomic(path, state)
        with self.assertRaisesRegex(ValueError, "live checks or roles"):
            self.wait()
        end = self.at(20)
        self.at(30)
        observed = C.inspect(self.root, self.run, host_status="idle", host_ended_at=end)
        self.assertFalse(observed["unclosed"])
        self.assertIsNone(observed["known_idle_seconds"])

    def test_wait_blocks_completion_and_acknowledgement_only_schedules_verification(self):
        self.at(10)
        self.wait()
        before = self.files()
        with self.assertRaisesRegex(ValueError, "registered human wait"):
            R.finish(self.root, self.run, self.owner, "completed", executors_stopped=True, stop_evidence="all stopped")
        self.assertEqual(self.files(), before)
        with self.assertRaises(ValueError):
            C.resume_work(self.root, self.run, "wrong-owner", evidence="logged in")
        self.at(40)
        resumed = C.resume_work(self.root, self.run, self.owner, evidence="User reports login complete")
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(resumed["next"], self.args["verify"])
        self.assertEqual(resumed["pending"], self.state["pending"])
        self.assertEqual(resumed["artifacts"], self.state["artifacts"])
        self.assertNotIn("qa", resumed)
        self.at(50)
        timing = R.timing(self.root, self.run)
        self.assertEqual(timing["activity_seconds"]["human_wait"], 30)
        self.assertEqual(timing["activity_seconds"]["execution"], 20)
        self.assertTrue((self.root / "tmp/pipeline/.active").exists())

    def test_explicit_host_idle_caps_only_open_tail_and_never_writes(self):
        self.at(10)
        self.wait()
        end = self.at(40)
        self.at(70)
        before = self.files()
        observed = C.inspect(self.root, self.run, host_status="idle", host_ended_at=end)
        self.assertTrue(observed["unclosed"])
        self.assertTrue(observed["waiting_for_user"])
        self.assertEqual(observed["known_idle_seconds"], 30)
        self.assertEqual(observed["timing"]["tracked_seconds"], 40)
        self.assertEqual(observed["timing"]["activity_seconds"]["execution"], 10)
        self.assertEqual(observed["timing"]["activity_seconds"]["human_wait"], 30)
        self.assertNotIn(self.owner, json.dumps(observed))
        self.assertEqual(self.files(), before)
        self.assertEqual(R.timing(self.root, self.run)["tracked_seconds"], 70)
        self.assertEqual(R.status(self.root)["state"]["status"], "running")

    def test_unknown_stale_future_and_unrelated_host_observations_stay_unknown(self):
        stale = self.at(5)
        self.at(10)
        self.wait()
        future = self.at(90)
        self.at(70)
        for kwargs in ({}, {"host_status": "idle"}, {"host_status": "idle", "host_ended_at": stale},
                       {"host_status": "idle", "host_ended_at": future},
                       {"host_status": "idle", "host_ended_at": "malformed"},
                       {"host_status": "unknown", "host_ended_at": stale}):
            observed = C.inspect(self.root, self.run, **kwargs)
            self.assertEqual(observed["host_observation"], "unknown")
            self.assertIsNone(observed["known_idle_seconds"])
            self.assertEqual(observed["timing"]["tracked_seconds"], 70)

    def test_pause_resume_preserves_explicit_wait_and_legacy_completion(self):
        self.at(10)
        self.wait()
        R.finish(self.root, self.run, self.owner, "paused", executors_stopped=True, stop_evidence="stopped")
        self.at(20)
        R.resume(self.root, self.run, self.owner, executors_stopped=True, stop_evidence="stopped")
        with self.assertRaisesRegex(ValueError, "registered human wait"):
            R.finish(self.root, self.run, self.owner, "completed", executors_stopped=True, stop_evidence="stopped")
        C.resume_work(self.root, self.run, self.owner, evidence="manual action completed; verification pending")
        R.finish(self.root, self.run, self.owner, "aborted", executors_stopped=True, stop_evidence="stopped")
        legacy = R.start(self.root, {"activity": "human_wait"})
        done = R.finish(self.root, legacy["run_id"], legacy["owner_token"], "completed",
                        executors_stopped=True, stop_evidence="legacy stop")
        self.assertEqual(done["status"], "completed")


if __name__ == "__main__":
    unittest.main()
