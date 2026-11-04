"""Small caller-declared work allocation; never spawns or observes native agents."""
from copy import deepcopy
import json
import re
import unicodedata

import run_policy
import run_state as R

MODES = {"direct", "serial", "parallel", "verification"}
REASONS = {"small_scope", "shared_write_paths", "dependency_chain", "independent_work",
           "verification_only", "capacity_limit", "scope_changed"}
REVIEW_REASONS = {"task_start", "run_start", "scope_change"}
MAX_DECISIONS = 128
MAX_REVIEWS = 256
SUMMARY_LIMIT = 32


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", value):
        raise ValueError("allocation identifiers must be safe short technical identifiers")
    return value


def _no_owner(state, value):
    owner = state.get("owner_token")
    if owner and owner in json.dumps(value, ensure_ascii=False):
        raise ValueError("owner token cannot appear in allocation metadata")


def _scope(state):
    tasks = state.get("tasks", [])
    return {"run_id": state["run_id"], "task_id": tasks[-1]["task_id"] if tasks else None}


def initialize(state, trigger_id, reason="task_start"):
    """Add a pending review to supplied state; repeated triggers never reopen it."""
    _identifier(trigger_id)
    if reason not in REVIEW_REASONS:
        raise ValueError("unknown allocation review reason")
    _no_owner(state, trigger_id)
    allocation = deepcopy(state.get("work_allocation", {"status": "pending", "revision": 0,
                                                       "triggers": [], "decisions": []}))
    trigger = {**_scope(state), "trigger_id": trigger_id, "review_reason": reason}
    if trigger in allocation["triggers"]:
        return deepcopy(allocation)
    if len(allocation["triggers"]) >= MAX_REVIEWS:
        raise ValueError("allocation review limit reached")
    allocation["triggers"].append(trigger)
    allocation.update(trigger, status="pending")
    state["work_allocation"] = allocation
    return deepcopy(allocation)


def _current_decisions(state):
    scope = _scope(state)
    return [d for d in state.get("work_allocation", {}).get("decisions", [])
            if all(d.get(key) == value for key, value in scope.items())]


def _active_task(state):
    if state.get("tasks") and state["tasks"][-1]["status"] != "running":
        raise ValueError("allocation work requires a running current task")


def _owned(base, rid, owner):
    path, state = R._load_owned(base, rid, owner)
    if state["status"] != "running":
        raise ValueError("allocation requires an active owned running run")
    return path, state


def scope_change(root, rid, owner, change_id):
    """Record an explicit meaningful scope change, preserving earlier decisions."""
    with R._write_guard(root) as base:
        path, state = _owned(base, rid, owner)
        run_policy.require(root, state)
        _active_task(state)
        before = deepcopy(state.get("work_allocation"))
        result = initialize(state, change_id, "scope_change")
        if result != before:
            state["updated_at"] = R._now()
            R._atomic(path, state)
        return summary(state)


def _ownership(items):
    for index, item in enumerate(items):
        for other in items[index + 1:]:
            if item["executor_id"] != other["executor_id"] and any(
                    a == b or a.startswith(b + "/") or b.startswith(a + "/")
                    for a in item["write_paths"] for b in other["write_paths"]):
                raise ValueError("write paths overlap across different executors")


def _assignments(mode, assignments):
    if not isinstance(assignments, list) or len(assignments) > 32:
        raise ValueError("assignments must be a bounded list")
    items = deepcopy(assignments)
    ids = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"id", "executor_id", "write_paths", "depends_on"}:
            raise ValueError("assignment fields must be id, executor_id, write_paths, depends_on")
        key = _identifier(item["id"])
        _identifier(item["executor_id"])
        if key in ids:
            raise ValueError("duplicate work item")
        ids.add(key)
        paths, deps = item["write_paths"], item["depends_on"]
        if not isinstance(paths, list) or len(paths) > 32 or not isinstance(deps, list) or len(deps) > 32:
            raise ValueError("paths and dependencies must be bounded lists")
        for path in paths:
            if (not isinstance(path, str) or len(path) > 256
                    or re.match(r"^[A-Za-z]:", path)
                    or any(ch in "\\*?[]" or unicodedata.category(ch) in {"Cc", "Cf", "Cs"} for ch in path)
                    or any(part in {"", ".", "..", ".git"} for part in path.split("/"))):
                raise ValueError("write paths must be safe lexical repository-relative paths")
        for dep in deps:
            _identifier(dep)
        if len(set(paths)) != len(paths) or len(set(deps)) != len(deps):
            raise ValueError("duplicate path or dependency")
        item["write_paths"] = sorted(paths)
        item["depends_on"] = sorted(deps)
    executors = {item["executor_id"] for item in items}
    if ((mode in {"direct", "verification"} and items)
            or (mode == "serial" and len(executors) != 1)
            or (mode == "parallel" and not 2 <= len(executors) <= 3)):
        raise ValueError("mode requires zero developers, one serial executor, or two to three parallel executors")
    _ownership(items)
    graph = {item["id"]: item["depends_on"] for item in items}
    done, visiting = set(), set()
    def visit(key):
        if key not in graph:
            raise ValueError("unknown dependency")
        if key in visiting:
            raise ValueError("dependency cycle or self dependency")
        if key not in done:
            visiting.add(key)
            for dep in graph[key]:
                visit(dep)
            visiting.remove(key)
            done.add(key)
    for key in graph:
        visit(key)
    return sorted(items, key=lambda item: item["id"])


def _dispatched(state, revision, work_item):
    return [item for item in state.get("role_dispatches", []) if item.get("role") == "developer"
            and item.get("allocation_revision") == revision and item.get("work_item") == work_item]


def _bound(state):
    result = {}
    for decision in _current_decisions(state):
        for item in decision["assignments"]:
            if _dispatched(state, decision["revision"], item["id"]):
                result[item["id"]] = item
    return result


def decide(root, rid, owner, mode, reason, assignments):
    """Record a technical decision after policy acknowledgement, without dispatch."""
    if mode not in MODES or reason not in REASONS:
        raise ValueError("unknown allocation mode or technical reason")
    items = _assignments(mode, assignments)
    with R._write_guard(root) as base:
        path, state = _owned(base, rid, owner)
        run_policy.require(root, state)
        _active_task(state)
        _no_owner(state, [mode, reason, items])
        allocation = state.get("work_allocation")
        if allocation is None:
            raise ValueError("allocation review must be initialized before deciding")
        decision = {"mode": mode, "reason": reason, "assignments": items}
        previous = allocation["decisions"][-1] if allocation["decisions"] else None
        if allocation["status"] == "decided" and previous and all(previous[k] == v for k, v in decision.items()):
            return summary(state)
        bound = _bound(state)
        for item in items:
            if item["id"] in bound and item != bound[item["id"]]:
                raise ValueError("cannot rewrite a dispatched assignment")
        # In-flight ownership continues even when omitted from the remaining plan.
        live = [item for prior in allocation["decisions"] for item in prior["assignments"]
                if any(d.get("status") in {"dispatching", "working"}
                       for d in _dispatched(state, prior["revision"], item["id"]))]
        _ownership([*live, *items])
        if len(allocation["decisions"]) >= MAX_DECISIONS:
            raise ValueError("allocation decision limit reached")
        revision = allocation["revision"] + 1
        allocation["decisions"].append({**decision, **_scope(state), "revision": revision})
        allocation.update(status="decided", revision=revision)
        state["updated_at"] = R._now()
        R._atomic(path, state)
        return summary(state)


def require_dispatch(state, work_item=None, executor_id=None):
    """Validate a developer request and return its planned binding; legacy stays open."""
    allocation = state.get("work_allocation")
    if allocation is None:
        return {}
    _active_task(state)
    if allocation["status"] != "decided":
        raise ValueError("allocation review pending before developer dispatch")
    decision = allocation["decisions"][-1]
    if decision["mode"] in {"direct", "verification"}:
        raise ValueError("allocation mode requires scope redecision before developer dispatch")
    item = next((item for item in decision["assignments"] if item["id"] == work_item), None)
    if item is None:
        raise ValueError("developer dispatch requires a planned work item")
    if executor_id is not None and executor_id != item["executor_id"]:
        raise ValueError("executor differs from planned executor alias")
    decisions = _current_decisions(state)
    revisions = {d["revision"] for d in decisions}
    def latest(key):
        matches = [d for d in state.get("role_dispatches", []) if d.get("role") == "developer"
                   and d.get("allocation_revision") in revisions and d.get("work_item") == key]
        return matches[-1] if matches else None
    previous = latest(work_item)
    if previous and previous.get("status") not in {"failed", "interrupted"}:
        raise ValueError("work item already has a dispatched binding")
    for dep in item["depends_on"]:
        previous = latest(dep)
        if not previous or previous.get("status") != "completed":
            raise ValueError("work item dependency has not completed")
    return {"allocation_revision": decision["revision"], "work_item": work_item,
            "planned_executor_id": item["executor_id"]}


def summary(state):
    """Bounded current-task view; only matching explicit dispatches advance plans."""
    allocation = state.get("work_allocation") if state else None
    result = {"status": "unknown", "mode": None, "reason": None, "revision": None,
              "items": [], "total_items": 0, "omitted_items": 0, "truncated": False,
              "counts": {}, "host_acceptance_is_caller_observed": True}
    if allocation is None:
        return result
    decisions = _current_decisions(state)
    result.update(status=allocation["status"], revision=allocation["revision"])
    if decisions:
        result.update({key: decisions[-1][key] for key in ("mode", "reason")})
    bindings = {(decision["revision"], item["id"]): item
                for decision in decisions for item in decision["assignments"]}
    items = {}
    for dispatch in state.get("role_dispatches", []):
        key = (dispatch.get("allocation_revision"), dispatch.get("work_item"))
        if dispatch.get("role") != "developer" or key not in bindings:
            continue
        item = bindings[key]
        status = {"dispatching": "requested", "working": "host_accepted", "completed": "completed",
                  "failed": "failed", "interrupted": "failed"}.get(dispatch.get("status"), "requested")
        items[item["id"]] = {"allocation_revision": key[0], "work_item": item["id"],
                             "executor_id": item["executor_id"], "status": status}
    if decisions and allocation["status"] != "pending":
        for item in decisions[-1]["assignments"]:
            items.setdefault(item["id"], {"allocation_revision": decisions[-1]["revision"],
                             "work_item": item["id"], "executor_id": item["executor_id"], "status": "planned"})
    # Actual live requests take precedence over closed attempts and unrequested plans.
    ordered = sorted(items.values(), key=lambda item: {"requested": 0, "host_accepted": 0,
                                                     "planned": 2}.get(item["status"], 1))
    for item in ordered:
        result["counts"][item["status"]] = result["counts"].get(item["status"], 0) + 1
    result.update(items=ordered[:SUMMARY_LIMIT], total_items=len(ordered),
                  omitted_items=max(0, len(ordered) - SUMMARY_LIMIT), truncated=len(ordered) > SUMMARY_LIMIT)
    return result
