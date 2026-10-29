"""Release attribution is optional, read-only, and never promotes acceptance."""
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import operation_metrics as O
import release_timing as T
import run_checks as C
import run_evidence as E
import run_state as R


class ReleaseTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "PIPELINE.md").write_text("isolated fixture")
        (self.root / "source").write_text("fixture")
        state = R.start(self.root)
        self.args = self.root, state["run_id"], state["owner_token"]
        self.origin = R._timestamp(state["created_at"])

    def at(self, seconds):
        return patch.object(R, "_now", return_value=(self.origin + timedelta(seconds=seconds)).isoformat())

    def snapshot(self):
        return {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_nested_steps_union_and_read_only_summary_with_unknown_remainder(self):
        with self.at(1):
            outer = T.step(*self.args, "gates")
            outer.__enter__()
        with self.at(3):
            inner = T.step(*self.args, "build")
            inner.__enter__()
        with self.at(7):
            inner.__exit__(None, None, None)
        with self.at(10):
            outer.__exit__(None, None, None)
        with self.at(12):
            before = self.snapshot()
            result = T.summary(self.root, self.args[1])
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(result["measured_step_seconds"], 9)
        self.assertEqual(result["step_seconds"]["gates"], 9)
        self.assertEqual(result["step_seconds"]["build"], 4)
        self.assertIsNone(result["step_seconds"]["dependencies"])
        self.assertIsNone(result["command_seconds"])
        self.assertNotIn("release_plan", R.status(self.root)["state"])
        self.assertFalse(R.status(self.root)["state"]["artifacts"])

    def test_failed_step_retained_pending_unknown_and_invalid_owner_rejected(self):
        with self.at(1):
            step = T.step(*self.args, "transfer")
            step.__enter__()
        with self.at(4):
            error = RuntimeError("fixture transfer failed")
            self.assertFalse(step.__exit__(RuntimeError, error, None))
        with self.at(5):
            O.begin(*self.args, "verify", "release_readiness")
        with self.at(8):
            result = T.summary(self.root, self.args[1])
        self.assertEqual(result["step_seconds"]["transfer"], 3)
        self.assertIsNone(result["step_seconds"]["readiness"])
        self.assertEqual(result["pending_steps"], ["readiness"])
        self.assertEqual(result["status_counts"]["failed"], 1)
        with self.assertRaises(ValueError), T.step(self.root, self.args[1], "wrong-owner", "build"):
            self.fail("must not run")
        with self.assertRaises(ValueError), T.step(*self.args, "invented"):
            self.fail("must not run")

    def test_monolithic_execution_is_measured_once_without_inventing_inner_steps(self):
        result = C.execute_check(*self.args, "deploy", argv=[sys.executable, "-c", "print('fixture only')"],
                                 scope=["source"], timeout=10, coverage=["fixture_release"])
        duplicate = C.execute_check(*self.args, "reuse", argv=[sys.executable, "-c", "print('fixture only')"],
                                    scope=["source"], timeout=10, coverage=["fixture_release"], impact_covered=True)
        self.assertTrue(duplicate["reused"])
        self.assertEqual(duplicate["report"], result["report"])
        before = self.snapshot()
        report = T.summary(self.root, self.args[1], check_ref=result["report"])
        self.assertEqual(report["coverage"], "partial")
        self.assertGreater(report["command_seconds"], 0)
        self.assertEqual(report["unattributed_command_seconds"], report["command_seconds"])
        self.assertTrue(all(v is None for v in report["step_seconds"].values()))
        self.assertEqual(before, self.snapshot())
        with self.assertRaises(ValueError):
            T.summary(self.root, self.args[1], check_ref="../other/check.json")

    def test_inherited_execution_is_not_counted_in_current_run(self):
        ref = E.write_report(*self.args, "check", "inherited", {"inherited_from": {"fixture": True},
                             "started_at": "2000-01-01T00:00:00Z", "finished_at": "2000-01-02T00:00:00Z"})
        result = T.summary(self.root, self.args[1], check_ref=ref)
        self.assertEqual(result["reused_reports_excluded"], 1)
        self.assertIsNone(result["command_seconds"])


if __name__ == "__main__":
    unittest.main()
