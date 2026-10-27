"""Native tool boundaries are simulated; host actual model is never inferred."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import role_dispatch as D
import run_state as R


class RoleDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.s = R.start(self.root)
        self.args = self.root, self.s["run_id"], self.s["owner_token"]

    def done(self, ticket, outcome="completed"):
        return D.complete(*self.args, ticket["id"], executors_stopped=True, stop_evidence="fixture host confirms stopped", outcome=outcome)

    def test_attempt_is_not_creation_and_acceptance_records_actual_boundaries_once(self):
        ticket = D.begin(*self.args, "developer", requested_model="gpt-6.1-sol", requested_effort="medium")
        self.assertNotIn("role_events", R.status(self.root)["state"])
        self.assertEqual(ticket["actual_model"], "unknown")
        accepted = D.accepted(*self.args, ticket["id"], "dev-1")
        self.assertEqual(D.accepted(*self.args, ticket["id"], "dev-1"), accepted)
        done = self.done(ticket)
        self.assertEqual(self.done(ticket), done)
        events = R.status(self.root)["state"]["role_events"]
        self.assertEqual([e["event"] for e in events], ["created", "working", "stopped"])
        summary = D.summary(self.root, self.s["run_id"])
        self.assertEqual(summary["attempts"][0]["actual_effort"], "unknown")
        self.assertEqual(summary["metrics"]["roles"]["developer"]["created_count"], 1)

    def test_followup_reuses_executor_without_extra_creation_or_inferred_actual_model(self):
        first = D.begin(*self.args, "tester")
        D.accepted(*self.args, first["id"], "qa-1", actual_model="host-reported-model", actual_effort="high")
        self.done(first)
        second = D.begin(*self.args, "tester", executor_id="qa-1", requested_model="gpt-6.1-sol")
        accepted = D.accepted(*self.args, second["id"], "qa-1")
        self.assertEqual(accepted["actual_model"], "unknown")
        self.done(second)
        counts = D.summary(self.root)["metrics"]["roles"]["tester"]
        self.assertEqual((counts["created_count"], counts["reused_count"]), (1, 1))

    def test_known_preexisting_executor_has_unknown_creation_history(self):
        ticket = D.begin(*self.args, "scope_guardian", executor_id="existing-guardian")
        D.accepted(*self.args, ticket["id"], "existing-guardian")
        self.done(ticket)
        role = D.summary(self.root)["metrics"]["roles"]["scope_guardian"]
        self.assertEqual(role["creation_history"], "unknown")
        self.assertEqual(role["created_count"], 0)

    def test_failed_spawn_cannot_be_completed_or_counted_as_created(self):
        ticket = D.begin(*self.args, "developer")
        with self.assertRaises(ValueError):
            self.done(ticket)
        self.done(ticket, "failed")
        self.assertEqual(D.summary(self.root)["metrics"]["roles"], {})
        with self.assertRaises(ValueError):
            D.accepted(*self.args, ticket["id"], "late-dev")

    def test_direct_execution_is_explicitly_not_missing_role_events(self):
        self.assertEqual(D.summary(self.root)["metrics"]["coverage"], "unknown")
        D.confirm_direct(*self.args)
        self.assertEqual(D.summary(self.root)["metrics"]["coverage"], "not_applicable")
        ticket = D.begin(*self.args, "developer")
        with self.assertRaises(ValueError):
            D.confirm_direct(*self.args)
        self.done(ticket, "failed")

    def test_wrong_owner_unsafe_metadata_and_unconfirmed_stop_are_rejected(self):
        with self.assertRaises(ValueError):
            D.begin(self.root, self.s["run_id"], "wrong", "developer")
        for options in ({"requested_model":"model with secret"}, {"requested_effort":"invalid"}, {"executor_id":"../agent"}, {"requested_model":self.s["owner_token"]}):
            with self.assertRaises(ValueError):
                D.begin(*self.args, "developer", **options)
        ticket = D.begin(*self.args, "developer")
        with self.assertRaises(ValueError):
            D.complete(*self.args, ticket["id"], outcome="failed")
        self.assertEqual(D.summary(self.root)["attempts"][0]["status"], "dispatching")

    def test_duplicate_followup_and_executor_mismatch_cannot_corrupt_lifecycle(self):
        ticket = D.begin(*self.args, "tester", executor_id="qa")
        with self.assertRaises(ValueError):
            D.begin(*self.args, "tester", executor_id="qa")
        with self.assertRaises(ValueError):
            D.accepted(*self.args, ticket["id"], "different-qa")
        D.accepted(*self.args, ticket["id"], "qa")
        self.done(ticket)

    def test_unknown_summary_is_read_only_and_request_accept_clock_cannot_reverse(self):
        ticket = D.begin(*self.args, "developer")
        with patch.object(R, "_now", return_value="2000-01-01T00:00:00+00:00"):
            with self.assertRaises(ValueError):
                D.accepted(*self.args, ticket["id"], "dev")
        before = {p:p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        D.summary(self.root)
        self.assertEqual(before, {p:p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_finish_cannot_silently_drop_registered_live_dispatch(self):
        ticket = D.begin(*self.args, "developer")
        with self.assertRaisesRegex(ValueError, "close observed"):
            R.finish(*self.args, "completed", executors_stopped=True, stop_evidence="fixture stopped")
        self.assertIsNotNone(R.status(self.root)["active"])
        self.done(ticket, "failed")
        R.finish(*self.args, "completed", executors_stopped=True, stop_evidence="fixture stopped")

    def test_old_unaccepted_request_cannot_masquerade_as_fresh_acceptance_after_recovery(self):
        ticket = D.begin(*self.args, "developer")
        R.resume(*self.args, executors_stopped=True, stop_evidence="fixture confirms interrupted request stopped")
        with self.assertRaisesRegex(ValueError, "recovery"):
            D.accepted(*self.args, ticket["id"], "dev")
        self.done(ticket, "interrupted")
        fresh = D.begin(*self.args, "developer")
        D.accepted(*self.args, fresh["id"], "new-dev")
        self.done(fresh)


if __name__ == "__main__":
    unittest.main()
