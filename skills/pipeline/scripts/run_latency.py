"""Anonymous task-bounded observations; missing time is never called waste."""
import run_state as R
from workflow_metrics import interval_seconds

SAMPLE_KEYS = {"index", "wall_seconds", "check_union_seconds", "operation_union_seconds",
               "wait_union_seconds", "observed_union_seconds", "unattributed_seconds",
               "check_records", "reuse_records", "failure_records", "environment_mismatch_records",
               "invalid_timing_records", "pending_operation_records", "review_candidates"}
CANDIDATES = {"review_failed_checks", "review_environment_identity", "review_wait_or_handoff"}


def summarize(state, records):
    """Uses already-loaded safe metadata, no I/O or source/log/lineage inspection."""
    def time(value):
        return R._timestamp(value).timestamp()

    result = {"coverage": "recorded_only", "samples": [], "truncated": False}
    try:
        created, boundary = time(state["created_at"]), time(state["updated_at"])
        if boundary < created:
            raise ValueError("invalid bounds")
        tasks = state.get("tasks", [])
        if tasks:
            import run_tasks
            run_tasks._tasks(state)
            windows = [(i, time(t["started_at"]), time(t["ended_at"]) if t["ended_at"] else boundary)
                       for i, t in enumerate(tasks, 1)]
        else:
            windows = [(0, created, boundary)]
        if any(not created <= left <= right <= boundary for _, left, right in windows):
            raise ValueError("task bounds outside run")
    except (KeyError, ValueError, TypeError, OverflowError):
        return {**result, "coverage": "unknown"}
    history = state.get("history", [])
    waits, valid_history = [], bool(history)
    try:
        last = created
        for i, event in enumerate(history):
            at = time(event["at"])
            if not last <= at <= boundary:
                raise ValueError("nonmonotonic history")
            last = at
            if event.get("event") not in {"start", "transition", "resumed", *R.FINAL}:
                raise ValueError("unknown event")
            if event.get("event") in {"paused", "aborted", "completed"}:
                continue
            end = time(history[i + 1]["at"]) if i + 1 < len(history) else boundary
            if end < at or end > boundary:
                raise ValueError("invalid history interval")
            if event.get("activity") in {"human_wait", "role_wait", "handoff"}:
                waits.append((at, end))
    except (ValueError, KeyError, TypeError, OverflowError):
        waits, valid_history = [], False
    operations, valid_operations = [], True
    pending = []
    try:
        import operation_metrics as O
        for span in O._spans(state):
            left = time(span["start"])
            if not created <= left <= boundary:
                raise ValueError("invalid operation start")
            if span["end"] is None:
                pending.append(left)
                continue
            right = time(span["end"])
            if not created <= left <= right <= boundary:
                raise ValueError("invalid operation bounds")
            if span["status"] == "interrupted" or any(
                    left < time(e["at"]) <= right for e in history
                    if e.get("event") in R.FINAL | {"resumed"}):
                continue
            operations.append((left, right))
    except (ValueError, KeyError, TypeError, OverflowError):
        operations, pending, valid_operations = [], [], False

    for index, left, right in windows[-20:]:
        def inside_window(at):
            # Point records have one owner even with adjacent zero-length tasks.
            owner = max((i for i, a, b in windows if a <= at <= b), default=None)
            return owner == index
        def clip(intervals):
            return [(max(left, a), min(right, b)) for a, b in intervals if max(left, a) < min(right, b)]
        def union(intervals):
            return interval_seconds(clip(intervals))
        checks, reuse, failures, environment, invalid, count = [], 0, 0, 0, 0, 0
        for record in records:
            try:
                at = time(record["recorded_at"])
            except (KeyError, ValueError, TypeError, OverflowError):
                result["coverage"] = "partial"
                continue
            if not created <= at <= boundary:
                result["coverage"] = "partial"
                continue
            if record["kind"] == "check_reuse":
                reuse += int(inside_window(at))
                continue
            if record["kind"] != "check" or record["inherited"]:
                continue
            inside = inside_window(at)
            count += int(inside)
            failures += int(inside and record["failure"])
            environment += int(inside and record["environment_mismatch"])
            try:
                start, stop = time(record["started_at"]), time(record["finished_at"])
                if not created <= start <= stop <= at <= boundary:
                    raise ValueError("invalid check timing")
                checks.append((start, stop))
            except (KeyError, ValueError, TypeError, OverflowError):
                invalid += int(inside)
        observed = clip(checks) + clip(operations) + clip(waits)
        total = interval_seconds(observed)
        candidates = []
        if failures:
            candidates.append("review_failed_checks")
        if environment:
            candidates.append("review_environment_identity")
        if union(waits):
            candidates.append("review_wait_or_handoff")
        result["samples"].append({"index": index, "wall_seconds": right - left,
            "check_union_seconds": union(checks) if checks else None,
            "operation_union_seconds": union(operations) if operations and valid_operations else None,
            "wait_union_seconds": union(waits) if valid_history else None,
            "observed_union_seconds": total if observed else None,
            "unattributed_seconds": max(0.0, right - left - total),
            "check_records": count, "reuse_records": reuse, "failure_records": failures,
            "environment_mismatch_records": environment, "invalid_timing_records": invalid,
            "pending_operation_records": sum(inside_window(at) for at in pending),
            "review_candidates": candidates})
        if invalid or not valid_operations or not valid_history or pending:
            result["coverage"] = "partial"
    result["truncated"] = len(windows) > 20
    return result


def validate(value):
    if (not isinstance(value, dict) or set(value) != {"coverage", "samples", "truncated"}
            or value["coverage"] not in {"recorded_only", "partial", "unknown"}
            or type(value["truncated"]) is not bool or not isinstance(value["samples"], list)
            or len(value["samples"]) > 20):
        raise ValueError("invalid latency summary")
    import math
    for sample in value["samples"]:
        if (not isinstance(sample, dict) or set(sample) != SAMPLE_KEYS
                or not isinstance(sample["review_candidates"], list)
                or any(not isinstance(v, str) or v not in CANDIDATES for v in sample["review_candidates"])):
            raise ValueError("invalid latency sample")
        for key, child in sample.items():
            if key == "review_candidates":
                continue
            if child is None and key.endswith("seconds"):
                continue
            if type(child) not in (float, int) or not math.isfinite(child) or child < 0:
                raise ValueError("invalid latency measure")
