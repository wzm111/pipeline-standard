"""Explicit owned operation spans, not hooks for native host tools."""
from contextlib import contextmanager
from copy import deepcopy
import re
import uuid

import run_state as R
from workflow_metrics import interval_seconds

CATEGORIES = {"read", "write", "locate", "handoff", "git", "network", "verify", "other"}
STATUSES = {"pending", "completed", "executed", "reused", "failed", "interrupted"}


def _label(category, label):
    if not isinstance(category, str) or category not in CATEGORIES:
        raise ValueError("unknown operation category")
    if not isinstance(label, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", label):
        raise ValueError("operation label must be a safe short identifier")


def _spans(state):
    spans = state.get("operation_spans", [])
    if not isinstance(spans, list):
        raise ValueError("operation spans must be a list")
    seen = set()
    for item in spans:
        if not isinstance(item, dict) or set(item) != {"id", "category", "label", "start", "end", "status"}:
            raise ValueError("invalid operation span")
        _label(item["category"], item["label"])
        if not isinstance(item["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", item["id"]) or item["id"] in seen:
            raise ValueError("invalid or duplicate operation id")
        seen.add(item["id"])
        if item["status"] not in STATUSES:
            raise ValueError("invalid operation status")
        start = R._timestamp(item["start"])
        if item["status"] == "pending":
            if item["end"] is not None:
                raise ValueError("pending span cannot have an end")
        elif R._timestamp(item["end"]) < start:
            raise ValueError("operation end precedes start")
    return spans


def _owned(base, run_id, owner):
    path, state = R._load_owned(base, run_id, owner)
    if state["status"] != "running":
        raise ValueError("operations require an active running owner")
    _spans(state)
    return path, state


def _clock(state, now):
    instant = R._timestamp(now)
    latest = [state.get("updated_at"), *(item[key] for item in state.get("operation_spans", [])
                                        for key in ("start", "end") if item[key] is not None)]
    if any(R._timestamp(value) > instant for value in latest if value is not None):
        raise ValueError("operation clock moved backwards")
    return instant


def _interrupted(state, start, stop):
    return any(start < R._timestamp(event.get("at")) <= stop for event in state.get("history", [])
               if isinstance(event, dict) and event.get("event") in R.FINAL | {"resumed"})


def begin(root, run_id, owner, category, label):
    _label(category, label)
    if isinstance(owner, str) and owner and owner in label:
        raise ValueError("owner must not appear in operation label")
    with R._write_guard(root) as base:
        path, state = _owned(base, run_id, owner)
        now = R._now()
        _clock(state, now)
        item = {"id": uuid.uuid4().hex, "category": category, "label": label,
                "start": now, "end": None, "status": "pending"}
        state.setdefault("operation_spans", []).append(item)
        state["updated_at"] = now
        R._atomic(path, state)
        return item["id"]


def end(root, run_id, owner, span_id, *, status="completed"):
    if not isinstance(status, str) or status not in STATUSES - {"pending"}:
        raise ValueError("invalid closed operation status")
    with R._write_guard(root) as base:
        path, state = _owned(base, run_id, owner)
        item = next((item for item in state.get("operation_spans", []) if item["id"] == span_id), None)
        if item is None:
            raise ValueError("operation span does not belong to this run")
        if item["status"] != "pending":
            return deepcopy(item)
        now = R._now()
        instant = _clock(state, now)
        if _interrupted(state, R._timestamp(item["start"]), instant):
            status = "interrupted"
        item.update(end=now, status=status)
        state["updated_at"] = now
        R._atomic(path, state)
        return deepcopy(item)


def _failed(root, run_id, owner, span_id, error):
    try:
        end(root, run_id, owner, span_id,
            status="interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed")
    except Exception:
        # Ownership or storage may be unavailable after an interruption. Keep
        # the pending span and preserve the original callback/cleanup error.
        pass


@contextmanager
def span(root, run_id, owner, category, label):
    span_id = begin(root, run_id, owner, category, label)
    try:
        yield span_id
    except BaseException as error:
        _failed(root, run_id, owner, span_id, error)
        raise
    else:
        end(root, run_id, owner, span_id)


def call(root, run_id, owner, category, label, callback, *args, result_status=None, **kwargs):
    if not callable(callback) or (result_status is not None and not callable(result_status)):
        raise ValueError("operation callback and optional result classifier must be callable")
    span_id = begin(root, run_id, owner, category, label)
    try:
        result = callback(*args, **kwargs)
        status = result_status(result) if result_status else "completed"
    except BaseException as error:
        _failed(root, run_id, owner, span_id, error)
        raise
    end(root, run_id, owner, span_id, status=status)
    return result


def summary(root, run_id=None):
    """Union closed observations clipped to known active run intervals.

    Pending spans have no measured duration. Nested and parallel categories
    may overlap; their category totals must not be added as wall time.
    """
    snapshot = R.status(root, run_id)
    state = snapshot["state"]
    timing = R.timing(root, run_id)
    result = {"run_id": state.get("run_id") if state else run_id, "measured_at": timing["measured_at"],
              "coverage": "unknown", "reason": "operation_history_missing", "recorded_span_count": 0,
              "pending_span_count": 0, "status_counts": {}, "category_seconds": {key: None for key in sorted(CATEGORIES)},
              "attributed_seconds": None, "unattributed_seconds": None, "run_tracked_seconds": None}
    if state is None:
        result["reason"] = timing["reason"]
        return result
    try:
        spans = _spans(state)
        now = R._timestamp(timing["measured_at"])
        if any(R._timestamp(item["end"] or item["start"]) > now for item in spans):
            raise ValueError("future operation timestamp")
    except (ValueError, TypeError):
        result["reason"] = "invalid_operation_history"
        return result
    result["recorded_span_count"] = len(spans)
    for item in spans:
        result["status_counts"][item["status"]] = result["status_counts"].get(item["status"], 0) + 1
    result["pending_span_count"] = result["status_counts"].get("pending", 0)
    if timing["coverage"] == "unknown":
        result["reason"] = timing["reason"]
        return result
    bounds = [(R._timestamp(item["start"]).timestamp(), R._timestamp(item["end"]).timestamp())
              for item in timing["intervals"]]
    categories = {key: [] for key in CATEGORIES}
    closed = False
    for item in spans:
        if item["end"] is None:
            continue
        if _interrupted(state, R._timestamp(item["start"]), R._timestamp(item["end"])):
            continue
        closed = True
        start, stop = (R._timestamp(item[key]).timestamp() for key in ("start", "end"))
        for left, right in bounds:
            low, high = max(start, left), min(stop, right)
            if low <= high:
                categories[item["category"]].append((low, high))
    intervals = [item for spans in categories.values() for item in spans]
    attributed = interval_seconds(intervals)
    result["category_seconds"] = {key: interval_seconds(spans) if spans else None for key, spans in sorted(categories.items())}
    result["attributed_seconds"] = attributed if closed else None
    result["run_tracked_seconds"] = timing["tracked_seconds"]
    result["unattributed_seconds"] = max(0.0, timing["tracked_seconds"] - attributed)
    if spans:
        result["coverage"] = ("complete" if timing["coverage"] == "complete" and not result["pending_span_count"]
                              and result["unattributed_seconds"] < 1e-9 else "partial")
        result["reason"] = "observed_operation_spans"
    return result
