"""Native tool boundaries are simulated; host actual model is never inferred."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import role_dispatch as D
import run_state as R
import run_tasks as T


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

    def test_native_parameters_and_host_mapping_do_not_infer_actual_model(self):
        parameters = {"model": "gpt-6.1-sol", "reasoning_effort": "medium", "fork_turns": "none"}
        ticket = D.begin(*self.args, "developer", requested_model="gpt-6.1-sol", requested_effort="medium",
                         native_parameters=parameters)
        parameters["model"] = "changed-after-request"
        result = D.accepted(*self.args, ticket["id"], "dev", host_executor_id="/root/native-dev")
        self.assertEqual(result["native_parameters"]["model"], "gpt-6.1-sol")
        self.assertEqual(result["host_executor_id"], "/root/native-dev")
        self.assertEqual(result["actual_model"], "unknown")
        self.assertEqual(result["actual_effort"], "unknown")
        with self.assertRaises(ValueError):
            D.accepted(*self.args, ticket["id"], "dev", host_executor_id="/root/another-dev")
        self.done(ticket)
        followup = D.begin(*self.args, "developer", executor_id="dev")
        with self.assertRaises(ValueError):
            D.accepted(*self.args, followup["id"], "dev", host_executor_id="/root/another-dev")
        D.accepted(*self.args, followup["id"], "dev", host_executor_id="/root/native-dev")
        self.done(followup)

    def test_native_parameter_mismatch_and_unrelated_payload_are_rejected(self):
        for parameters in ({"model": "another-model"}, {"fork_turns": "none"},
                           {"model": "gpt-6.1-sol", "prompt": "unrelated body"},
                           {"model": "gpt-6.1-sol", "reasoning_effort": "high"}):
            with self.assertRaises(ValueError):
                D.begin(*self.args, "developer", requested_model="gpt-6.1-sol", requested_effort="medium",
                        native_parameters=parameters)

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

    def test_opted_in_phase_changes_only_at_acceptance_and_is_idempotent(self):
        R.update(*self.args, {"stage": "implementation", "activity": "execution"})
        ticket = D.begin(*self.args, "tester", workflow_phase="verification")
        self.assertEqual(R.status(self.root)["state"]["stage"], "implementation")
        accepted = D.accepted(*self.args, ticket["id"], "qa")
        self.assertTrue(accepted["workflow_applied"])
        state = R.status(self.root)["state"]
        self.assertEqual((state["stage"], state["activity"]), ("verification", "testing"))
        self.assertEqual(state["history"][-1]["at"], accepted["accepted_at"])
        self.assertEqual(D.accepted(*self.args, ticket["id"], "qa"), accepted)
        self.assertEqual(R.status(self.root)["state"]["history"], state["history"])

    def test_failed_or_unaccepted_phase_request_does_not_claim_qa_entry(self):
        before = R.status(self.root)["state"]
        ticket = D.begin(*self.args, "tester", workflow_phase="verification")
        with self.assertRaisesRegex(ValueError, "accepted working"):
            D.wait_for_dispatches(*self.args, [ticket["id"]])
        self.done(ticket, "failed")
        state = R.status(self.root)["state"]
        self.assertEqual(state["history"], before["history"])
        self.assertEqual((state["stage"], state["activity"]), (before["stage"], before["activity"]))

    def test_background_roles_and_stale_phase_requests_preserve_explicit_main_state(self):
        R.update(*self.args, {"stage": "implementation", "activity": "execution"})
        background = D.begin(*self.args, "tester")
        D.accepted(*self.args, background["id"], "background")
        self.assertEqual(R.status(self.root)["state"]["stage"], "implementation")
        primary = D.begin(*self.args, "tester", workflow_phase="verification")
        R.update(*self.args, {"stage": "review", "activity": "execution"})
        accepted = D.accepted(*self.args, primary["id"], "primary")
        self.assertFalse(accepted["workflow_applied"])
        self.assertEqual(R.status(self.root)["state"]["stage"], "review")

    def test_parallel_selected_wait_reaches_handoff_only_after_all_return(self):
        tickets = [D.begin(*self.args, role) for role in ("developer", "tester", "scope_guardian")]
        for index, ticket in enumerate(tickets):
            D.accepted(*self.args, ticket["id"], "actor" + str(index))
        waiting = D.wait_for_dispatches(*self.args, [item["id"] for item in tickets[:2]])
        history = R.status(self.root)["state"]["history"]
        self.assertEqual(D.wait_for_dispatches(*self.args, [item["id"] for item in tickets[:2]]), waiting)
        self.assertEqual(R.status(self.root)["state"]["history"], history)
        self.done(tickets[2])  # A background return cannot close the selected wait.
        self.assertEqual(R.status(self.root)["state"]["activity"], "role_wait")
        self.done(tickets[0])
        self.assertEqual(R.status(self.root)["state"]["activity"], "role_wait")
        self.assertEqual(D.wait_for_dispatches(*self.args, [item["id"] for item in tickets[:2]]), waiting)
        done = self.done(tickets[1], "failed")
        state = R.status(self.root)["state"]
        self.assertEqual(state["activity"], "handoff")
        self.assertEqual(state["history"][-1]["at"], done["completed_at"])
        self.done(tickets[1], "failed")
        self.assertEqual(R.status(self.root)["state"]["history"], state["history"])

    def test_human_wait_wins_over_phase_acceptance_and_role_return(self):
        ticket = D.begin(*self.args, "tester", workflow_phase="verification")
        R.update(*self.args, {"activity": "human_wait"})
        self.assertFalse(D.accepted(*self.args, ticket["id"], "qa")["workflow_applied"])
        with self.assertRaisesRegex(ValueError, "human wait"):
            D.wait_for_dispatches(*self.args, [ticket["id"]])
        R.update(*self.args, {"activity": "testing"})
        D.wait_for_dispatches(*self.args, [ticket["id"]])
        R.update(*self.args, {"activity": "human_wait"})
        self.done(ticket)
        self.assertEqual(R.status(self.root)["state"]["activity"], "human_wait")

    def test_explicit_main_transition_invalidates_wait_even_if_activity_returns(self):
        ticket = D.begin(*self.args, "tester")
        D.accepted(*self.args, ticket["id"], "qa")
        D.wait_for_dispatches(*self.args, [ticket["id"]])
        R.update(*self.args, {"activity": "execution"})
        R.update(*self.args, {"activity": "role_wait"})
        before = R.status(self.root)["state"]["history"]
        self.done(ticket)
        self.assertEqual(R.status(self.root)["state"]["activity"], "role_wait")
        self.assertEqual(R.status(self.root)["state"]["history"], before)

    def test_same_value_main_update_and_new_task_supersede_pending_phase(self):
        for kind in ("same-value", "task"):
            with self.subTest(kind=kind):
                ticket = D.begin(*self.args, "tester", workflow_phase="verification")
                state = R.status(self.root)["state"]
                if kind == "same-value":
                    R.update(*self.args, {"stage": state["stage"], "activity": state["activity"]})
                else:
                    T.start(*self.args, "new-task", "new goal", ["observed"])
                accepted = D.accepted(*self.args, ticket["id"], kind)
                self.assertFalse(accepted["workflow_applied"])
                self.done(ticket)

    def test_same_value_update_supersedes_registered_wait_without_duplicate_transition(self):
        ticket = D.begin(*self.args, "tester")
        D.accepted(*self.args, ticket["id"], "qa")
        D.wait_for_dispatches(*self.args, [ticket["id"]])
        R.update(*self.args, {"activity": "role_wait"})
        before = R.status(self.root)["state"]["history"]
        self.done(ticket)
        self.assertEqual(R.status(self.root)["state"]["history"], before)
        self.assertEqual(R.status(self.root)["state"]["activity"], "role_wait")

    def test_foreground_check_or_host_claim_prevents_role_wait_and_phase_takeover(self):
        for field, claim in (("running_checks", [{"name": "foreground", "write_paths": []}]),
                             ("observed_checks", {"host": {"status": "running"}})):
            with self.subTest(field=field):
                with R._write_guard(self.root) as base:
                    path, state = R._load_owned(base, self.s["run_id"], self.s["owner_token"])
                    state[field] = claim
                    R._atomic(path, state)
                ticket = D.begin(*self.args, "tester", workflow_phase="verification")
                self.assertFalse(D.accepted(*self.args, ticket["id"], field)["workflow_applied"])
                with self.assertRaisesRegex(ValueError, "foreground checks"):
                    D.wait_for_dispatches(*self.args, [ticket["id"]])
                self.done(ticket)
                with R._write_guard(self.root) as base:
                    path, state = R._load_owned(base, self.s["run_id"], self.s["owner_token"])
                    state.pop(field)
                    R._atomic(path, state)


if __name__ == "__main__":
    unittest.main()
