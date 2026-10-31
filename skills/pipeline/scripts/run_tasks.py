"""Owned execution segments within a run; never QA or release acceptance."""
from copy import deepcopy
import json

import run_evidence as E
import run_state as R

ENDINGS = {"execution_ended", "paused", "aborted"}
NOTICE = "Task end records an execution boundary, not QA acceptance, run completion or release authorization."


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("task goal and acceptance entries must be nonempty strings")


def _tasks(state):
    tasks = state.get("tasks", [])
    if not isinstance(tasks, list):
        raise ValueError("tasks must be a list")
    seen = set()
    previous_end = None
    for item in tasks:
        if not isinstance(item, dict) or set(item) != {
                "task_id", "goal", "acceptance", "started_at", "ended_at", "status", "report_refs"}:
            raise ValueError("invalid task record")
        E._label(item["task_id"])
        if item["task_id"] in seen:
            raise ValueError("duplicate task identity")
        seen.add(item["task_id"])
        _text(item["goal"])
        if not isinstance(item["acceptance"], list) or not item["acceptance"]:
            raise ValueError("task acceptance must be a nonempty list")
        for entry in item["acceptance"]:
            _text(entry)
        start = R._timestamp(item["started_at"])
        if previous_end is not None and start < previous_end:
            raise ValueError("task intervals overlap")
        if item["status"] == "running":
            if item["ended_at"] is not None or item is not tasks[-1]:
                raise ValueError("only the last task may be running without an end")
        elif item["status"] not in ENDINGS or R._timestamp(item["ended_at"]) < start:
            raise ValueError("invalid task ending")
        previous_end = R._timestamp(item["ended_at"]) if item["ended_at"] else None
        if (not isinstance(item["report_refs"], list)
                or any(not isinstance(ref, str) for ref in item["report_refs"])
                or len(set(item["report_refs"])) != len(item["report_refs"])):
            raise ValueError("invalid task report references")
    return tasks


def _owned(base, run_id, owner):
    path, state = R._load_owned(base, run_id, owner)
    if state["status"] != "running":
        raise ValueError("task recording requires an active running run")
    _tasks(state)
    return path, state


def _clock(state):
    now = R._now()
    latest = [state.get("updated_at"), *(item[key] for item in state.get("tasks", [])
              for key in ("started_at", "ended_at") if item[key] is not None)]
    if any(R._timestamp(value) > R._timestamp(now) for value in latest if value is not None):
        raise ValueError("task clock moved backwards")
    return now


def start(root, run_id, owner, task_id, goal, acceptance):
    E._label(task_id)
    _text(goal)
    if not isinstance(acceptance, list) or not acceptance:
        raise ValueError("task acceptance must be a nonempty list")
    for entry in acceptance:
        _text(entry)
    if isinstance(owner, str) and owner and owner in json.dumps([task_id, goal, acceptance], ensure_ascii=False):
        raise ValueError("owner token must not appear in task records")
    with R._write_guard(root) as base:
        path, state = _owned(base, run_id, owner)
        tasks = state.get("tasks", [])
        existing = next((item for item in tasks if item["task_id"] == task_id), None)
        if existing is not None:
            if existing["goal"] != goal or existing["acceptance"] != acceptance:
                raise ValueError("task identity already records a different goal or acceptance")
            return deepcopy(existing)
        if any(item["status"] == "running" for item in tasks):
            raise ValueError("end the current task explicitly before starting another")
        now = _clock(state)
        item = {"task_id": task_id, "goal": goal, "acceptance": deepcopy(acceptance),
                "started_at": now, "ended_at": None, "status": "running", "report_refs": []}
        state.setdefault("tasks", []).append(item)
        state["updated_at"] = now
        R._atomic(path, state)
        return deepcopy(item)


def end(root, run_id, owner, task_id, *, outcome="execution_ended", report_refs=None):
    E._label(task_id)
    if not isinstance(outcome, str) or outcome not in ENDINGS:
        raise ValueError("task outcome must be execution_ended, paused or aborted")
    refs = [] if report_refs is None else report_refs
    if (not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs)
            or len(set(refs)) != len(refs)):
        raise ValueError("report_refs must be a list of unique report paths")
    with R._write_guard(root) as base:
        path, state = _owned(base, run_id, owner)
        item = next((item for item in state.get("tasks", []) if item["task_id"] == task_id), None)
        if item is None:
            raise ValueError("unknown task in this run; historical timestamps are not inferred")
        if item["status"] != "running":
            if item["status"] != outcome or item["report_refs"] != refs:
                raise ValueError("task ending is immutable")
            return deepcopy(item)
        for ref in refs:
            if ref not in state.get("artifacts", []):
                raise ValueError("task report must already be registered in this run")
            E.read_report(root, run_id, ref)
        now = _clock(state)
        item.update(status=outcome, ended_at=now, report_refs=deepcopy(refs))
        state["updated_at"] = now
        R._atomic(path, state)
        return deepcopy(item)


def status(root, run_id=None):
    """Read only; absent history stays unknown, including before first task."""
    state = R.status(root, run_id)["state"]
    tasks = _tasks(state) if state else []
    return {"run_id": state["run_id"] if state else run_id,
            "coverage": "observed_tasks_only" if tasks else "unknown",
            "current_task_id": next((item["task_id"] for item in tasks if item["status"] == "running"), None),
            "tasks": deepcopy(tasks), "notice": NOTICE}
