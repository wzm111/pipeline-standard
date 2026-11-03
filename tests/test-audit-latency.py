"""Anonymous latency metadata is task bounded and never promotes evidence."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import run_audit as A
import run_evidence as E
import run_latency as L
import run_state as R


def stamp(seconds):
    from datetime import datetime, timezone, timedelta
    return (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


class LatencyTests(unittest.TestCase):
    def state(self):
        def task(name, start, end):
            return {"task_id": name, "goal": "SECRET", "acceptance": ["PRIVATE"],
                    "started_at": stamp(start), "ended_at": stamp(end), "status": "execution_ended", "report_refs": []}
        return {"created_at": stamp(0), "updated_at": stamp(100), "status": "completed",
                "tasks": [task("first", 0, 50), task("second", 50, 100)],
                "history": [{"event": event, "at": stamp(at), "activity": activity}
                            for event, at, activity in [("start", 0, "execution"), ("transition", 30, "human_wait"),
                                ("transition", 40, "execution"), ("transition", 80, "role_wait"),
                                ("transition", 90, "handoff"), ("transition", 95, "execution"), ("completed", 100, "execution")]],
                "operation_spans": [{"id": f"{i:032x}", "category": "verify", "label": "safe",
                                     "start": stamp(a), "end": stamp(b), "status": "completed"}
                                    for i, (a, b) in enumerate([(5, 25), (50, 80)])]}

    def record(self, start, end, **extra):
        return {"kind": "check", "recorded_at": stamp(end), "started_at": stamp(start),
                "finished_at": stamp(end), "inherited": False, "failure": False,
                "environment_mismatch": False, **extra}

    def test_parallel_nested_intervals_are_unioned_and_tasks_separated(self):
        value = L.summarize(self.state(), [self.record(0, 20), self.record(10, 30), self.record(55, 65)])
        first, second = value["samples"]
        self.assertEqual((first["wall_seconds"], second["wall_seconds"]), (50, 50))
        self.assertEqual((first["check_union_seconds"], second["check_union_seconds"]), (30, 10))
        self.assertEqual((first["observed_union_seconds"], second["observed_union_seconds"]), (40, 45))
        self.assertEqual((first["unattributed_seconds"], second["unattributed_seconds"]), (10, 5))
        L.validate(value)
        self.assertNotIn("SECRET", json.dumps(value))

    def test_inheritance_missing_timing_and_future_records_do_not_invent_time(self):
        records = [self.record(0, 20, inherited=True), self.record(10, 20, started_at=None), self.record(200, 201)]
        value = L.summarize(self.state(), records)
        self.assertEqual(value["coverage"], "partial")
        self.assertIsNone(value["samples"][0]["check_union_seconds"])
        self.assertEqual(value["samples"][0]["invalid_timing_records"], 1)

    def test_candidates_are_evidence_codes_not_business_details_or_waste(self):
        value = L.summarize(self.state(), [self.record(10, 20, failure=True, environment_mismatch=True),
                                         {"kind": "check_reuse", "recorded_at": stamp(50)}])
        first, second = value["samples"]
        self.assertEqual(first["failure_records"], 1)
        self.assertEqual(first["environment_mismatch_records"], 1)
        self.assertIn("review_failed_checks", first["review_candidates"])
        self.assertEqual((first["reuse_records"], second["reuse_records"]), (0, 1))
        self.assertNotIn("waste", json.dumps(value))

    def test_shared_boundary_with_zero_duration_tasks_has_one_latest_owner(self):
        state = self.state()
        zero = copy.deepcopy(state["tasks"][1])
        zero.update(task_id="zero", ended_at=stamp(50))
        state["tasks"].insert(1, zero)
        value = L.summarize(state, [{"kind": "check_reuse", "recorded_at": stamp(50)}])
        self.assertEqual([s["reuse_records"] for s in value["samples"]], [0, 0, 1])

    def test_unknown_invalid_history_and_pending_operations_remain_partial(self):
        state = self.state()
        state["history"][2]["at"] = stamp(10)
        state["operation_spans"].append({"id": "a" * 32, "category": "read", "label": "safe",
                                         "start": stamp(98), "end": None, "status": "pending"})
        value = L.summarize(state, [])
        self.assertEqual(value["coverage"], "partial")
        self.assertIsNone(value["samples"][0]["wait_union_seconds"])
        self.assertEqual(value["samples"][1]["pending_operation_records"], 1)
        self.assertEqual(L.summarize({}, [])["coverage"], "unknown")

    def test_no_task_uses_recorded_run_boundary_not_scan_time(self):
        state = self.state()
        state.pop("tasks")
        state["status"] = "running"
        value = L.summarize(state, [])
        self.assertEqual(value["samples"][0]["index"], 0)
        self.assertEqual(value["samples"][0]["wall_seconds"], 100)

    def test_privacy_closed_validation_rejects_free_text_and_nonfinite_values(self):
        good = L.summarize(self.state(), [])
        for mutate in [lambda v: v["samples"][0].update(command="SECRET"),
                       lambda v: v["samples"][0].update(review_candidates=["SECRET"]),
                       lambda v: v["samples"][0].update(wall_seconds=float("nan")),
                       lambda v: v["samples"][0].update(check_records=True)]:
            value = copy.deepcopy(good)
            mutate(value)
            with self.assertRaises(ValueError):
                L.validate(value)

    def test_audit_integration_never_reads_raw_inputs_or_logs_and_old_snapshot_remains_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = R.start(root)
            args = root, state["run_id"], state["owner_token"]
            (root / "raw.log").write_text("PRIVATE")
            (root / "input").write_text("PRIVATE")
            now = R._now()
            E.write_report(*args, "check", "observed", {"started_at": now, "finished_at": now,
                           "exit_code": 1, "result_class": "fixture_failure", "log_path": "raw.log",
                           "scope": ["input"], "command": "PRIVATE", "environment": {"SECRET": "PRIVATE"},
                           "reuse_decision": {"environment_changes": {"status": "known", "changed_keys": ["SHLVL"]}}})
            original = Path.open
            def guarded(path, *a, **kw):
                if path in {root / "raw.log", root / "input"}:
                    raise AssertionError("raw content read")
                return original(path, *a, **kw)
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
            with patch.object(Path, "open", guarded):
                value = A.audit(root, args[1], live=True)
            self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()})
            self.assertEqual(value["live"]["latency"]["samples"][0]["failure_records"], 1)
            self.assertEqual(value["live"]["latency"]["samples"][0]["environment_mismatch_records"], 1)
            self.assertNotIn("PRIVATE", json.dumps(value))
            self.assertNotIn("SECRET", json.dumps(value))
            self.assertNotIn(args[2], json.dumps(value))
            A._validate_document(value["live"])
            old = value["live"].copy()
            old.pop("latency")
            A._validate_document(old)


if __name__ == "__main__":
    unittest.main()
