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
import role_dispatch as D
import release_checks as F
import run_evidence as E
import run_checks


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

    def test_recovery_projects_latest_roles_without_reading_reports_or_writing(self):
        first = D.begin(self.root, self.run, self.owner, "developer")
        D.accepted(self.root, self.run, self.owner, first["id"], "worker", host_executor_id="native/worker")
        D.complete(self.root, self.run, self.owner, first["id"], executors_stopped=True, stop_evidence="host stopped")
        latest = D.begin(self.root, self.run, self.owner, "developer", executor_id="worker")
        D.accepted(self.root, self.run, self.owner, latest["id"], "worker", host_executor_id="native/worker")
        pending = D.begin(self.root, self.run, self.owner, "tester")
        R.update(self.root, self.run, self.owner, {"next": "verify " + self.owner})
        before = self.files()
        with patch.object(E, "read_report", side_effect=AssertionError("must not read evidence")), \
                patch.object(run_checks, "execute_check", side_effect=AssertionError("must not execute")):
            summary = C.recovery(self.root)
        roles = summary["roles"]["items"]
        worker = next(item for item in roles if item.get("executor_id") == "worker")
        self.assertEqual(worker["dispatch_id"], latest["id"])
        self.assertEqual(worker["host_executor_id"], "native/worker")
        self.assertEqual(worker["status"], "working")
        self.assertEqual(next(item for item in roles if item["dispatch_id"] == pending["id"])["status"], "dispatching")
        self.assertTrue(summary["selected_run_active"])
        self.assertNotIn(self.owner, json.dumps(summary))
        self.assertNotIn("owner_token", json.dumps(summary))
        self.assertEqual(self.files(), before)
        with self.assertRaisesRegex(ValueError, "live checks or roles"):
            self.wait()

    def test_recovery_is_bounded_and_rejects_concurrent_snapshot(self):
        refs = [f"tmp/pipeline/runs/{self.run}/artifacts/qa/attempt-{i}.json" for i in range(25)]
        R.update(self.root, self.run, self.owner, {"artifacts": refs, "pending": ["x" * 500] * 25})
        summary = C.recovery(self.root)
        self.assertEqual(summary["reports"]["omitted"], 5)
        self.assertEqual(summary["reports"]["items"], refs[-20:])
        self.assertEqual(len(summary["progress"]["pending"]["items"][0]), 256)
        before = R.status(self.root)
        self.at(10)
        R.update(self.root, self.run, self.owner, {"next": "new transition"})
        after = R.status(self.root)
        with patch.object(R, "status", side_effect=[before, after]):
            with self.assertRaisesRegex(ValueError, "changed during recovery"):
                C.recovery(self.root)

    def test_recovery_limits_prioritize_live_roles_and_unresolved_holds(self):
        role_metrics.record_role_event(self.root, self.run, self.owner, "developer", "live-first", "created")
        for i in range(25):
            role_metrics.record_role_event(self.root, self.run, self.owner, "tester", f"closed-{i}", "created")
            role_metrics.record_role_event(self.root, self.run, self.owner, "tester", f"closed-{i}", "stopped")
        with R._write_guard(self.root) as base:
            path, state = R._load_owned(base, self.run, self.owner)
            state["running_checks"] = [{"name": f"running-{i}"} for i in range(25)]
            state["release_plan"] = {"attempts": [
                {"name": "old-held", "phase": "activate", "status": "failed", "recovery_hold": {"status": "held"}},
                *[{"name": f"resolved-{i}", "phase": "activate", "status": "failed",
                   "recovery_hold": {"status": "resolved"}} for i in range(25)]]}
            R._atomic(path, state)
        summary = C.recovery(self.root)
        self.assertEqual(summary["live_role_count"], 1)
        self.assertEqual(summary["running_check_count"], 25)
        self.assertEqual(summary["running_checks"]["omitted"], 5)
        self.assertTrue(next(actor for actor in summary["roles"]["items"]
                             if actor["executor_id"] == "live-first")["live"])
        self.assertEqual(summary["release"]["unresolved_hold_count"], 1)
        self.assertIn("old-held", [hold["attempt"] for hold in summary["release"]["holds"]["items"]])
        self.assertFalse(summary["release"]["rerun_permitted"])

    def test_recovery_release_hold_and_activated_candidate_do_not_authorize_retry(self):
        (self.root / "PIPELINE.md").write_text("isolated fixture")
        (self.root / "source.py").write_text("stable input")
        args = self.root, self.run, self.owner

        def execute(name, phase, code="print('fixture')", **options):
            return F.execute(*args, name, phase=phase, argv=[sys.executable, "-c", code],
                             scope=["source.py"], timeout=10, **options)

        execute("preflight", "preflight")
        execute("compatibility", "compatibility")
        execute("build", "build", "open('artifact', 'w').write('candidate')", artifact_paths=["artifact"])
        execute("interrupted", "activate", "raise SystemExit(130)")
        before = self.files()
        summary = C.recovery(self.root)["release"]
        self.assertIsNotNone(summary["candidate"]["id"])
        self.assertEqual(summary["holds"]["items"][0]["status"], "held")
        self.assertFalse(summary["rerun_permitted"])
        self.assertEqual(self.files(), before)
        with self.assertRaisesRegex(ValueError, "hold unresolved"):
            execute("retry", "activate")
        self.at(10)
        evidence = E.write_report(self.root, self.run, self.owner, "release_recovery", "safe", {
            "attempt": "interrupted", "executors_stopped": True, "stop_evidence": "host stopped",
            "remote_state": "not_activated"})
        import delivery
        F.reconcile(*args, "interrupted", evidence=delivery._reference(self.root, self.run, evidence))
        execute("activated", "activate")
        summary = C.recovery(self.root)["release"]
        self.assertEqual(next(item for item in summary["latest_phases"]["items"]
                              if item["phase"] == "activate")["status"], "passed")
        self.assertFalse(summary["rerun_permitted"])
        with self.assertRaisesRegex(ValueError, "already activated"):
            execute("duplicate", "activate")

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
