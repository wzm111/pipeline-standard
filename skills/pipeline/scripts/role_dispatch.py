"""Observed host dispatch boundaries; does not create or stop any host agent."""
from copy import deepcopy
import re
import uuid

import role_metrics as M
import run_state as R

EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "inherit", "unknown"}


def _model(value):
    value = "unknown" if value is None else value
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,95}", value):
        raise ValueError("model must be a safe identifier or unknown")
    return value


def _effort(value):
    value = "unknown" if value is None else value
    if not isinstance(value, str) or value not in EFFORTS:
        raise ValueError("unknown reasoning effort")
    return value


def _owned(base, run_id, owner):
    path, state = R._load_owned(base, run_id, owner)
    if state["status"] != "running":
        raise ValueError("dispatch requires an active owned run")
    return path, state


def _ticket(state, dispatch_id):
    item = next((d for d in state.get("role_dispatches", []) if d["id"] == dispatch_id), None)
    if item is None:
        raise ValueError("dispatch does not belong to this run")
    return item


def _record(state, role, executor_id, event, now):
    entry = {"role": role, "executor_id": executor_id, "event": event, "at": now}
    events = [*state.get("role_events", []), entry]
    M._actors(events)
    state["role_events"] = events


def begin(project_root, run_id, owner_token, role, *, executor_id=None, requested_model=None, requested_effort=None):
    """Before native spawn/follow-up: one compact attempt, no invented creation."""
    M._validate(role, executor_id or "new-executor", "reused" if executor_id else "created")
    model, effort = _model(requested_model), _effort(requested_effort)
    if any(owner_token in text for text in (model, effort, executor_id or "")):
        raise ValueError("owner cannot appear in dispatch metadata")
    with R._write_guard(project_root) as base:
        path, state = _owned(base, run_id, owner_token)
        if executor_id and any(d["executor_id"] == executor_id and d["status"] in {"dispatching", "working"}
                               for d in state.get("role_dispatches", [])):
            raise ValueError("executor already has an outstanding dispatch")
        actor = M._actors(state.get("role_events", [])).get(executor_id)
        if actor and (actor["role"] != role or actor["entries"][-1]["event"] != "stopped"):
            raise ValueError("existing executor must finish its current episode before a new dispatch")
        now = R._now()
        item = {"id": uuid.uuid4().hex, "role": role, "mode": "reuse" if executor_id else "create",
                "executor_id": executor_id, "requested_model": model, "requested_effort": effort,
                "actual_model": "unknown", "actual_effort": "unknown", "status": "dispatching",
                "requested_at": now, "accepted_at": None, "completed_at": None}
        state.setdefault("role_dispatches", []).append(item)
        state["role_execution"] = "delegated"
        state["updated_at"] = now
        R._atomic(path, state)
        return deepcopy(item)


def accepted(project_root, run_id, owner_token, dispatch_id, executor_id, *, actual_model=None, actual_effort=None):
    """Only after host acceptance; actual feedback is separate from request."""
    model, effort = _model(actual_model), _effort(actual_effort)
    if any(isinstance(text, str) and owner_token in text for text in (model, effort, executor_id)):
        raise ValueError("owner cannot appear in dispatch metadata")
    with R._write_guard(project_root) as base:
        path, state = _owned(base, run_id, owner_token)
        item = _ticket(state, dispatch_id)
        M._validate(item["role"], executor_id, "working")
        if item["status"] == "working" and (item["executor_id"], item["actual_model"], item["actual_effort"]) == (executor_id, model, effort):
            return deepcopy(item)
        if item["status"] != "dispatching" or (item["mode"] == "reuse" and item["executor_id"] != executor_id):
            raise ValueError("dispatch acceptance does not match request")
        actors = M._actors(state.get("role_events", []))
        if executor_id in actors and (item["mode"] == "create" or actors[executor_id]["entries"][-1]["event"] != "stopped"):
            raise ValueError("executor is already observed working or was previously created")
        now = R._now()
        if R._timestamp(now) < R._timestamp(item["requested_at"]):
            raise ValueError("dispatch clock moved backwards")
        if any(R._timestamp(item["requested_at"]) < R._timestamp(e["at"]) <= R._timestamp(now)
               for e in state["history"] if e["event"] in R.FINAL | {"resumed"}):
            raise ValueError("old dispatch crossed recovery; close it with observed stop evidence and dispatch anew")
        _record(state, item["role"], executor_id, "reused" if item["mode"] == "reuse" else "created", now)
        _record(state, item["role"], executor_id, "working", now)
        item.update(executor_id=executor_id, actual_model=model, actual_effort=effort, status="working", accepted_at=now)
        state["updated_at"] = now
        R._atomic(path, state)
        return deepcopy(item)


def complete(project_root, run_id, owner_token, dispatch_id, *, executors_stopped=False, stop_evidence=None, outcome="completed"):
    """Observe completion/failure after checking the host; never stops it itself."""
    R._stopped(executors_stopped, stop_evidence)
    if outcome not in {"completed", "failed", "interrupted"}:
        raise ValueError("invalid dispatch outcome")
    with R._write_guard(project_root) as base:
        path, state = _owned(base, run_id, owner_token)
        item = _ticket(state, dispatch_id)
        if item["status"] in {"completed", "failed", "interrupted"}:
            if item["status"] != outcome:
                raise ValueError("cannot rewrite a closed dispatch outcome")
            return deepcopy(item)
        if item["status"] == "dispatching" and outcome == "completed":
            raise ValueError("unaccepted dispatch cannot be completed")
        now = R._now()
        if R._timestamp(now) < R._timestamp(item["accepted_at"] or item["requested_at"]):
            raise ValueError("dispatch clock moved backwards")
        if item["status"] == "working":
            _record(state, item["role"], item["executor_id"], "stopped", now)
        item.update(status=outcome, completed_at=now)
        state["updated_at"] = now
        R._atomic(path, state)
        return deepcopy(item)


def confirm_direct(project_root, run_id, owner_token):
    """Caller confirms the completed batch had no actual role delegation."""
    with R._write_guard(project_root) as base:
        path, state = _owned(base, run_id, owner_token)
        if state.get("role_events") or state.get("role_dispatches"):
            raise ValueError("observed role work cannot be relabeled direct")
        if state.get("role_execution") != "direct":
            state["role_execution"] = "direct"
            state["updated_at"] = R._now()
            R._atomic(path, state)
        return {"run_id": run_id, "role_execution": "direct"}


def summary(project_root, run_id=None):
    state = R.status(project_root, run_id)["state"]
    entries = deepcopy(state.get("role_dispatches", [])) if state else []
    return {"run_id": state["run_id"] if state else run_id, "attempts": entries,
            "actual_models_are_host_reported_only": True, "metrics": M.summary(project_root, run_id)}
