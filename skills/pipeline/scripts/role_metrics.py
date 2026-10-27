"""Observed role events and interval unions; never creates host agents."""
from copy import deepcopy
import re

import run_state
from workflow_metrics import interval_seconds

ROLES = {"developer", "planner", "scope_guardian", "tester", "releaser"}
PHASES = {"preparing", "working", "waiting"}
EVENTS = PHASES | {"created", "reused", "stopped"}


def _validate(role, executor_id, event):
    if not isinstance(role, str) or role not in ROLES:
        raise ValueError("unknown role")
    if not isinstance(executor_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", executor_id):
        raise ValueError("executor_id must be a safe nonempty short identifier")
    if not isinstance(event, str) or event not in EVENTS:
        raise ValueError("unknown role event")


def _actors(events):
    if not isinstance(events, list):
        raise ValueError("role_events must be an array")
    actors = {}
    previous_at = None
    for entry in events:
        if not isinstance(entry, dict):
            raise ValueError("invalid role event")
        role, executor_id, event = (entry.get(key) for key in ("role", "executor_id", "event"))
        _validate(role, executor_id, event)
        at = run_state._timestamp(entry.get("at")).timestamp()
        if previous_at is not None and at < previous_at:
            raise ValueError("nonmonotonic role events")
        actor = actors.setdefault(executor_id, {"role": role, "entries": []})
        if actor["role"] != role:
            raise ValueError("executor cannot change roles")
        history = actor["entries"]
        if not history:
            if event not in {"created", "reused"}:
                raise ValueError("first executor event must be created or reused")
        elif event == "created":
            raise ValueError("created cannot repeat for an executor")
        elif history[-1]["event"] == "stopped" and event != "reused":
            raise ValueError("stopped executor must be reused before continuing")
        history.append({"event": event, "at": at})
        previous_at = at
    return actors


def record_role_event(root, run_id, owner, role, executor_id, event):
    """Record caller-observed work only; stopped does not destroy a host agent."""
    _validate(role, executor_id, event)
    with run_state._write_guard(root) as base:
        path, state = run_state._load_owned(base, run_id, owner)
        if state["status"] != "running":
            raise ValueError("only an active running owner can record role events")
        events = state.get("role_events", [])
        actors = _actors(events)
        previous = actors.get(executor_id)
        if previous and previous["role"] != role:
            raise ValueError("executor cannot change roles")
        if previous and event in PHASES and previous["entries"][-1]["event"] == event:
            return deepcopy(next(entry for entry in reversed(events) if entry["executor_id"] == executor_id))
        now = run_state._now()
        entry = {"role": role, "executor_id": executor_id, "event": event, "at": now}
        updated = [*events, entry]
        _actors(updated)  # Validate lifecycle and the actual clock before writing.
        state["role_events"] = updated
        state["updated_at"] = now
        run_state._atomic(path, state)
        return deepcopy(entry)


def summary(root, run_id=None):
    """Read-only observed phase unions; absent evidence stays null/unknown.

    Phase unions can overlap each other. Their sum is not role or overall wall
    time. Open tails are bounded by now only for the confirmed active owner.
    """
    snapshot = run_state.status(root, run_id)
    state, active = snapshot["state"], snapshot["active"]
    measured_at = run_state._now()
    result = {"run_id": state.get("run_id") if state else run_id, "measured_at": measured_at,
              "coverage": "unknown", "reason": "role_history_missing", "roles": {},
              "observed_wall_seconds": None, "counts_are_observed": True}
    if state is None:
        result["reason"] = "active_owner_unrecognized" if active == "unrecognized" else "no_state"
        return result
    if run_id is not None and state["run_id"] != run_id:
        raise ValueError("run identity mismatch")
    if isinstance(active, dict) and active["run_id"] == state["run_id"]:
        try:
            run_state._owned(active, state["run_id"], state["owner_token"])
        except ValueError:
            result["reason"] = "active_owner_mismatch"
            return result
    owns_active = isinstance(active, dict) and active["run_id"] == state["run_id"]
    try:
        actors = _actors(state.get("role_events", []))
        now = run_state._timestamp(measured_at).timestamp()
        run_stops = [run_state._timestamp(entry.get("at")).timestamp()
                     for entry in state.get("history", [])
                     if isinstance(entry, dict) and entry.get("event") in run_state.FINAL]
        run_resumes = [run_state._timestamp(entry.get("at")).timestamp()
                       for entry in state.get("history", [])
                       if isinstance(entry, dict) and entry.get("event") == "resumed"]
        if any(actor["entries"][-1]["at"] > now for actor in actors.values()):
            raise ValueError("role events extend beyond read time")
    except (TypeError, ValueError, OverflowError):
        result["reason"] = "invalid_role_history"
        return result
    if not actors:
        if state.get("role_execution") == "direct" and not state.get("role_dispatches"):
            result.update(coverage="not_applicable", reason="direct_execution_without_roles", observed_wall_seconds=0.0)
        return result
    roles = {}
    for actor in actors.values():
        role = roles.setdefault(actor["role"], {"created_count": 0, "reused_count": 0,
            "creation_history": "observed", "incomplete": not bool(state.get("created_at")),
            "unclosed_phase_count": 0, "unclassified_gap_count": 0,
            "phases": {phase: [] for phase in PHASES}})
        entries = actor["entries"]
        if entries[0]["event"] == "reused":
            role["creation_history"] = "unknown"
            role["incomplete"] = True
        for index, entry in enumerate(entries):
            event, start = entry["event"], entry["at"]
            if event in {"created", "reused"}:
                role[f"{event}_count"] += 1
            end = entries[index + 1]["at"] if index + 1 < len(entries) else None
            if event in PHASES:
                if end is None:
                    if state["status"] == "running" and owns_active:
                        end = now
                    else:
                        role["unclosed_phase_count"] += 1
                        role["incomplete"] = True
                if end is not None:
                    if (any(start <= boundary < end for boundary in run_stops)
                            or any(start < boundary <= end for boundary in run_resumes)):
                        # The run stopped without this executor recording an
                        # end. A later resume cannot supply that missing end.
                        role["unclosed_phase_count"] += 1
                        role["incomplete"] = True
                    else:
                        role["phases"][event].append((start, end))
            elif event in {"created", "reused"}:
                if end is None or end > start:
                    role["unclassified_gap_count"] += 1
                    role["incomplete"] = True
    all_intervals = []
    for name, role in roles.items():
        phases = role.pop("phases")
        incomplete = role.pop("incomplete")
        intervals = [span for spans in phases.values() for span in spans]
        all_intervals.extend(intervals)
        role["phase_seconds"] = {phase: interval_seconds(spans) if spans else None for phase, spans in sorted(phases.items())}
        role["observed_wall_seconds"] = interval_seconds(intervals) if intervals else None
        role["coverage"] = "unknown" if not intervals else "partial" if incomplete else "complete"
        result["roles"][name] = role
    if all_intervals:
        result["observed_wall_seconds"] = interval_seconds(all_intervals)
        result["coverage"] = "complete" if all(role["coverage"] == "complete" for role in result["roles"].values()) else "partial"
    result["reason"] = "observed_role_events" if result["coverage"] == "complete" else "role_history_incomplete"
    return result
