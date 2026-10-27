"""Run lifecycle tests use isolated directories; never touch the real active run."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUN = load("run_state")
MODELS = load("model_config")


class RunStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.base = self.root / "tmp/pipeline"

    def finish(self, state, outcome="completed"):
        return RUN.finish(self.root, state["run_id"], state["owner_token"], outcome,
                          executors_stopped=True, stop_evidence="caller observed all executor completions")

    def test_status_is_read_only_including_empty_project_and_cli(self):
        self.assertEqual(RUN.status(self.root), {"active": None, "state": None})
        result = subprocess.run([sys.executable, str(SCRIPTS / "run_state.py"), "--project", str(self.root)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"active": None, "state": None})
        self.assertEqual(list(self.root.iterdir()), [])
        state = RUN.start(self.root)
        before = {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(RUN.status(self.root)["state"], state)
        after = {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_start_is_unique_and_refuses_existing_active(self):
        first = RUN.start(self.root)
        marker = (self.base / ".active").read_bytes()
        with self.assertRaises(ValueError):
            RUN.start(self.root)
        self.assertEqual((self.base / ".active").read_bytes(), marker)
        self.finish(first)
        second = RUN.start(self.root)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(first["owner_token"], second["owner_token"])
        self.assertEqual(RUN.status(self.root, first["run_id"])["state"]["status"], "completed")

    def test_competing_starts_have_exactly_one_winner(self):
        def attempt(_):
            try:
                return RUN.start(self.root)
            except (OSError, ValueError):
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(RUN.status(self.root)["state"], winners[0])
        self.assertEqual(len(list((self.base / "runs").glob("*.json"))), 1)

    def test_old_and_corrupt_marker_is_not_deleted_or_adopted(self):
        self.base.mkdir(parents=True)
        for content in ("legacy-run", "{broken", '{"schema_version":2}'):
            (self.base / ".active").write_text(content)
            self.assertEqual(RUN.status(self.root)["active"], "unrecognized")
            with self.assertRaises(ValueError):
                RUN.start(self.root)
            self.assertEqual((self.base / ".active").read_text(), content)
        self.assertFalse((self.base / "runs").exists())

    def test_wrong_owner_cannot_update_finish_or_resume(self):
        state = RUN.start(self.root)
        before = RUN.status(self.root)
        for operation in (
            lambda: RUN.update(self.root, state["run_id"], "other-owner", {"stage": "wrong"}),
            lambda: RUN.finish(self.root, state["run_id"], "other-owner", "completed", executors_stopped=True, stop_evidence="stopped"),
            lambda: RUN.resume(self.root, state["run_id"], "other-owner", executors_stopped=True, stop_evidence="stopped"),
        ):
            with self.assertRaises(ValueError):
                operation()
            self.assertEqual(RUN.status(self.root), before)

    def test_update_preserves_artifacts_and_disallows_ownership_overwrite(self):
        state = RUN.start(self.root, {"artifacts": ["report.md"], "pending": ["test"]})
        updated = RUN.update(self.root, state["run_id"], state["owner_token"], {"stage": "testing"})
        self.assertEqual(updated["artifacts"], ["report.md"])
        self.assertEqual(updated["pending"], ["test"])
        with self.assertRaises(ValueError):
            RUN.update(self.root, state["run_id"], state["owner_token"], {"owner_token": "injected"})

    def test_finish_all_outcomes_preserves_state_and_requires_stopped_evidence(self):
        for outcome in ("paused", "aborted", "completed"):
            state = RUN.start(self.root, {"pending": ["remaining"], "artifacts": ["code.patch"]})
            for confirmed, evidence in ((False, "observed"), (True, None), (1, "observed"), (True, " ")):
                with self.assertRaises(ValueError):
                    RUN.finish(self.root, state["run_id"], state["owner_token"], outcome,
                               executors_stopped=confirmed, stop_evidence=evidence)
                self.assertTrue((self.base / ".active").exists())
            final = self.finish(state, outcome)
            self.assertFalse((self.base / ".active").exists())
            self.assertEqual(final["status"], outcome)
            self.assertEqual(final["artifacts"], ["code.patch"])
            self.assertEqual(final["pending"], ["remaining"])
            self.assertEqual(RUN.status(self.root, state["run_id"])["state"], final)

    def test_resume_requires_ownership_and_evidence_retains_run_identity(self):
        state = RUN.start(self.root)
        self.finish(state, "paused")
        with self.assertRaises(ValueError):
            RUN.resume(self.root, state["run_id"], state["owner_token"])
        resumed = RUN.resume(self.root, state["run_id"], state["owner_token"],
                             executors_stopped=True, stop_evidence="all executor ids confirmed stopped")
        self.assertEqual(resumed["run_id"], state["run_id"])
        self.assertEqual(resumed["owner_token"], state["owner_token"])
        self.assertEqual(RUN.status(self.root)["state"], resumed)
        self.finish(resumed)
        with self.assertRaises(ValueError):
            RUN.resume(self.root, resumed["run_id"], resumed["owner_token"], executors_stopped=True, stop_evidence="stopped")

    def test_recovery_never_clears_another_runs_marker(self):
        old = RUN.start(self.root)
        self.finish(old, "aborted")
        current = RUN.start(self.root)
        with self.assertRaises(ValueError):
            RUN.resume(self.root, old["run_id"], old["owner_token"], executors_stopped=True, stop_evidence="old stopped")
        self.assertEqual(RUN.status(self.root)["state"], current)

    def test_resume_marker_failure_can_be_recovered_with_same_identity(self):
        state = RUN.start(self.root)
        self.finish(state, "paused")
        with patch.object(RUN, "_claim", side_effect=OSError("marker write failed")):
            with self.assertRaises(OSError):
                RUN.resume(self.root, state["run_id"], state["owner_token"],
                           executors_stopped=True, stop_evidence="all stopped before recovery")
        partial = RUN.status(self.root, state["run_id"])
        self.assertIsNone(partial["active"])
        self.assertEqual(partial["state"]["owner_token"], state["owner_token"])
        recovered = RUN.resume(self.root, state["run_id"], state["owner_token"],
                               executors_stopped=True, stop_evidence="all stopped, rechecked after failure")
        self.assertEqual(RUN.status(self.root)["state"], recovered)

    def test_path_safety_system_alias_and_internal_symlinks(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        state = RUN.start(alias)
        self.assertEqual(RUN.status(alias)["state"], state)
        self.finish(state)
        state_path = self.base / "runs" / f'{state["run_id"]}.json'
        contents = state_path.read_bytes()
        target = self.root / "external.json"
        target.write_bytes(contents)
        state_path.unlink()
        state_path.symlink_to(target)
        with self.assertRaises(ValueError):
            RUN.status(self.root, state["run_id"])
        for control in (self.base / ".active", self.root / ".pipeline.models.lock"):
            control.symlink_to(target)
            with self.assertRaises(ValueError):
                RUN.start(self.root)
            control.unlink()
        with tempfile.TemporaryDirectory() as other:
            root = Path(other).resolve()
            (root / "tmp").symlink_to(self.base, target_is_directory=True)
            with self.assertRaises(ValueError):
                RUN.status(root)

    def test_shared_config_lock_blocks_run_start_and_active_blocks_model_save(self):
        lock = self.root / ".pipeline.models.lock"
        lock.write_text("model save in progress")
        with self.assertRaises(FileExistsError):
            RUN.start(self.root)
        self.assertEqual(lock.read_text(), "model save in progress")
        self.assertFalse(self.base.exists())
        lock.unlink()
        state = RUN.start(self.root)
        with self.assertRaises(ValueError):
            MODELS.save_project(self.root, {"schema_version": 1, "runtimes": {}}, None)
        self.finish(state)
        MODELS.save_project(self.root, {"schema_version": 1, "runtimes": {}}, None)

    def test_run_start_and_model_save_coordinate_before_active_marker_exists(self):
        blocked = []
        def try_settings(_fd):
            active_exists = (self.base / ".active").exists()
            with self.assertRaises(ValueError if active_exists else FileExistsError):
                MODELS.save_project(self.root, {"schema_version": 1, "runtimes": {}}, None)
            blocked.append(active_exists)
        with patch.object(RUN.os, "fsync", side_effect=try_settings):
            state = RUN.start(self.root)
        self.assertEqual(blocked, [False, True])
        self.finish(state)
        def try_start(_fd):
            with self.assertRaises(FileExistsError):
                RUN.start(self.root)
        with patch.object(MODELS.os, "fsync", side_effect=try_start):
            MODELS.save_project(self.root, {"schema_version": 1, "runtimes": {}}, None)
        self.assertIsNone(RUN.status(self.root)["active"])

    def test_state_write_failure_keeps_marker_and_previous_state(self):
        state = RUN.start(self.root)
        with patch.object(RUN.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.finish(state)
        self.assertEqual(RUN.status(self.root)["state"], state)
        self.assertTrue((self.base / ".active").exists())
        self.assertFalse((self.root / ".pipeline.models.lock").exists())
        self.assertEqual(list(self.base.rglob(".run-state.*")), [])


class TimingTests(unittest.TestCase):
    setUp = RunStateTests.setUp

    def at(self, seconds):
        return (datetime(2026, 10, 27, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()

    def call_at(self, seconds, function, *args, **kwargs):
        with patch.object(RUN, "_now", return_value=self.at(seconds)):
            return function(*args, **kwargs)

    def start(self, seconds=0, initial=None):
        return self.call_at(seconds, RUN.start, self.root, initial)

    def update(self, state, seconds, changes):
        return self.call_at(seconds, RUN.update, self.root, state["run_id"], state["owner_token"], changes)

    def finish(self, state, seconds, outcome="completed"):
        return self.call_at(seconds, RUN.finish, self.root, state["run_id"], state["owner_token"], outcome,
                            executors_stopped=True, stop_evidence="all executor completions observed")

    def resume(self, state, seconds):
        return self.call_at(seconds, RUN.resume, self.root, state["run_id"], state["owner_token"],
                            executors_stopped=True, stop_evidence="all stopped, recovery checked")

    def timing(self, seconds, run_id=None):
        return self.call_at(seconds, RUN.timing, self.root, run_id)

    def test_full_frozen_lifecycle_partitions_time_without_pause_or_parallel_sum(self):
        state = self.start()
        self.assertEqual(state["created_at"], self.at(0))
        self.assertEqual(state["activity"], "execution")
        self.assertEqual(state["history"], [{"event": "start", "at": self.at(0), "stage": "start", "activity": "execution"}])
        self.update(state, 5, {"stage": "design"})
        self.update(state, 10, {"activity": "role_wait", "pending": ["parallel-role-A", "parallel-role-B"]})
        self.update(state, 15, {"activity": "human_wait"})
        self.update(state, 20, {"activity": "handoff"})
        self.update(state, 25, {"stage": "verify", "activity": "testing"})
        paused = self.finish(state, 35, "paused")
        self.assertIn("stop_evidence", paused["history"][-1])
        resumed = self.resume(state, 100)
        self.assertEqual(resumed["owner_token"], state["owner_token"])
        self.assertEqual(resumed["activity"], "execution")
        self.finish(state, 110)
        result = self.timing(200, state["run_id"])
        self.assertEqual(result["coverage"], "complete")
        self.assertEqual(result["tracked_seconds"], 45)
        self.assertEqual(result["paused_seconds"], 65)
        self.assertEqual(result["activity_seconds"], {"execution": 20, "testing": 10, "human_wait": 5, "role_wait": 5, "handoff": 5})
        self.assertEqual(result["stage_seconds"], {"start": 5, "design": 20, "verify": 20})
        self.assertEqual(sum(item["seconds"] for item in result["intervals"]), 45)
        for previous, following in zip(result["intervals"], result["intervals"][1:]):
            self.assertLessEqual(previous["end"], following["start"])

    def test_pending_queries_and_same_stage_activity_add_no_events(self):
        state = self.start()
        self.update(state, 5, {"pending": ["next"]})
        self.update(state, 10, {"stage": "start", "activity": "execution"})
        snapshot = RUN.status(self.root)
        self.assertEqual(len(snapshot["state"]["history"]), 1)
        self.assertEqual(self.timing(20)["tracked_seconds"], 20)
        self.assertEqual(len(RUN.status(self.root)["state"]["history"]), 1)
        self.update(state, 25, {"stage": "testing", "activity": "testing"})
        self.assertEqual(len(RUN.status(self.root)["state"]["history"]), 2)

    def test_running_interval_extends_to_read_time_paused_interval_does_not_execute(self):
        state = self.start(initial={"stage": "implementation", "activity": "execution"})
        self.assertEqual(self.timing(10)["tracked_seconds"], 10)
        self.finish(state, 15, "paused")
        result = self.timing(100, state["run_id"])
        self.assertEqual(result["tracked_seconds"], 15)
        self.assertEqual(result["paused_seconds"], 85)
        self.assertEqual(result["coverage"], "complete")

    def test_legacy_state_no_fabrication_from_updated_at(self):
        state = self.start()
        path = self.base / "runs" / f'{state["run_id"]}.json'
        legacy = dict(state)
        legacy.pop("created_at")
        legacy.pop("activity")
        legacy["history"] = []
        legacy["updated_at"] = self.at(-500)
        path.write_text(json.dumps(legacy))
        result = self.timing(10)
        self.assertEqual(result["coverage"], "unknown")
        self.assertEqual(result["tracked_seconds"], 0)
        self.update(state, 20, {"pending": ["no-timeline-change"]})
        self.assertEqual(self.timing(30)["tracked_seconds"], 0)
        self.update(state, 40, {"stage": "known-now", "activity": "testing"})
        result = self.timing(50)
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["tracked_seconds"], 10)
        self.assertNotIn("created_at", RUN.status(self.root)["state"])

    def test_legacy_resume_only_tracks_from_new_observed_boundary(self):
        state = self.start()
        self.finish(state, 10, "paused")
        path = self.base / "runs" / f'{state["run_id"]}.json'
        legacy = json.loads(path.read_text())
        legacy.pop("created_at")
        legacy.pop("activity")
        legacy["history"] = [{"event": "paused", "at": self.at(10), "stop_evidence": "old observation"}]
        path.write_text(json.dumps(legacy))
        self.resume(state, 40)
        result = self.timing(50)
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["tracked_seconds"], 10)
        self.assertEqual(result["paused_seconds"], 30)

    def test_recovering_running_state_does_not_count_unknown_interruption_gap(self):
        state = self.start()
        self.update(state, 10, {"stage": "work"})
        self.resume(state, 100)
        result = self.timing(110)
        self.assertEqual(result["tracked_seconds"], 20)
        self.assertEqual(result["coverage"], "partial")

    def test_aborted_terminal_has_complete_boundary_and_no_post_abort_tail(self):
        state = self.start()
        self.finish(state, 15, "aborted")
        for read_at in (15, 100, 1000):
            result = self.timing(read_at, state["run_id"])
            self.assertEqual(result["coverage"], "complete")
            self.assertEqual(result["reason"], "observed_timeline")
            self.assertEqual(result["tracked_seconds"], 15)
            self.assertEqual(result["paused_seconds"], 0)
            self.assertEqual(result["activity_seconds"]["execution"], 15)

    def test_explicit_resume_after_abort_accounts_gap_only_as_nonexecution(self):
        state = self.start()
        self.finish(state, 15, "aborted")
        self.resume(state, 100)
        self.finish(state, 120)
        result = self.timing(1000, state["run_id"])
        self.assertEqual(result["coverage"], "complete")
        self.assertEqual(result["tracked_seconds"], 35)
        self.assertEqual(result["paused_seconds"], 85)
        self.assertEqual(result["activity_seconds"]["execution"], 35)

    def test_unknown_or_mismatched_active_owner_does_not_count_live_tail(self):
        state = self.start()
        marker = self.base / ".active"
        original = json.loads(marker.read_text())
        altered = dict(original, owner_token="not-this-owner")
        marker.write_text(json.dumps(altered))
        result = self.timing(100)
        self.assertEqual(result["coverage"], "unknown")
        self.assertEqual(result["reason"], "active_owner_mismatch")
        self.assertEqual(result["tracked_seconds"], 0)
        marker.unlink()
        result = self.timing(100, state["run_id"])
        self.assertEqual(result["coverage"], "unknown")
        self.assertEqual(result["tracked_seconds"], 0)
        marker.write_text("legacy-unknown-owner")
        self.assertEqual(self.timing(100)["reason"], "active_owner_unrecognized")

    def test_timing_function_and_cli_are_read_only_and_exclude_owner_token(self):
        empty = self.timing(0)
        self.assertEqual(empty["coverage"], "unknown")
        self.assertEqual(list(self.root.iterdir()), [])
        state = self.start()
        before = {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.rglob("*") if path.is_file()}
        result = self.timing(10)
        self.assertNotIn(state["owner_token"], json.dumps(result))
        output = subprocess.run([sys.executable, str(SCRIPTS / "run_state.py"), "--project", str(self.root), "--timing"],
                                capture_output=True, text=True)
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertNotIn("owner_token", output.stdout)
        self.assertNotIn(state["owner_token"], output.stdout)
        self.assertEqual(json.loads(output.stdout)["run_id"], state["run_id"])
        after = {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_failed_transition_and_finish_do_not_leave_phantom_timing_events(self):
        state = self.start()
        with patch.object(RUN.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.update(state, 10, {"stage": "not-written", "activity": "testing"})
            with self.assertRaises(OSError):
                self.finish(state, 20)
        self.assertEqual(RUN.status(self.root)["state"], state)
        result = self.timing(30)
        self.assertEqual(result["stage_seconds"], {"start": 30})
        self.assertEqual(result["activity_seconds"]["execution"], 30)

    def test_invalid_activity_and_stage_refused_without_mutation(self):
        state = self.start()
        for changes in ({"activity": "parallel_role_total"}, {"activity": []}, {"stage": ""}, {"stage": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.update(state, 10, changes)
        self.assertEqual(RUN.status(self.root)["state"], state)

    def test_nonmonotonic_or_invalid_history_does_not_produce_negative_or_fake_totals(self):
        state = self.start()
        self.update(state, 10, {"stage": "testing", "activity": "testing"})
        path = self.base / "runs" / f'{state["run_id"]}.json'
        saved = json.loads(path.read_text())
        for malformed in (dict(saved["history"][-1], at=self.at(-1)),
                          dict(saved["history"][-1], at="invalid-date"),
                          {"event": "unknown", "at": self.at(5)}):
            altered = dict(saved, history=[saved["history"][0], malformed])
            path.write_text(json.dumps(altered))
            result = self.timing(20)
            self.assertEqual(result["coverage"], "unknown")
            self.assertEqual(result["reason"], "invalid_timeline")
            self.assertEqual(result["tracked_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
