"""Advisory boundaries use only local, self-cleaning fixtures; no model calls."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import expert_advice as A
import run_evidence as E
import run_state as R
import run_tasks as T


class ExpertAdviceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.tick = 0
        self.clock = patch.object(R, "_now", side_effect=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.s = R.start(self.root)
        self.args = self.root, self.s["run_id"], self.s["owner_token"]
        self.n = 0

    def now(self):
        self.tick += 1
        return f"2026-01-01T00:{self.tick // 60:02d}:{self.tick % 60:02d}+00:00"

    def state(self):
        return R.status(self.root)["state"]

    def check(self, **changes):
        self.n += 1
        log_ref = f"tmp/pipeline/log-{self.n}.txt"
        log = self.root / log_ref
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("observed diagnostic snippet", encoding="utf-8")
        payload = {"exit_code": 1, "intent": "test", "result_class": "test_failure", "inputs_stable": True,
                   "command": "python3 -B tests.py", "cwd": ".", "coverage": ["unit"],
                   "scope": ["source.py"], "environment": {"platform": "fixture"},
                   "fingerprint": "observed-input", "started_at": self.now(), "finished_at": self.now(),
                   "log_path": log_ref, "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest()}
        payload.update(changes)
        return E.write_report(*self.args, "check", f"check-{self.n}", payload)

    def review(self, kind="qa"):
        self.n += 1
        return E.write_report(*self.args, kind, f"review-{self.n}", {"gate": "CONCERNS", "finding": "caller asserted conflict"})

    def prepare(self, refs=None, case="cause-a", trigger="repeated_failure", brief="诊断、已尝试的修复及关键片段"):
        refs = refs if refs is not None else [self.check(), self.check()]
        return A.prepare(*self.args, case, trigger, refs, brief, local_fix_attempted=True)

    def reserve(self, case="cause-a", role="planner", native=None):
        with R._write_guard(self.root) as base:
            path, state = R._load_owned(base, self.s["run_id"], self.s["owner_token"])
            binding = A.require_dispatch(self.root, state, case, role, native)
            state.setdefault("role_dispatches", []).append({**binding, "status": "dispatching", "role": role})
            R._atomic(path, state)
        return binding

    def test_timing_requires_second_sequential_failure_and_explicit_local_fix(self):
        first = self.check()
        self.assertFalse(self.prepare([first])["eligible"])
        refs = [first, self.check()]
        self.assertEqual(A.prepare(*self.args, "cause-a", "repeated_failure", refs, "brief")["reason"], "local_fix_required")
        self.assertTrue(self.prepare(refs)["eligible"])
        self.assertEqual(A.summary(self.state())["attempts"], 0)

    def test_parallel_passed_fake_and_mismatched_reports_fail(self):
        for changes in ({"exit_code": 0}, {"intent": "search"}, {"result_class": "command_unavailable"},
                        {"inputs_stable": False}, {"command": "different"}, {"coverage": ["other"]}):
            with self.subTest(changes=changes):
                self.assertFalse(self.prepare([self.check(), self.check(**changes)])["eligible"])
        first = self.check()
        start = E.read_report(self.root, self.s["run_id"], first)["payload"]["started_at"]
        self.assertEqual(self.prepare([first, self.check(started_at=start)])["reason"], "parallel_failures")

    def test_changed_inputs_after_repair_and_environment_drift_allow_diagnosis(self):
        refs = [self.check(), self.check(fingerprint="repaired-input", environment={"platform": "changed"})]
        self.assertTrue(self.prepare(refs)["eligible"])
        self.reserve()

    def test_duplicates_inheritance_and_registration_fail(self):
        ref = self.check()
        self.assertFalse(self.prepare([ref, ref])["eligible"])
        self.assertFalse(self.prepare([ref, "missing.json"])["eligible"])
        self.assertFalse(self.prepare([ref, self.check(inherited_from={"source": "old"})])["eligible"])

    def test_newer_success_supersedes_blocker_at_prepare_and_dispatch(self):
        refs = [self.check(), self.check()]
        self.assertTrue(self.prepare(refs)["eligible"])
        self.check(exit_code=0, result_class="success")
        self.assertEqual(self.prepare(refs)["reason"], "newer_success")
        with self.assertRaisesRegex(ValueError, "newer_success"):
            self.reserve()

    def test_brief_bound_privacy_and_safe_summary(self):
        refs = [self.check(), self.check()]
        for brief in ("", "中" * 6001, "secret " + self.s["owner_token"]):
            self.assertFalse(self.prepare(refs, brief=brief)["eligible"])
        brief = "中" * 6000
        self.assertTrue(self.prepare(refs, brief=brief)["eligible"])
        stored = self.state()["expert_advice_cases"][0]
        self.assertEqual(stored["brief_chars"], 6000)
        self.assertNotIn(brief, json.dumps(stored, ensure_ascii=False))
        safe = json.dumps(A.summary(self.state()))
        for value in (refs[0], "sha256", "owner_token", "command", "environment", "host_executor_id"):
            self.assertNotIn(value, safe)

    def test_idempotence_no_rename_reset_and_one_attempt_per_cause(self):
        refs = [self.check(), self.check()]
        self.assertTrue(self.prepare(refs)["eligible"])
        before = self.state()
        self.assertTrue(self.prepare(list(reversed(refs)))["eligible"])
        self.assertEqual(self.state(), before)
        self.assertEqual(self.prepare(refs, case="renamed")["reason"], "evidence_already_bound")
        self.reserve()
        self.assertEqual(self.prepare(refs)["reason"], "case_attempt_used")
        with self.assertRaisesRegex(ValueError, "case_attempt_used"):
            self.reserve(role="scope_guardian")

    def test_task_cap_counts_pending_and_failed_across_roles(self):
        self.prepare()
        self.reserve()
        self.prepare([self.check(command="other-check"), self.check(command="other-check")], case="cause-b")
        self.reserve("cause-b", "scope_guardian")
        with R._write_guard(self.root) as base:
            path, state = R._load_owned(base, self.s["run_id"], self.s["owner_token"])
            state["role_dispatches"][0]["status"] = "failed"
            R._atomic(path, state)
        self.assertEqual(self.prepare([self.check(command="third"), self.check(command="third")], case="cause-c")["reason"], "task_budget_exhausted")
        self.assertEqual(A.summary(self.state())["attempts"], 2)

    def test_task_isolation_old_refs_and_ended_status(self):
        refs = [self.check(), self.check()]
        T.start(*self.args, "task-one", "goal", ["proof"])
        self.assertEqual(self.prepare(refs)["reason"], "old_task_evidence")
        self.prepare()
        self.reserve()
        T.end(*self.args, "task-one")
        self.assertEqual(A.summary(self.state())["attempts"], 1)
        self.assertEqual(self.prepare()["reason"], "task_not_running")
        T.start(*self.args, "task-two", "new goal", ["proof"])
        self.assertEqual(A.summary(self.state())["attempts"], 0)
        with self.assertRaisesRegex(ValueError, "not prepared"):
            self.reserve()

    def test_review_conflict_is_explicit_caller_assertion_not_machine_semantics(self):
        self.assertTrue(self.prepare([self.review(), self.review("review")], trigger="review_conflict")["eligible"])
        with self.assertRaisesRegex(ValueError, "planner or scope_guardian"):
            self.reserve(role="developer")
        with self.assertRaisesRegex(ValueError, "fork_turns=all"):
            self.reserve(native={"fork_turns": "all"})
        self.reserve(native={"fork_turns": "none"})

    def test_waits_and_owner_validation(self):
        refs = [self.check(), self.check()]
        for field, value in (("activity", "human_wait"), ("continuity_wait", {"status": "waiting"})):
            with R._write_guard(self.root) as base:
                path, state = R._load_owned(base, self.s["run_id"], self.s["owner_token"])
                state[field] = value
                R._atomic(path, state)
            self.assertEqual(self.prepare(refs)["reason"], "human_wait")
        with self.assertRaises(ValueError):
            A.prepare(self.root, self.s["run_id"], "wrong-owner", "case", "repeated_failure", refs, "brief", True)

    def test_report_or_log_mutation_invalidates_dispatch(self):
        for target in ("report", "log"):
            refs = [self.check(command=target), self.check(command=target)]
            case = "cause-" + target
            self.assertTrue(self.prepare(refs, case=case)["eligible"])
            report = E.read_report(self.root, self.s["run_id"], refs[0])
            path = self.root / (refs[0] if target == "report" else report["payload"]["log_path"])
            path.write_text(path.read_text() + " ", encoding="utf-8")
            with self.assertRaises(ValueError):
                self.reserve(case)

    def test_same_logical_cause_cannot_rename_using_new_or_overlapping_reports(self):
        r1, r2 = self.check(), self.check()
        self.assertTrue(self.prepare([r1, r2])["eligible"])
        self.reserve()
        self.assertEqual(self.prepare([r2, self.check()], case="renamed")["reason"], "evidence_already_bound")
        self.assertEqual(self.prepare(case="fresh-pair")["reason"], "cause_already_bound")
        self.assertTrue(self.prepare([self.review(), self.review()], case="different", trigger="review_conflict")["eligible"])
        self.reserve("different", "scope_guardian")
        self.assertEqual(A.summary(self.state())["attempts"], 2)

    def test_success_scan_stops_before_current_failure_window(self):
        old_refs = [self.check() for _ in range(12)]
        refs = [self.check(), self.check()]
        with patch.object(E, "read_report", wraps=E.read_report) as read:
            self.assertTrue(self.prepare(refs)["eligible"])
        scanned = [call.args[2] for call in read.call_args_list]
        self.assertNotIn(old_refs[0], scanned)
        self.assertLessEqual(len(scanned), 5)

    def test_interrupt_and_timeout_exit_codes_never_qualify(self):
        for code in (124, 130, 137, 143, -9):
            self.assertEqual(self.prepare([self.check(), self.check(exit_code=code)])["reason"], "qualifying_failure_required")

    def test_duplicate_zero_duration_execution_wrappers_are_not_two_attempts(self):
        stamp = self.now()
        first = self.check(started_at=stamp, finished_at=stamp)
        duplicate = self.check(started_at=stamp, finished_at=stamp)
        self.assertEqual(self.prepare([first, duplicate])["reason"], "duplicate_execution")
        # Separate sequential executions may emit exactly the same bytes.
        self.assertTrue(self.prepare()["eligible"])

    def test_delayed_old_inherited_incomplete_and_malformed_success_do_not_veto(self):
        old_start, old_end = self.now(), self.now()
        T.start(*self.args, "current", "goal", ["proof"])
        refs = [self.check(), self.check()]
        self.check(exit_code=0, result_class="success", started_at=old_start, finished_at=old_end)
        self.check(exit_code=0, result_class="success", inherited_from={"source": "old"})
        self.check(exit_code=0, result_class="success", finished_at=None)
        self.check(exit_code=0, result_class="success", finished_at="malformed")
        changed_log = self.check(exit_code=0, result_class="success")
        payload = E.read_report(self.root, self.s["run_id"], changed_log)["payload"]
        (self.root / payload["log_path"]).write_text("tampered")
        self.assertTrue(self.prepare(refs)["eligible"])
        self.reserve()

    def test_delayed_same_task_old_success_uses_execution_end_not_envelope_time(self):
        began, ended = self.now(), self.now()
        refs = [self.check(), self.check()]
        self.check(exit_code=0, result_class="success", started_at=began, finished_at=ended)
        self.assertTrue(self.prepare(refs)["eligible"])

    def test_managed_policy_must_be_acknowledged_before_prepare_write(self):
        refs = [self.check(), self.check()]
        for policy in (None, {}, {"mode": "managed", "caller_acknowledged": False}):
            with R._write_guard(self.root) as base:
                path, state = R._load_owned(base, self.s["run_id"], self.s["owner_token"])
                state["run_policy"] = policy
                R._atomic(path, state)
            before = self.state()
            with self.assertRaisesRegex(ValueError, "policy requires"):
                self.prepare(refs)
            self.assertEqual(before, self.state())

    def test_prepared_case_count_and_summary_are_bounded(self):
        for index in range(A.PREPARED_LIMIT):
            refs = [self.check(command=str(index)), self.check(command=str(index))]
            self.assertTrue(self.prepare(refs, case=f"case-{index}")["eligible"])
        refs = [self.check(command="new"), self.check(command="new")]
        self.assertEqual(self.prepare(refs, case="overflow")["reason"], "prepared_limit_reached")
        safe = A.summary(self.state())
        self.assertEqual(safe["prepared_cases"], A.PREPARED_LIMIT)
        self.assertEqual(len(safe["cases"]), A.PREPARED_LIMIT)
        self.assertEqual(safe["attempts"], 0)

    def test_owned_lock_prevents_competing_write_and_legacy_status_stays_empty(self):
        self.assertEqual(A.summary({})["attempts"], 0)
        self.assertNotIn("expert_advice_cases", self.state())
        refs = [self.check(), self.check()]
        with R._write_guard(self.root):
            with self.assertRaises(FileExistsError):
                self.prepare(refs)
        self.assertNotIn("expert_advice_cases", self.state())
        self.assertTrue(self.prepare(refs)["eligible"])
        self.assertEqual(len(self.state()["expert_advice_cases"]), 1)


if __name__ == "__main__":
    unittest.main()
