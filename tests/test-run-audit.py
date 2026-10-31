"""Safe lifecycle audit behavior, using self-cleaning isolated projects only."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"
sys.path.insert(0, str(SCRIPTS))
import run_audit as A
import run_state as R
import run_evidence as E
import run_tasks as T


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def finish(self, state, outcome="completed"):
        return R.finish(self.root, state["run_id"], state["owner_token"], outcome,
                        executors_stopped=True, stop_evidence="SECRET stop observation")

    def target(self, state):
        return self.root / f"tmp/pipeline/runs/{state['run_id']}/audit.json"

    def files(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()}

    def test_start_finish_resume_and_state_compatibility(self):
        state = R.start(self.root)
        self.assertEqual(R.status(self.root)["state"], state)
        result = A.audit(self.root, state["run_id"])
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["audit"]["completeness"], "partial")
        self.assertEqual(result["audit"]["timing"]["tracked_seconds"], 0)
        paused = self.finish(state, "paused")
        self.assertEqual(A.audit(self.root, state["run_id"])["audit"]["status"], "paused")
        resumed = R.resume(self.root, state["run_id"], state["owner_token"],
                           executors_stopped=True, stop_evidence="explicit observation")
        self.assertEqual(A.audit(self.root, state["run_id"])["audit"]["status"], "running")
        self.finish(resumed)
        result = A.audit(self.root, state["run_id"])
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["audit"]["status"], "completed")
        self.assertIsNone(result["audit"]["actual_model"])
        self.assertIsNone(result["audit"]["tokens"])

    def test_stale_interruption_live_and_legacy_are_read_only(self):
        state = R.start(self.root)
        R.update(self.root, state["run_id"], state["owner_token"], {"activity": "human_wait", "stage": "SECRET business"})
        before = self.files()
        result = A.audit(self.root, state["run_id"], live=True)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["live"]["status"], "running")
        self.assertEqual(result["live"]["completeness"], "partial")
        self.assertEqual(before, self.files())
        self.target(state).unlink()
        before = self.files()
        self.assertEqual(A.scan(self.root)["runs"][0]["status"], "missing")
        self.assertTrue(A.audit(self.root, state["run_id"], live=True)["legacy"])
        self.assertEqual(before, self.files())

    def test_no_project_created_by_empty_scan(self):
        self.assertEqual(A.scan(self.root)["runs"], [])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(A.scan(self.root, 101)["error"], "invalid_limit")

    def test_bounded_scan_orders_candidates_by_state_mtime(self):
        first = R.start(self.root)
        self.finish(first, "aborted")
        second = R.start(self.root)
        self.finish(second, "aborted")
        first_path = self.root / f"tmp/pipeline/runs/{first['run_id']}.json"
        second_path = self.root / f"tmp/pipeline/runs/{second['run_id']}.json"
        newest = second_path.stat().st_mtime_ns + 1000000000
        os.utime(first_path, ns=(newest, newest))
        before = self.files()
        result = A.scan(self.root, 1)
        self.assertEqual(result["runs"][0]["run_id"], first["run_id"])
        self.assertTrue(result["truncated"])
        self.assertEqual(before, self.files())

    def test_timeline_nonoverlap_and_unknown_old_data(self):
        state = {"created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:10+00:00", "status": "completed",
                 "history": [{"event": "start", "at": "2026-01-01T00:00:00+00:00", "activity": "execution"},
                             {"event": "transition", "at": "2026-01-01T00:00:04+00:00", "activity": "testing"},
                             {"event": "completed", "at": "2026-01-01T00:00:10+00:00", "activity": "execution"}]}
        result = A._timeline(state)
        self.assertEqual(result["tracked_seconds"], 10)
        self.assertEqual(result["activity_seconds"]["execution"], 4)
        self.assertEqual(result["activity_seconds"]["testing"], 6)
        state.pop("history")
        self.assertIsNone(A._timeline(state)["tracked_seconds"])

    def test_safe_metadata_counts_no_raw_or_lineage_reads(self):
        state = R.start(self.root)
        run, owner = state["run_id"], state["owner_token"]
        T.start(self.root, run, owner, "secret-business", "SECRET GOAL", ["SECRET ACCEPTANCE"])
        T.end(self.root, run, owner, "secret-business")
        first = E.write_report(self.root, run, owner, "check", "first", {"result_class": "test_failure", "command": ["SECRET"], "stdout": "SECRET"})
        E.write_report(self.root, run, owner, "check_review", "review", {"source": first, "result_class": "environment_failure", "reason": "SECRET"})
        E.write_report(self.root, run, owner, "check_reuse", "reuse", {"source": first})
        E.write_report(self.root, run, owner, "check", "inherited", {"inherited_from": {"SECRET": "not_followed"}})
        with patch.object(E, "validate_lineage", side_effect=AssertionError("must not validate")):
            self.finish(state, "aborted")
        result = A.audit(self.root, run)
        text = json.dumps(result)
        for sensitive in ("SECRET", "secret-business", owner, str(self.root), '"stdout":', '"command":'):
            self.assertNotIn(sensitive, text)
        audit = result["audit"]
        self.assertEqual(audit["tasks"]["count"], 1)
        self.assertEqual(audit["tasks"]["boundaries"][0]["status"], "execution_ended")
        self.assertIsNotNone(audit["tasks"]["boundaries"][0]["ended_at"])
        self.assertEqual(audit["checks"]["executed"], 1)
        self.assertEqual(audit["checks"]["reused"], 1)
        self.assertEqual(audit["checks"]["inherited"], 1)
        self.assertEqual(audit["checks"]["by_raw_result_class"]["test_failure"], 1)
        self.assertEqual(audit["checks"]["by_result_class"]["environment_failure"], 1)

    def test_missing_and_bad_reports_are_partial_not_fabricated_success(self):
        state = R.start(self.root)
        ref = E.write_report(self.root, state["run_id"], state["owner_token"], "check", "bad", {})
        (self.root / ref).write_text("not json")
        E.write_report(self.root, state["run_id"], state["owner_token"], "check", "missing", {})
        (self.root / ref.replace("bad.json", "missing.json")).unlink()
        audit = A.audit(self.root, state["run_id"], live=True)["live"]
        self.assertEqual(audit["checks"]["unavailable"], 2)
        self.assertEqual(audit["checks"]["coverage"], "partial")

    def test_plan_and_planned_check_are_not_unavailable_check_reports(self):
        state = R.start(self.root)
        run, owner = state["run_id"], state["owner_token"]
        for kind in ("check_plan", "planned_check", "qa"):
            E.write_report(self.root, run, owner, kind, "plan", {})
        E.write_report(self.root, run, owner, "check", "actual", {"result_class": "success"})
        checks = A.audit(self.root, run, live=True)["live"]["checks"]
        self.assertEqual(checks["executed"], 1)
        self.assertEqual(checks["unavailable"], 0)
        self.assertEqual(checks["coverage"], "recorded_only")

    def test_shared_log_candidates_do_not_merge_checks_or_coverage(self):
        state = R.start(self.root)
        run, owner = state["run_id"], state["owner_token"]
        payload = {"command": "SECRET browser", "scope": ["SECRET fixture"],
                   "environment": {"runtime": "SECRET"}, "fingerprint": "a" * 64,
                   "log_path": "SECRET.log", "log_sha256": "b" * 64,
                   "result_class": "success", "exit_code": 0, "observation_kind": "host_tool"}
        E.write_report(self.root, run, owner, "check", "first", {**payload, "observation_id": "first", "coverage": ["browser"]})
        E.write_report(self.root, run, owner, "check", "second", {**payload, "observation_id": "second", "coverage": ["visual"]})
        before = self.files()
        with patch.object(E, "validate_lineage", side_effect=AssertionError("metadata only")), \
                patch.object(E, "fingerprint", side_effect=AssertionError("no project scans")):
            checks = A.audit(self.root, run, live=True)["live"]["checks"]
        self.assertEqual(checks["check_records"], 2)
        self.assertEqual(checks["host_observation_records"], 2)
        self.assertEqual(checks["shared_log_source_candidates"], 1)
        self.assertEqual(checks["executed"], 2)  # Two IDs remain two reported invocations.
        self.assertEqual(checks["source_identity_unknown"], 0)
        self.assertNotIn("SECRET", json.dumps(checks))
        self.assertEqual(before, self.files())

    def test_shared_log_identity_changes_and_unknown_legacy_stay_distinct(self):
        state = R.start(self.root)
        run, owner = state["run_id"], state["owner_token"]
        payload = {"command": "browser", "scope": ["fixture"], "environment": {"runtime": "fixture"},
                   "fingerprint": "a" * 64, "log_path": "result.log", "log_sha256": "b" * 64,
                   "result_class": "success", "exit_code": 0}
        variations = [{}, {"log_path": "other.log"}, {"fingerprint": "c" * 64},
                      {"environment": {"runtime": "other"}}, {"cwd": "other"}, {"intent": "check"}]
        for index, delta in enumerate(variations):
            E.write_report(self.root, run, owner, "check", f"case-{index}", {**payload, **delta})
        E.write_report(self.root, run, owner, "check", "legacy", {"exit_code": 0})
        checks = A.audit(self.root, run, live=True)["live"]["checks"]
        self.assertEqual(checks["check_records"], 7)
        self.assertEqual(checks["shared_log_source_candidates"], 0)
        self.assertEqual(checks["source_identity_unknown"], 1)
        self.assertEqual(checks["executed"], 7)

    def test_old_summary_without_source_counters_remains_readable(self):
        state = R.start(self.root)
        document = json.loads(self.target(state).read_text())
        for key in ("check_records", "host_observation_records", "shared_log_source_candidates", "source_identity_unknown"):
            document["checks"].pop(key)
        self.target(state).write_text(json.dumps(document))
        self.assertEqual(A.audit(self.root, state["run_id"])["status"], "current")

    def test_incomplete_environment_identity_is_unknown_not_shared(self):
        state = R.start(self.root)
        run, owner = state["run_id"], state["owner_token"]
        payload = {"command": "browser", "scope": ["fixture"], "fingerprint": "a" * 64,
                   "log_path": "result.log", "log_sha256": "b" * 64, "exit_code": 0}
        for index, env in enumerate(({}, {"runtime": None}, {"runtime": ""}, {"runtime": 4})):
            E.write_report(self.root, run, owner, "check", f"case-{index}", {**payload, "environment": env})
        checks = A.audit(self.root, run, live=True)["live"]["checks"]
        self.assertEqual(checks["source_identity_unknown"], 4)
        self.assertEqual(checks["shared_log_source_candidates"], 0)
        self.assertEqual(checks["executed"], 4)

    def test_latest_registered_review_wins_across_hash_seeds(self):
        state = R.start(self.root)
        run, owner = state["run_id"], state["owner_token"]
        first = E.write_report(self.root, run, owner, "check", "failed", {"result_class": "test_failure"})
        E.write_report(self.root, run, owner, "check_review", "older", {"source": first, "result_class": "environment_failure"})
        E.write_report(self.root, run, owner, "check_review", "newer", {"source": first, "result_class": "fixture_failure"})
        command = [sys.executable, "-B", "-c",
                   "import sys,json;sys.path.insert(0,sys.argv[1]);import run_audit as A;"
                   "print(json.dumps(A.audit(sys.argv[2],sys.argv[3],live=True)['live']['checks']))",
                   str(SCRIPTS), str(self.root), run]
        for seed in range(16):
            with self.subTest(seed=seed):
                result = subprocess.run(command, capture_output=True, text=True,
                                        env={**os.environ, "PYTHONHASHSEED": str(seed)})
                self.assertEqual(result.returncode, 0, result.stderr)
                counts = json.loads(result.stdout)
                self.assertEqual(counts["by_result_class"]["fixture_failure"], 1)
                self.assertEqual(counts["reviews"], 2)

    def test_check_envelope_requires_valid_recorded_time(self):
        state = R.start(self.root)
        ref = E.write_report(self.root, state["run_id"], state["owner_token"], "check", "timestamp", {"result_class": "success"})
        target = self.root / ref
        report = json.loads(target.read_text())
        for timestamp in (None, "SECRET invalid time", "2026-01-01T00:00:00"):
            with self.subTest(timestamp=timestamp):
                if timestamp is None:
                    report.pop("recorded_at", None)
                else:
                    report["recorded_at"] = timestamp
                target.write_text(json.dumps(report))
                counts = A.audit(self.root, state["run_id"], live=True)["live"]["checks"]
                self.assertEqual(counts["executed"], 0)
                self.assertEqual(counts["unavailable"], 1)
                self.assertEqual(counts["coverage"], "partial")

    def test_symlink_and_tampered_summary_refused_without_overwrite(self):
        state = R.start(self.root)
        target = self.target(state)
        outside = self.root / "outside.json"
        outside.write_text("SECRET external")
        target.unlink()
        target.symlink_to(outside)
        self.assertEqual(A.audit(self.root, state["run_id"])["error"], "unsafe_path")
        with self.assertWarnsRegex(RuntimeWarning, "unsafe_path"):
            self.finish(state, "aborted")
        self.assertEqual(outside.read_text(), "SECRET external")
        target.unlink()
        target.write_text(json.dumps({"schema_version": 1, "run_id": "a" * 32}))
        before = target.read_bytes()
        with self.assertWarnsRegex(RuntimeWarning, "identity_mismatch"):
            R.resume(self.root, state["run_id"], state["owner_token"], executors_stopped=True, stop_evidence="observed")
        self.assertEqual(target.read_bytes(), before)

    def test_storage_failure_does_not_change_success_or_delete_other_lock(self):
        import warnings
        with patch.object(A, "_build", side_effect=PermissionError("SECRET path")):
            with self.assertWarnsRegex(RuntimeWarning, "permission_denied"):
                state = R.start(self.root)
        self.assertEqual(R.status(self.root)["state"], state)
        self.assertEqual(A.audit(self.root, state["run_id"])["status"], "missing")
        lock = self.root / ".pipeline.models.lock"
        lock.write_text("other owner")
        self.assertEqual(A.audit(self.root, state["run_id"], live=True)["status"], "missing")
        self.assertEqual(lock.read_text(), "other owner")
        lock.unlink()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with patch.object(A, "_build", side_effect=OSError("SECRET")):
                self.assertEqual(self.finish(state, "aborted")["status"], "aborted")
        self.assertFalse((self.root / "tmp/pipeline/.active").exists())

    def test_untrusted_audit_strings_and_id_never_echoed(self):
        state = R.start(self.root)
        document = json.loads(self.target(state).read_text())
        document["actual_model"] = "SECRET credential"
        self.target(state).write_text(json.dumps(document))
        result = A.audit(self.root, state["run_id"])
        self.assertEqual(result["error"], "invalid_audit")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertIsNone(A.audit(self.root, "SECRET")["run_id"])

    def test_concurrent_reads_only_see_atomic_snapshots(self):
        state = R.start(self.root)
        def read(_):
            return A.audit(self.root, state["run_id"])["status"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(read, range(20)))
        self.assertEqual(set(results), {"current"})

    def test_installed_layouts_and_both_readonly_cli_entries(self):
        state = R.start(self.root)
        before = self.files()
        for runtime in ("claude", "codex"):
            with tempfile.TemporaryDirectory() as install:
                import shutil
                scripts = Path(install) / (".claude" if runtime == "claude" else ".agents") / "skills/pipeline/scripts"
                shutil.copytree(SCRIPTS, scripts, ignore=shutil.ignore_patterns("__pycache__"))
                for script in ("pipeline_cli.py", "run_audit.py"):
                    result = subprocess.run([sys.executable, "-B", str(scripts / script), "audit", "--project", str(self.root),
                                             "--run-id", state["run_id"], "--live"], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout)["status"], "current")
        self.assertEqual(before, self.files())


if __name__ == "__main__":
    unittest.main()
