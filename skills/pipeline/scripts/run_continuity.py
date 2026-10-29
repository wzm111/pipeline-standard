"""Owned human handoffs and read-only host idle observations.

Callers must adopt these APIs explicitly; this module cannot intercept a native
host final response or establish that an external acceptance check passed.
"""
from copy import deepcopy

import run_state as R


def _text(value, name, owner):
    if (not isinstance(owner, str) or not owner or not isinstance(value, str)
            or not value.strip() or owner in value):
        raise ValueError(f"{name} must be nonempty and must not contain ownership credentials")
    return value


def _live_actors(state):
    from role_metrics import _actors
    actors = _actors(state.get("role_events", []))
    live = {key for key, actor in actors.items() if actor["entries"][-1]["event"] != "stopped"}
    for item in state.get("role_dispatches", []):
        if item.get("status") in {"dispatching", "working"}:
            live.add(item.get("executor_id") or "dispatch:" + item["id"])
    return len(live) + len(state.get("running_checks", []))


def wait_for_user(project_root, run_id, owner_token, *, reason, action, verify):
    """Register an actionable wait atomically, without discarding task evidence."""
    details = {key: _text(value, key, owner_token)
               for key, value in (("reason", reason), ("action", action), ("verify", verify))}
    with R._write_guard(project_root) as base:
        path, state = R._load_owned(base, run_id, owner_token)
        if state["status"] != "running":
            raise ValueError("only a running owner can register a human wait")
        if _live_actors(state):
            raise ValueError("live checks or roles must stop before human wait")
        previous = state.get("continuity_wait", {})
        if previous.get("status") == "waiting":
            if all(previous.get(key) == value for key, value in details.items()) and state.get("activity") == "human_wait":
                return deepcopy(state)
            raise ValueError("resolve the existing human wait before replacing it")
        now = R._now()
        state["continuity_wait"] = {"status": "waiting", **details, "started_at": now,
                                    "previous_next": deepcopy(state.get("next"))}
        state.update(activity="human_wait", updated_at=now)
        state["history"].append(R._event(state, "transition", now))
        R._atomic(path, state)
        return deepcopy(state)


def resume_work(project_root, run_id, owner_token, *, evidence):
    """Resume owned work; acknowledgement schedules verification, never PASS."""
    _text(evidence, "evidence", owner_token)
    with R._write_guard(project_root) as base:
        path, state = R._load_owned(base, run_id, owner_token)
        if state["status"] != "running":
            raise ValueError("resume a paused run through run_state.resume first")
        wait = state.get("continuity_wait", {})
        if wait.get("status") != "waiting":
            raise ValueError("no unresolved human wait")
        if _live_actors(state):
            raise ValueError("live checks or roles prevent continuation of a human wait")
        now = R._now()
        wait.update(status="resumed", resumed_at=now, evidence=evidence)
        state.update(activity="execution", next=wait["verify"], updated_at=now)
        state["history"].append(R._event(state, "transition", now))
        R._atomic(path, state)
        return deepcopy(state)


def inspect(project_root, run_id, *, host_status="unknown", host_ended_at=None):
    """Observe explicit host idle only; do not adopt, clean, or mutate a run.

    Idle evidence older than any durable state change is stale. Only the open
    timing tail can be capped; recorded historical intervals remain untouched.
    """
    if host_status not in {"unknown", "idle", "running"}:
        raise ValueError("host_status must be unknown, idle or running")
    snapshot = R.status(project_root, run_id)
    state = snapshot["state"]
    timing = R.timing(project_root, run_id)
    result = {"run_id": run_id, "status": state.get("status") if state else None,
              "activity": state.get("activity") if state else None,
              "waiting_for_user": False, "unclosed": False, "live_actors": None,
              "host_observation": "unknown", "known_idle_seconds": None, "timing": timing}
    if state is None:
        return result
    if state["run_id"] != run_id:
        raise ValueError("run identity mismatch")
    result["waiting_for_user"] = state.get("continuity_wait", {}).get("status") == "waiting"
    if R.status(project_root, run_id) != snapshot:
        # A concurrent owner transition invalidates the combined observation.
        return result
    try:
        result["live_actors"] = _live_actors(state)
    except (ValueError, KeyError, TypeError):
        return result
    if host_status == "running":
        result["host_observation"] = "running"
        return result
    if host_status != "idle" or host_ended_at is None:
        return result
    try:
        boundary = R._timestamp(host_ended_at)
        now = R._timestamp(timing["measured_at"])
        latest = max(R._timestamp(state[key]) for key in ("created_at", "updated_at"))
        latest = max([latest, *[R._timestamp(event["at"]) for event in state.get("history", [])],
                      *[R._timestamp(event["at"]) for event in state.get("role_events", [])]])
        if not latest <= boundary <= now:
            return result
    except (ValueError, TypeError, KeyError):
        return result
    if result["live_actors"] or timing["coverage"] == "unknown":
        return result
    result["host_observation"] = "idle"
    result["unclosed"] = state.get("status") == "running"
    if not result["unclosed"]:
        return result
    intervals = timing["intervals"]
    if not intervals or intervals[-1]["end"] != timing["measured_at"]:
        return result
    tail = intervals[-1]
    if boundary < R._timestamp(tail["start"]):
        return result
    idle = (now - boundary).total_seconds()
    result["known_idle_seconds"] = idle
    tail["end"] = boundary.isoformat()
    tail["seconds"] -= idle
    timing["tracked_seconds"] -= idle
    timing["activity_seconds"][tail["activity"]] -= idle
    timing["stage_seconds"][tail["stage"]] -= idle
    timing["reason"] = "explicit_host_idle_boundary"
    return result
