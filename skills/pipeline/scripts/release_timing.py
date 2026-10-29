"""Optional timing of existing release operations; never runs or accepts a release.

Use step() around work already required by a project's deploy script. A
monolithic command without inner observations remains partially attributed.
"""
from contextlib import contextmanager

import operation_metrics as O
import run_evidence as E
import run_state as R
from workflow_metrics import interval_seconds

STEPS = {"dependencies", "gates", "build", "transfer", "readiness", "activate", "smoke"}


@contextmanager
def step(root, run_id, owner, name):
    if name not in STEPS:
        raise ValueError("unknown release timing step")
    with O.span(root, run_id, owner, "verify", "release_" + name) as identity:
        yield identity


def _clip(intervals, bounds):
    return [(max(left, start), min(right, stop)) for left, right in intervals
            for start, stop in bounds if max(left, start) < min(right, stop)]


def summary(root, run_id, *, check_ref=None):
    """Union observations, excluding human waiting and inherited execution time.

    These are observed wall intervals, not CPU time. Neither a timed step nor
    an exit code proves deployment/QA acceptance. Unknown time is not waste.
    """
    snapshot = R.status(root, run_id)
    state = snapshot["state"]
    timing = R.timing(root, run_id)
    result = {"run_id": run_id, "coverage": "unknown", "reason": "release_timing_missing",
              "step_seconds": {key: None for key in sorted(STEPS)},
              "pending_steps": [], "unmeasured_steps": sorted(STEPS), "status_counts": {},
              "measured_step_seconds": None, "command_seconds": None,
              "unattributed_command_seconds": None, "reused_reports_excluded": 0}
    if state is None or timing["coverage"] == "unknown":
        return result
    now = R._timestamp(timing["measured_at"])
    bounds = [(R._timestamp(i["start"]).timestamp(), R._timestamp(i["end"]).timestamp())
              for i in timing["intervals"] if i["activity"] != "human_wait"]
    groups = {key: [] for key in STEPS}
    try:
        spans = O._spans(state)
        for item in spans:
            name = item["label"].removeprefix("release_")
            if item["category"] != "verify" or item["label"] != "release_" + name or name not in STEPS:
                continue
            result["status_counts"][item["status"]] = result["status_counts"].get(item["status"], 0) + 1
            if item["end"] is None:
                result["pending_steps"].append(name)
                continue
            start, stop = R._timestamp(item["start"]), R._timestamp(item["end"])
            if stop > now:
                raise ValueError("future release observation")
            if item["status"] in {"interrupted", "reused"} or O._interrupted(state, start, stop):
                continue
            groups[name].extend(_clip([(start.timestamp(), stop.timestamp())], bounds))
        references = {a["report"] for a in state.get("release_plan", {}).get("attempts", [])}
        if check_ref is not None:
            references.add(check_ref)
        commands = []
        for ref in references:
            if ref not in state.get("artifacts", []):
                raise ValueError("release timing check must be registered to this run")
            payload = E.read_report(root, run_id, ref, kind="check")["payload"]
            if payload.get("inherited_from"):
                result["reused_reports_excluded"] += 1
                continue
            start, stop = R._timestamp(payload["started_at"]), R._timestamp(payload["finished_at"])
            if stop < start or stop > now or start < R._timestamp(state["created_at"]):
                raise ValueError("invalid release command interval")
            commands.extend(_clip([(start.timestamp(), stop.timestamp())], bounds))
    except (KeyError, TypeError, ValueError) as error:
        if check_ref is not None:
            raise ValueError("invalid current-run release timing evidence") from error
        result["reason"] = "invalid_release_timing_history"
        return result
    if R.status(root, run_id) != snapshot:
        result["reason"] = "concurrent_state_change"
        return result
    measured = [i for items in groups.values() for i in items]
    result["pending_steps"] = sorted(set(result["pending_steps"]))
    result["step_seconds"] = {key: interval_seconds(items) if items else None for key, items in sorted(groups.items())}
    result["unmeasured_steps"] = sorted(key for key, items in groups.items() if not items)
    if measured:
        result["measured_step_seconds"] = interval_seconds(measured)
    if commands:
        result["command_seconds"] = interval_seconds(commands)
        result["unattributed_command_seconds"] = max(0, interval_seconds(commands) - interval_seconds(_clip(measured, commands)))
    if measured or commands:
        result.update(coverage="partial", reason="observed_wall_intervals_only")
    return result
