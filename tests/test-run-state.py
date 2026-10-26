"""Run lifecycle tests use isolated directories; never touch the real active run."""
from concurrent.futures import ThreadPoolExecutor
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


if __name__ == "__main__":
    unittest.main()
