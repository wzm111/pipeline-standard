"""Polling advice is bounded and private, never a task acceptance engine."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import monitor_policy as M


class MonitorPolicyTests(unittest.TestCase):
    def setUp(self):
        self.current = {"task_id": "task-one", "cursor": "cursor:1", "stage": "implementation",
                        "activity": "execution", "host_status": "active", "live_actors": 1}

    def test_unchanged_progress_is_quiet_and_backs_off_with_cap(self):
        first = M.decide(None, self.current)
        self.assertFalse(first["notify"])
        self.assertEqual(first["next_check_seconds"], 600)
        previous = first["state"]
        for _ in range(10):
            result = M.decide(previous, self.current)
            self.assertFalse(result["notify"])
            self.assertFalse(result["record_change"])
            self.assertLessEqual(result["next_check_seconds"], 3600)
            previous = result["state"]
        self.assertEqual(result["next_check_seconds"], 3600)
        self.current["cursor"] = "cursor:2"
        progress = M.decide(previous, self.current)
        self.assertEqual(progress["next_check_seconds"], 600)
        self.assertFalse(progress["record_change"])

    def test_human_wait_notifies_once_and_never_claims_completion(self):
        self.current.update(activity="human_wait", host_status="idle", live_actors=0,
                            needs_user=True, action_id="unlock-device", executors_stopped=True)
        result = M.decide(None, self.current)
        self.assertEqual(result["reason"], "required_user_action")
        self.assertGreaterEqual(result["next_check_seconds"], 1800)
        for _ in range(4):
            result = M.decide(result["state"], self.current)
            self.assertFalse(result["notify"])
            self.assertFalse(result["stop_monitor"])
        self.current["action_id"] = "grant-permission"
        self.assertTrue(M.decide(result["state"], self.current)["notify"])

    def test_completion_requires_acceptance_host_stop_and_extra_snapshot(self):
        self.current.update(host_status="idle", live_actors=0, executors_stopped=True)
        partial = M.decide(None, self.current)
        self.assertFalse(partial["stop_monitor"])
        self.current["outcome"] = "accepted"
        first = M.decide(partial["state"], self.current)
        self.assertEqual(first["next_check_seconds"], 0)
        self.assertFalse(first["stop_monitor"])
        last = M.decide(first["state"], self.current)
        self.assertTrue(last["stop_monitor"])
        self.assertTrue(last["notify"])
        self.assertIsNone(last["next_check_seconds"])
        repeated = M.decide(last["state"], self.current)
        self.assertTrue(repeated["stop_monitor"])
        self.assertFalse(repeated["notify"])
        for key, value in (("live_actors", 1), ("live_actors", None), ("executors_stopped", False),
                           ("host_status", "active")):
            changed = {**self.current, key: value}
            self.assertFalse(M.decide(first["state"], changed)["stop_monitor"])
        no_cursor = {**self.current, "cursor": None}
        unknown = M.decide(None, no_cursor)
        self.assertFalse(M.decide(unknown["state"], no_cursor)["stop_monitor"])

    def test_new_task_cannot_inherit_previous_completion(self):
        self.current.update(host_status="idle", live_actors=0, executors_stopped=True, outcome="accepted")
        first = M.decide(None, self.current)
        self.current["task_id"] = "task-two"
        second = M.decide(first["state"], self.current)
        self.assertFalse(second["stop_monitor"])
        self.assertEqual(second["state"]["terminal_observations"], 1)

    def test_waiting_activity_blocks_old_terminal_label_without_new_user_action(self):
        for activity in ("human_wait", "role_wait"):
            for outcome in ("accepted", "stopped"):
                current = {**self.current, "activity": activity, "host_status": "idle",
                           "live_actors": 0, "executors_stopped": True, "outcome": outcome}
                first = M.decide(None, current)
                last = M.decide(first["state"], current)
                self.assertFalse(first["stop_monitor"])
                self.assertFalse(last["stop_monitor"])
                self.assertFalse(last["notify"])
                self.assertGreater(first["next_check_seconds"], 0)

    def test_fault_and_real_model_feedback_are_notified_without_repeated_noise(self):
        prior = M.decide(None, self.current)["state"]
        self.current["failure_id"] = "capacity-episode-one"
        fault = M.decide(prior, self.current)
        self.assertEqual(fault["reason"], "new_failure")
        self.assertFalse(M.decide(fault["state"], self.current)["notify"])
        recovered = M.decide(fault["state"], {**self.current, "failure_id": None})
        self.assertEqual(recovered["reason"], "failure_recovered")
        self.assertFalse(M.decide(recovered["state"], {**self.current, "failure_id": None})["notify"])
        self.current.update(failure_id=None, model_feedback_id="host-model-evidence-one")
        self.assertEqual(M.decide(fault["state"], self.current)["reason"], "actual_model_feedback_changed")

    def test_unknown_private_fields_are_discarded_and_inputs_unchanged(self):
        self.current.update(owner_token="never-save-me", prompt="private prompt", environment={"secret": "value"})
        before = deepcopy(self.current)
        result = M.decide(None, self.current)
        self.assertEqual(before, self.current)
        self.assertNotIn("never-save-me", str(result))
        self.assertNotIn("private prompt", str(result))
        self.assertEqual(result["acceptance"], "not_determined_by_monitor")

    def test_invalid_state_is_not_silently_treated_as_progress(self):
        for changes in ({"needs_user": True}, {"executors_stopped": 1}, {"live_actors": -1},
                        {"task_id": "unsafe label"}, {"activity": "invented"}):
            with self.assertRaises(ValueError):
                M.decide(None, {**self.current, **changes})
        with self.assertRaises(ValueError):
            M.decide({"schema_version": 7}, self.current)
        with self.assertRaises(ValueError):
            M.decide(None, self.current, base_seconds=0)


if __name__ == "__main__":
    unittest.main()
