"""Explicit operation timing in isolated projects; real tiny check integration."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import operation_metrics as O
import run_checks as C
import run_state as R


class OperationMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.at_call(0, R.start, self.root)

    def at(self, seconds):
        return (datetime(2026, 10, 27, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()

    def at_call(self, seconds, callback, *args, **kwargs):
        with patch.object(R, "_now", return_value=self.at(seconds)):
            return callback(*args, **kwargs)

    def begin(self, seconds, category="read", label="fixture"):
        return self.at_call(seconds, O.begin, self.root, self.state["run_id"], self.state["owner_token"], category, label)

    def end(self, seconds, span_id, **kwargs):
        return self.at_call(seconds, O.end, self.root, self.state["run_id"], self.state["owner_token"], span_id, **kwargs)

    def summary(self, seconds):
        return self.at_call(seconds, O.summary, self.root, self.state["run_id"])

    def spans(self):
        return R.status(self.root)["state"].get("operation_spans", [])

    def files(self):
        return {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.rglob("*") if path.is_file()}

    def test_parallel_nested_spans_use_category_and_total_unions(self):
        read = self.begin(0)
        verify = self.begin(2, "verify")
        network = self.begin(5, "network")
        self.end(8, verify)
        self.end(10, read)
        git = self.begin(10, "git")
        self.end(20, network)
        self.end(25, git)
        result = self.summary(30)
        self.assertEqual(result["category_seconds"]["read"], 10)
        self.assertEqual(result["category_seconds"]["verify"], 6)
        self.assertEqual(result["category_seconds"]["network"], 15)
        self.assertEqual(result["category_seconds"]["git"], 15)
        self.assertIsNone(result["category_seconds"]["write"])
        self.assertEqual(result["attributed_seconds"], 25)
        self.assertEqual(result["unattributed_seconds"], 5)
        self.assertEqual(result["run_tracked_seconds"], 30)
        self.assertEqual(result["coverage"], "partial")

    def test_call_and_context_return_values_without_raw_arguments_or_content(self):
        args = (self.root, self.state["run_id"], self.state["owner_token"])
        with patch.object(R, "_now", side_effect=[self.at(5), self.at(10)]):
            result = O.call(*args, "read", "safe-read", lambda secret: {"answer": secret}, "private-body")
        self.assertEqual(result, {"answer": "private-body"})
        with patch.object(R, "_now", side_effect=[self.at(15), self.at(20)]):
            with O.span(*args, "write", "safe-write") as span_id:
                self.assertIsInstance(span_id, str)
        encoded = json.dumps(self.spans())
        self.assertNotIn("private-body", encoded)
        self.assertNotIn("owner_token", encoded)
        self.assertNotIn(self.state["owner_token"], encoded)
        self.assertEqual([span["status"] for span in self.spans()], ["completed", "completed"])
        self.assertTrue(all(set(span) == {"id", "category", "label", "start", "end", "status"} for span in self.spans()))

    def test_callback_exception_and_interrupt_are_recorded_then_same_exception_is_raised(self):
        args = (self.root, self.state["run_id"], self.state["owner_token"])
        failure = RuntimeError("secret payload must not be persisted")
        def failed():
            raise failure
        with patch.object(R, "_now", side_effect=[self.at(1), self.at(2)]):
            with self.assertRaises(RuntimeError) as caught:
                O.call(*args, "other", "failed", failed)
        self.assertIs(caught.exception, failure)
        interruption = KeyboardInterrupt()
        with patch.object(R, "_now", side_effect=[self.at(3), self.at(4)]):
            with self.assertRaises(KeyboardInterrupt):
                with O.span(*args, "handoff", "interrupted"):
                    raise interruption
        self.assertEqual([span["status"] for span in self.spans()], ["failed", "interrupted"])
        self.assertNotIn(str(failure), json.dumps(self.spans()))

    def test_recording_failure_never_masks_the_original_callback_error(self):
        failure = RuntimeError("primary callback failure")
        def failed():
            raise failure
        with patch.object(O, "end", side_effect=OSError("storage unavailable")):
            with self.assertRaises(RuntimeError) as caught:
                self.at_call(5, O.call, self.root, self.state["run_id"], self.state["owner_token"], "other", "failed", failed)
        self.assertIs(caught.exception, failure)
        result = self.summary(10)
        self.assertEqual(result["pending_span_count"], 1)
        self.assertIsNone(result["attributed_seconds"])

    def test_end_is_idempotent_and_wrong_owner_or_bad_label_never_writes(self):
        span_id = self.begin(1)
        finished = self.end(5, span_id)
        before = self.files()
        self.assertEqual(self.end(10, span_id, status="failed"), finished)
        self.assertEqual(before, self.files())
        for category, label in (("other", "../unsafe"), ("unknown", "fixture"), ("read", self.state["owner_token"])):
            with self.assertRaises(ValueError):
                self.begin(15, category, label)
        with self.assertRaises(ValueError):
            O.begin(self.root, self.state["run_id"], "wrong", "read", "fixture")
        with self.assertRaises(ValueError):
            O.end(self.root, self.state["run_id"], "wrong", span_id)
        self.assertEqual(before, self.files())

    def test_clock_cannot_move_backwards_for_begin_or_end(self):
        span_id = self.begin(10)
        before = self.files()
        with self.assertRaises(ValueError):
            self.end(5, span_id)
        with self.assertRaises(ValueError):
            self.begin(8)
        self.assertEqual(before, self.files())

    def test_pending_spans_are_unknown_and_never_extend_across_recovery(self):
        self.begin(5)
        self.at_call(20, R.resume, self.root, self.state["run_id"], self.state["owner_token"],
                     executors_stopped=True, stop_evidence="fixture has no processes")
        result = self.summary(40)
        self.assertEqual(result["pending_span_count"], 1)
        self.assertIsNone(result["attributed_seconds"])
        self.assertIsNone(result["category_seconds"]["read"])
        self.assertEqual(result["run_tracked_seconds"], 20)
        self.assertEqual(result["unattributed_seconds"], 20)

    def test_closed_span_crossing_pause_resume_is_interrupted_not_reused_as_work(self):
        old = self.begin(1)
        self.at_call(10, R.finish, self.root, self.state["run_id"], self.state["owner_token"], "paused",
                     executors_stopped=True, stop_evidence="fixture has no processes")
        with self.assertRaises(ValueError):
            self.end(15, old)
        self.at_call(20, R.resume, self.root, self.state["run_id"], self.state["owner_token"],
                     executors_stopped=True, stop_evidence="fixture recovery confirmed")
        self.assertEqual(self.end(30, old)["status"], "interrupted")
        current = self.begin(31, "write")
        self.end(35, current)
        result = self.summary(40)
        self.assertIsNone(result["category_seconds"]["read"])
        self.assertEqual(result["category_seconds"]["write"], 4)
        self.assertEqual(result["attributed_seconds"], 4)
        self.assertEqual(result["run_tracked_seconds"], 30)
        self.assertEqual(result["unattributed_seconds"], 26)

    def test_legacy_missing_run_timeline_stays_unknown_and_queries_never_write(self):
        span_id = self.begin(1)
        self.end(5, span_id)
        state_path = self.root / "tmp/pipeline/runs" / f'{self.state["run_id"]}.json'
        state = json.loads(state_path.read_text())
        state.pop("created_at")
        state["history"] = []
        state_path.write_text(json.dumps(state))
        before = self.files()
        result = self.summary(20)
        self.assertEqual(result["coverage"], "unknown")
        self.assertIsNone(result["attributed_seconds"])
        self.assertIsNone(result["unattributed_seconds"])
        self.assertEqual(before, self.files())
        self.assertNotIn("owner_token", json.dumps(result))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(self.at_call(20, O.summary, root)["coverage"], "unknown")
            self.assertEqual(list(root.iterdir()), [])

    def test_real_check_execution_and_reuse_each_record_one_end_to_end_span(self):
        # Real operations use the real clock, after a fixed earlier run start.
        (self.root / "source.py").write_text("fixture input")
        argv = [sys.executable, "-c", "print('private-command-body')"]
        args = (self.root, self.state["run_id"], self.state["owner_token"])
        first = C.execute_check(*args, "first", argv=argv, scope=["source.py"], impact_covered=True)
        second = C.execute_check(*args, "second", argv=argv, scope=["source.py"], impact_covered=True)
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertGreater(first["seconds"], 0)
        self.assertEqual(second["seconds"], 0)
        spans = self.spans()
        self.assertEqual(len(spans), 2)
        self.assertEqual([span["status"] for span in spans], ["executed", "reused"])
        total = (R._timestamp(spans[0]["end"]) - R._timestamp(spans[0]["start"])).total_seconds()
        self.assertGreaterEqual(total, first["seconds"])
        self.assertNotIn("private-command-body", json.dumps(spans))
        self.assertEqual([span["category"] for span in spans], ["verify", "verify"])
        self.assertEqual(R.status(self.root)["state"]["running_checks"], [])

    def test_check_validation_failures_record_failed_without_changing_original_error(self):
        args = (self.root, self.state["run_id"], self.state["owner_token"])
        with self.assertRaisesRegex(ValueError, "argv"):
            C.execute_check(*args, "invalid-argv", argv="not-a-list", scope=["input"])
        self.assertEqual(self.spans()[0]["status"], "failed")
        (self.root / "input").write_text("fixture")
        failed = C.execute_check(*args, "nonzero", argv=[sys.executable, "-c", "raise SystemExit(1)"], scope=["input"])
        self.assertEqual(failed["exit_code"], 1)
        self.assertEqual(self.spans()[-1]["status"], "failed")
        self.assertEqual(R.status(self.root)["state"]["running_checks"], [])

    def test_check_cleanup_error_is_preserved_while_span_records_failure(self):
        (self.root / "input").write_text("fixture")
        failure = RuntimeError("original cleanup failure")
        with patch.object(C, "_release", side_effect=failure):
            with self.assertRaises(RuntimeError) as caught:
                C.execute_check(self.root, self.state["run_id"], self.state["owner_token"], "cleanup",
                                argv=[sys.executable, "-c", "print('ok')"], scope=["input"])
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.spans()[-1]["status"], "failed")

    def test_expected_search_and_reproduction_nonzero_are_executed_not_operation_failures(self):
        (self.root / "input").write_text("fixture")
        for intent in ("search", "reproduce"):
            result = C.execute_check(self.root, self.state["run_id"], self.state["owner_token"], intent,
                                     argv=[sys.executable, "-c", "raise SystemExit(1)"], scope=["input"], intent=intent)
            self.assertEqual(result["exit_code"], 1)
            self.assertEqual(self.spans()[-1]["status"], "executed")
        self.assertEqual([span["category"] for span in self.spans()], ["locate", "verify"])


if __name__ == "__main__":
    unittest.main()
