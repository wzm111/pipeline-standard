"""Pure advice for compact monitor snapshots; does not poll, schedule or accept work."""
import hashlib
import json
import re

import run_state as R


def _label(value, name, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", value):
        raise ValueError(f"{name} must be a safe observation identifier")
    return value


def decide(previous, observation, *, base_seconds=600, max_seconds=3600):
    """Return delta/notification/backoff advice, never an automation side effect.

    Host facts and acceptance are supplied by the observing agent. Merely idle,
    human-waiting, or a completed turn cannot establish task completion.
    Unknown fields are discarded, so prompts, reports and credentials need not
    be copied into the saved policy state.
    """
    if (type(base_seconds) is not int or type(max_seconds) is not int
            or not 1 <= base_seconds <= max_seconds <= 86400):
        raise ValueError("bounded positive polling intervals required")
    if not isinstance(observation, dict):
        raise ValueError("compact observation required")
    if previous is not None and (not isinstance(previous, dict) or previous.get("schema_version") != 1):
        raise ValueError("unknown previous policy state")
    current = {key: _label(observation.get(key), key, required=key == "task_id")
               for key in ("task_id", "cursor", "stage", "action_id", "failure_id", "model_feedback_id")}
    current["activity"] = observation.get("activity", "execution")
    current["host_status"] = observation.get("host_status", "unknown")
    current["outcome"] = observation.get("outcome", "open")
    if (current["activity"] not in R.ACTIVITIES or current["host_status"] not in {"active", "idle", "unknown"}
            or current["outcome"] not in {"open", "accepted", "stopped"}):
        raise ValueError("unknown observed state")
    for key in ("needs_user", "executors_stopped"):
        current[key] = observation.get(key, False)
        if type(current[key]) is not bool:
            raise ValueError(f"{key} must be an observed boolean")
    current["live_actors"] = observation.get("live_actors")
    if current["live_actors"] is not None and (type(current["live_actors"]) is not int or current["live_actors"] < 0):
        raise ValueError("live_actors must be a count or unknown")
    if current["needs_user"] and current["action_id"] is None:
        raise ValueError("a required action needs a stable safe identifier")
    prior = previous if previous and previous.get("task_id") == current["task_id"] else {}
    semantic = {k: v for k, v in current.items() if k != "cursor"}
    signature = hashlib.sha256(json.dumps(semantic, sort_keys=True).encode()).hexdigest()
    changed = prior.get("signature") != signature
    progress = changed or prior.get("cursor") != current["cursor"]
    unchanged = 0 if progress else min(prior.get("unchanged_count", 0) + 1, 16)
    reason = None
    if current["failure_id"] is not None and prior.get("failure_id") != current["failure_id"]:
        reason = "new_failure"
    elif current["needs_user"] and (not prior.get("needs_user") or prior.get("action_id") != current["action_id"]):
        reason = "required_user_action"
    elif prior and current["model_feedback_id"] is not None and prior.get("model_feedback_id") != current["model_feedback_id"]:
        reason = "actual_model_feedback_changed"
    elif prior.get("failure_id") is not None and current["failure_id"] is None:
        reason = "failure_recovered"
    terminal = (current["outcome"] in {"accepted", "stopped"} and current["host_status"] == "idle"
                and current["executors_stopped"] and current["live_actors"] == 0
                and current["activity"] not in {"human_wait", "role_wait"}
                and not current["needs_user"] and current["failure_id"] is None and current["cursor"] is not None)
    confirmations = prior.get("terminal_observations", 0) + 1 if terminal and not progress else int(terminal)
    stop = confirmations >= 2
    if stop and prior.get("terminal_observations", 0) < 2:
        reason = "current_task_closed"
    waiting = current["needs_user"] or current["activity"] == "human_wait"
    delay = min(max_seconds, base_seconds * (2 ** unchanged))
    if waiting:
        delay = max(delay, min(max_seconds, base_seconds * 3))
    if terminal and not stop:
        delay = 0  # One immediate compact confirmation, not a zero-interval schedule.
    state = {"schema_version": 1, **{k: current[k] for k in
             ("task_id", "cursor", "needs_user", "action_id", "failure_id", "model_feedback_id")},
             "signature": signature, "unchanged_count": unchanged, "terminal_observations": confirmations}
    return {"state": state, "record_change": changed, "notify": reason is not None,
            "reason": reason, "next_check_seconds": None if stop else delay,
            "stop_monitor": stop, "acceptance": "not_determined_by_monitor"}
