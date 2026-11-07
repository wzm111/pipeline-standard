"""Lifecycle-only, privacy-filtered run snapshots. Never acceptance or reuse evidence."""
import argparse
from bisect import bisect_right
import hashlib
import json
import math
import os
from pathlib import Path
import re

from observed_checks import FAILURE_CAUSES, OPERATIONS
import run_state as R

RESULTS = {"success", "search_no_match", "path_lookup_failure", "reproduction_nonzero",
           "test_failure", "command_failure", "command_unavailable", "timeout", "interrupted",
           "environment_failure", "fixture_failure", "regression_failure", "unclassified"}
KINDS = {"check", "check_reuse", "check_review"}
QA_GATES = {"PASS", "FAIL", "CONCERNS"}
ROLE_NAMES = {"planner", "scope_guardian", "developer", "tester", "releaser"}
DISPATCH_STATES = {"dispatching", "working", "completed", "failed", "interrupted"}
REUSE_REASONS = {
    "impact_not_confirmed", "no_matching_check", "unconfirmed_evidence",
    "executable_unconfirmed", "scope_or_intent_changed", "superseded_observation",
    "command_or_environment_changed", "inputs_changed", "inputs_changed_during_check",
    "log_changed", "coverage_changed", "same_run_inputs_environment_and_log",
    "unregistered_evidence", "baseline_not_acceptance", "visual_not_acceptance",
    "working_directory_unavailable", "check_not_passed", "unknown",
}
RELEASE_LABELS = {"release_preflight", "runner_compatibility", "release_activation",
                  "release_smoke", "release_identity"}
NOTICE = "collection_only_not_acceptance_or_reuse_evidence"


class AuditError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _local(root, relative):
    path = root
    for part in Path(relative).parts:
        if part in {"..", "/"}:
            raise AuditError("unsafe_path")
        path = path / part
        if path.is_symlink():
            raise AuditError("unsafe_path")
    return path


def _paths(project_root, run_id):
    root = Path(project_root).resolve(strict=True)
    for relative in ("tmp", "tmp/pipeline", "tmp/pipeline/runs", ".pipeline.models.lock", "tmp/pipeline/.active"):
        _local(root, relative)
    root, base = R._paths(root)
    state_path = R._state_path(base, run_id)
    audit_path = _local(root, f"tmp/pipeline/runs/{run_id}/audit.json")
    return root, state_path, audit_path


def _hash(state):
    # Only the already loaded control document, never project sources or logs.
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _stamp(value):
    try:
        return R._timestamp(value).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _implementation():
    try:
        digest = hashlib.sha256()
        for name in ("run_audit.py", "run_latency.py", "run_state.py", "pipeline_cli.py"):
            path = Path(__file__).parent / name
            if path.is_symlink():
                return None
            digest.update(name.encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()
    except OSError:
        return None


def _timeline(state):
    totals = {name: 0.0 for name in sorted(R.ACTIVITIES | {"paused"})}
    history = state.get("history")
    result = {"coverage": "unknown", "activity_seconds": None, "tracked_seconds": None}
    if not isinstance(history, list) or not history:
        return result
    complete = isinstance(history[0], dict) and history[0].get("event") == "start" and history[0].get("at") == state.get("created_at")
    previous, cursor = None, None
    try:
        boundary = R._timestamp(state.get("updated_at"))
        for item in history:
            at = R._timestamp(item.get("at"))
            kind = item.get("event")
            if (kind not in {"start", "transition", "resumed", *R.FINAL}
                    or (previous is not None and at < previous) or at > boundary):
                return result
            if cursor is not None:
                if kind == "resumed" and cursor[1] != "paused":
                    complete = False  # recovered execution has no observed stop
                else:
                    totals[cursor[1]] += (at - cursor[0]).total_seconds()
            activity = "paused" if kind in {"paused", "aborted"} else item.get("activity")
            cursor = None if kind == "completed" else (at, activity)
            if cursor and activity not in totals:
                return result
            previous = at
        # Never extrapolate to scan time or infer a host crash boundary.
        if cursor and state.get("status") == "running":
            totals[cursor[1]] += (boundary - cursor[0]).total_seconds()
        result.update(coverage="complete" if complete else "partial", activity_seconds=totals,
                      tracked_seconds=sum(totals.values()))
    except (ValueError, TypeError, AttributeError, OverflowError):
        pass
    return result


def _counts(state, key, field, values):
    items = state.get(key)
    if not isinstance(items, list):
        return None
    return {value: sum(isinstance(item, dict) and item.get(field) == value for item in items)
            for value in sorted(values)}


def _log_source(payload):
    """Metadata-only shared-source candidate; never an execution/reuse identity."""
    fields = ("command", "scope", "environment", "fingerprint", "log_path", "log_sha256")
    if (not isinstance(payload.get("command"), str) or not payload["command"]
            or not isinstance(payload.get("scope"), list) or not payload["scope"]
            or any(not isinstance(p, str) for p in payload["scope"])
            or not isinstance(payload.get("environment"), dict) or not payload["environment"]
            or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v
                   for k, v in payload["environment"].items())
            or any(not isinstance(payload.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", payload[key])
                   for key in ("fingerprint", "log_sha256"))
            or not isinstance(payload.get("log_path"), str) or not payload["log_path"]):
        return None
    identity = {key: payload[key] for key in fields}
    identity.update(cwd=payload.get("cwd", "."), intent=payload.get("intent", "test"))
    return hashlib.sha256(json.dumps(identity, sort_keys=True,
                                     allow_nan=False).encode()).hexdigest()


def _checks(root, state, telemetry=None):
    result = {"executed": 0, "reused": 0, "inherited": 0, "reviews": 0,
              "unavailable": 0, "by_raw_result_class": {k: 0 for k in sorted(RESULTS)},
              "by_result_class": {k: 0 for k in sorted(RESULTS)}, "coverage": "unknown",
              "check_records": 0, "host_observation_records": 0,
              "shared_log_source_candidates": 0, "source_identity_unknown": 0}
    checks, reviews, sources = {}, {}, set()
    refs = state.get("artifacts")
    if not isinstance(refs, list):
        return result
    for ref in dict.fromkeys(item for item in refs if isinstance(item, str)):
        # Only registered, current-run check JSON envelopes; no arbitrary refs.
        match = re.fullmatch(r"tmp/pipeline/runs/([0-9a-f]{32})/artifacts/(check|check_reuse|check_review|qa|acceptance|check_plan)/[a-z][a-z0-9_-]{0,63}\.json", ref)
        if not match:
            if re.search(r"/artifacts/(?:check|check_reuse|check_review)/", ref):
                result["unavailable"] += 1
            continue
        try:
            if match[1] != state["run_id"]:
                raise AuditError("identity_mismatch")
            report = json.loads(_local(root, ref).read_text(encoding="utf-8"))
            if (report.get("schema_version") != 1 or report.get("run_id") != state["run_id"]
                    or report.get("kind") != match[2] or not isinstance(report.get("payload"), dict)
                    or _stamp(report.get("recorded_at")) is None):
                raise AuditError("invalid_report")
            payload = report["payload"]
            kind = match[2]
            if kind == "check_plan":
                declared = payload.get("checks")
                if (telemetry is not None and ref in state.get("check_plans", {}) and isinstance(declared, dict)
                        and any(isinstance(check, dict) and isinstance(check.get("coverage"), list)
                                and "release_activation" in check["coverage"] for check in declared.values())):
                    telemetry.append({"kind": kind, "recorded_at": report["recorded_at"]})
                continue
            if kind in {"qa", "acceptance"}:
                if telemetry is not None:
                    validation = payload.get("machine_validation")
                    telemetry.append({"kind": kind, "recorded_at": report["recorded_at"], "ref": ref,
                                      "gate": payload.get("gate") if isinstance(payload.get("gate"), str) else None,
                                      "qa_ref": payload.get("qa_ref"),
                                      "machine": isinstance(validation, dict) and type(validation.get("schema")) is int
                                      and validation["schema"] == 1,
                                      "task_started_at": _stamp(validation.get("task_started_at"))
                                      if isinstance(validation, dict) else None})
                continue
            if telemetry is not None and kind in {"check", "check_reuse"}:
                decision = payload.get("reuse_decision")
                decision = decision if isinstance(decision, dict) else {}
                changes = decision.get("environment_changes")
                changes = changes if isinstance(changes, dict) else {}
                category, reason = payload.get("result_class"), decision.get("reason")
                coverage = payload.get("coverage")
                release_labels = [label for label in RELEASE_LABELS if isinstance(coverage, list) and label in coverage]
                diagnostics = payload.get("host_diagnostics")
                diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
                telemetry.append({"kind": kind, "recorded_at": report["recorded_at"], "ref": ref,
                    "inherited": bool(payload.get("inherited_from")),
                    "started_at": _stamp(payload.get("started_at")),
                    "finished_at": _stamp(payload.get("finished_at")),
                    "failure": isinstance(category, str) and category in RESULTS - {"success", "search_no_match", "reproduction_nonzero", "unclassified"},
                    "result_class": category if isinstance(category, str) else None,
                    "reuse_reason": reason if isinstance(reason, str) else None,
                    "host": payload.get("observation_kind") == "host_tool",
                    "operation": diagnostics.get("operation"),
                    "failure_cause": diagnostics.get("failure_cause"),
                    "recovery": isinstance(diagnostics.get("recovery_of"), str),
                    "exit_code": payload.get("exit_code"),
                    "release": bool(release_labels), "release_labels": release_labels,
                    "environment_mismatch": changes.get("status") == "known" and bool(
                        changes.get("changed_keys") or changes.get("other_environment_changed"))})
            if kind == "check":
                result["check_records"] += 1
                if payload.get("observation_kind") == "host_tool":
                    result["host_observation_records"] += 1
                source = _log_source(payload)
                if source is None:
                    result["source_identity_unknown"] += 1
                elif source in sources:
                    result["shared_log_source_candidates"] += 1
                else:
                    sources.add(source)
            if kind == "check_reuse":
                result["reused"] += 1
            elif kind == "check_review":
                category = payload.get("result_class")
                if category not in RESULTS or not isinstance(payload.get("source"), str):
                    raise AuditError("invalid_report")
                reviews[payload["source"]] = category
                result["reviews"] += 1
            elif payload.get("inherited_from"):
                result["inherited"] += 1
            else:
                category = payload.get("result_class")
                checks[ref] = category if isinstance(category, str) and category in RESULTS else "unclassified"
        except (OSError, ValueError, TypeError, AttributeError):
            if match[2] in KINDS:
                result["unavailable"] += 1
    result["executed"] = len(checks)
    for ref, category in checks.items():
        result["by_raw_result_class"][category] += 1
        result["by_result_class"][reviews.get(ref, category)] += 1
    for record in telemetry or ():
        if record["kind"] == "check" and record["ref"] in reviews:
            record["result_class"] = reviews[record["ref"]]
    result["coverage"] = "partial" if result["unavailable"] else "recorded_only"
    return result


def _task_details(state, records):
    """Attribute observed events to recorded windows without exporting identities."""
    from workflow_metrics import interval_seconds

    def at(value):
        stamp = _stamp(value)
        return R._timestamp(stamp).timestamp() if stamp else None

    created, boundary = at(state.get("created_at")), at(state.get("updated_at"))
    if created is None or boundary is None or created > boundary:
        return None
    tasks = state.get("tasks") or []
    windows = [(i, at(task.get("started_at")), at(task.get("ended_at") or state["updated_at"]))
               for i, task in enumerate(tasks, 1)] if tasks else [(0, created, boundary)]
    if any(a is None or b is None or not created <= a <= b <= boundary for _, a, b in windows):
        return None
    if any(left[2] > right[1] for left, right in zip(windows, windows[1:])):
        return None
    windows = windows[-20:]
    starts = [a for _, a, _ in windows]
    samples = [{"index": i, "executed": 0, "reused": 0, "inherited": 0, "host_observation_records": 0,
                "by_result_class": {}, "reuse_reasons": {}, "dispatches": {}, "expert_attempts": 0,
                "expert_prepared": 0, "host_model_feedback": 0, "host_effort_feedback": 0,
                "role_union_seconds": None, "qa_gates": {}, "machine_qa_records": 0,
                "qa_pass_before_tester_return": 0, "qa_boundary_mismatch": 0,
                "qa_handoff": {**dict.fromkeys(QA_HANDOFF_TIMES), "acceptance_gates": {}, "linked_acceptances": 0},
                "host_diagnostics": {"failures_by_operation": {}, "failure_causes": {},
                                     "recovery_successes": 0, "recovery_failures": 0},
                "release_checks": 0, "release_phases": {}, "release_plan_prepared": None,
                "release_plan_before_checks": None,
                "release_closeout": None}
               for i, _, _ in windows]

    def slot(value):
        time = at(value)
        if time is None:
            return None
        index = bisect_right(starts, time) - 1
        return index if index >= 0 and time <= windows[index][2] else None

    def count(bucket, key):
        bucket[key] = bucket.get(key, 0) + 1

    def mark(index, key, instant):
        _, left, right = windows[index]
        if instant is not None and left <= instant <= right:
            handoff = samples[index]["qa_handoff"]
            offset = instant - left
            handoff[key] = max(handoff[key] or 0, offset)

    testers = [[] for _ in windows]
    first_release = [None for _ in windows]
    release_intervals = [{} for _ in windows]
    check_plans = [None for _ in windows]
    plan = state.get("release_plan") or {}
    closeout_ref = (plan.get("closeout") or {}).get("path")
    closeout_index = None
    spans = []
    # The existing case scope ties preparation to a task even when no dispatch followed.
    scope_indexes = {task.get("task_id"): i for i, task in enumerate(tasks, 1)} if tasks else {state["run_id"]: 0}
    sample_by_index = {sample["index"]: sample for sample in samples}
    for case in state.get("expert_advice_cases", []):
        scope = case.get("scope") or {}
        sample = sample_by_index.get(scope_indexes.get(scope.get("id")))
        if sample is not None and scope.get("kind") == ("task" if tasks else "run"):
            sample["expert_prepared"] += 1
    for dispatch in state.get("role_dispatches", []):
        index = slot(dispatch.get("requested_at"))
        role, status = dispatch.get("role"), dispatch.get("status")
        accepted, finished = at(dispatch.get("accepted_at")), at(dispatch.get("completed_at"))
        if accepted is not None and finished is not None and created <= accepted <= finished <= boundary:
            spans.append((accepted, finished))
        if index is None or role not in ROLE_NAMES or status not in DISPATCH_STATES:
            continue
        sample = samples[index]
        count(sample["dispatches"].setdefault(role, {}), status)
        sample["expert_attempts"] += int(bool(dispatch.get("advice_case_id")))
        if accepted is not None and accepted <= boundary:
            sample["host_model_feedback"] += int(dispatch.get("actual_model") not in (None, "", "unknown"))
            sample["host_effort_feedback"] += int(dispatch.get("actual_effort") not in (None, "", "unknown"))
        if role == "tester" and status == "completed" and finished is not None and finished <= boundary:
            testers[index].append(finished)
            mark(index, "last_tester_return_seconds", finished)

    for sample, (_, left, right) in zip(samples, windows):
        intervals = [(max(a, left), min(b, right)) for a, b in spans if max(a, left) < min(b, right)]
        if intervals:
            sample["role_union_seconds"] = interval_seconds(intervals)

    qa_records = {record["ref"]: record for record in records if record["kind"] == "qa"}
    for record in records:
        kind = record["kind"]
        # Inheritance and reuse are registration events, never new execution.
        started = record.get("started_at") if kind == "check" and not record["inherited"] else None
        index = slot(started or record["recorded_at"])
        if index is None:
            continue
        sample = samples[index]
        if kind == "check_plan":
            time = at(record["recorded_at"])
            if check_plans[index] is None or time < check_plans[index]:
                check_plans[index] = time
        elif kind == "qa":
            if record["ref"] == closeout_ref:
                closeout_index = index
            if record["gate"] in QA_GATES:
                count(sample["qa_gates"], record["gate"])
            sample["machine_qa_records"] += int(record["machine"])
            sample["qa_boundary_mismatch"] += int(
                bool(tasks) and record["machine"] and at(record["task_started_at"]) != windows[index][1])
            if record["gate"] == "PASS" and any(at(record["recorded_at"]) < end for end in testers[index]):
                sample["qa_pass_before_tester_return"] += 1
            mark(index, "last_qa_seconds", at(record["recorded_at"]))
        elif kind == "acceptance":
            handoff = sample["qa_handoff"]
            when = at(record["recorded_at"])
            if record["gate"] in QA_GATES:
                count(handoff["acceptance_gates"], record["gate"])
            mark(index, "last_acceptance_seconds", when)
            ref = record.get("qa_ref")
            linked = qa_records.get(ref) if isinstance(ref, str) else None
            if linked and slot(linked["recorded_at"]) == index and at(linked["recorded_at"]) <= when:
                handoff["linked_acceptances"] += 1
        elif kind == "check" and record["inherited"]:
            sample["inherited"] += 1
        elif kind in {"check", "check_reuse"}:
            sample["release_checks"] += int(record["release"])
            if record["release"] and started:
                time = at(started)
                if first_release[index] is None or time < first_release[index]:
                    first_release[index] = time
                finished = at(record["finished_at"])
                if finished is not None and time <= finished <= at(record["recorded_at"]):
                    end = min(finished, windows[index][2])
                    for label in record["release_labels"]:
                        release_intervals[index].setdefault(label, []).append((time, end))
            if kind == "check_reuse":
                sample["reused"] += 1
            else:
                sample["executed"] += 1
                sample["host_observation_records"] += int(record["host"])
                category = record["result_class"]
                count(sample["by_result_class"], category if category in RESULTS else "unclassified")
                if record["exit_code"] == 0:
                    mark(index, "last_check_success_seconds", at(record["finished_at"]))
                if record["host"] and type(record["exit_code"]) is int:
                    host = sample["host_diagnostics"]
                    if record["exit_code"] != 0:
                        operation, cause = record["operation"], record["failure_cause"]
                        count(host["failures_by_operation"], operation if isinstance(operation, str) and operation in OPERATIONS else "unknown")
                        count(host["failure_causes"], cause if isinstance(cause, str) and cause in FAILURE_CAUSES else "unknown")
                    if record["recovery"]:
                        count(host, "recovery_successes" if record["exit_code"] == 0 else "recovery_failures")
            reason = record["reuse_reason"]
            if reason is not None:
                count(sample["reuse_reasons"], reason if reason in REUSE_REASONS else "unknown")

    adapter_prepared = at(plan.get("prepared_at"))
    for attempt in plan.get("attempts", []):
        index = slot(attempt.get("started_at"))
        phase, status = attempt.get("phase"), attempt.get("status")
        if index is None or phase not in RELEASE_PHASES or status not in RELEASE_STATES:
            continue
        count(samples[index]["release_phases"].setdefault(phase, {}), status)
        time = at(attempt["started_at"])
        if first_release[index] is None or time < first_release[index]:
            first_release[index] = time
    for index, sample in enumerate(samples):
        if release_intervals[index]:
            sample["release_command_seconds"] = {
                label: interval_seconds(intervals) for label, intervals in sorted(release_intervals[index].items())}
        prepared_times = [time for time in (check_plans[index], adapter_prepared) if time is not None]
        prepared = min(prepared_times, default=None)
        if sample["release_checks"] or sample["release_phases"] or index == closeout_index or check_plans[index] is not None:
            if prepared is not None:
                sample["release_plan_prepared"] = prepared <= windows[index][2]
            elif not plan or sample["release_phases"]:
                sample["release_plan_prepared"] = bool(plan)
            if first_release[index] is not None and (prepared is not None or not plan):
                sample["release_plan_before_checks"] = prepared is not None and prepared <= first_release[index]
            if not closeout_ref and (plan or not prepared_times):
                sample["release_closeout"] = False
            elif closeout_index is not None:
                sample["release_closeout"] = index == closeout_index
    return samples


def _build(root, state):
    import run_policy
    import run_latency
    telemetry = []
    checks = _checks(root, state, telemetry)
    latency = run_latency.summarize(state, telemetry)
    if checks["unavailable"]:
        latency["coverage"] = "partial"
    tasks = state.get("tasks")
    coverage = state.get("delivery_status", {}).get("coverage", {})
    missing = coverage.get("missing")
    return {"schema_version": 1, "run_id": state["run_id"], "notice": NOTICE,
            "source_state_hash": _hash(state), "implementation_hash": _implementation(),
            "created_at": _stamp(state.get("created_at")), "snapshot_at": _stamp(state.get("updated_at")),
            "status": state["status"] if state.get("status") in R.FINAL | {"running"} else "unknown",
            "completeness": "partial" if state.get("status") == "running" else "recorded_only",
            "timing": _timeline(state),
            "tasks": {"count": len(tasks) if isinstance(tasks, list) else None,
                      "by_status": _counts(state, "tasks", "status", {"running", "execution_ended", "paused", "aborted"}),
                      "boundaries": [{"index": index, "started_at": _stamp(item.get("started_at")),
                                      "ended_at": _stamp(item.get("ended_at")),
                                      "status": item.get("status") if item.get("status") in
                                      {"running", "execution_ended", "paused", "aborted"} else "unknown"}
                                     for index, item in enumerate(tasks, 1) if isinstance(item, dict)]
                      if isinstance(tasks, list) else None},
            "roles": _counts(state, "role_events", "event", {"created", "reused", "stopped"}),
            "checks": checks,
            "latency": latency,
            "task_details": _task_details(state, telemetry),
            "adoption": run_policy.audit_summary(root, state),
            "coverage": {"missing_count": len(missing) if isinstance(missing, list) else None,
                         "complete": coverage.get("complete") if type(coverage.get("complete")) is bool else None},
            "actual_model": None, "tokens": None}


def _error(error):
    if isinstance(error, AuditError):
        return error.reason
    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, OSError):
        return "storage_error"
    return "invalid_data"


def capture(project_root, state):
    """Called only inside the owner's existing coordination lock; non-blocking."""
    try:
        root, path, target = _paths(project_root, state["run_id"])
        current = R._read(path)
        R._owned(current, state["run_id"], state["owner_token"])
        if current != state:
            raise AuditError("state_changed")
        document = _build(root, state)
        if target.exists():
            existing = json.loads(target.read_text(encoding="utf-8"))
            if existing.get("run_id") != state["run_id"] or existing.get("schema_version") != 1:
                raise AuditError("identity_mismatch")
            _validate_document(existing)
        target.parent.mkdir(parents=True, exist_ok=True)
        _paths(project_root, state["run_id"])
        R._atomic(target, document)
        return {"status": "recorded"}
    except Exception as error:
        return {"status": "error", "error": _error(error)}


# Stored files are untrusted. A recursive closed vocabulary prevents even a
# hand-edited audit from becoming a channel for goals, paths or credentials.
KEYS = {"schema_version", "run_id", "notice", "source_state_hash", "implementation_hash",
        "created_at", "snapshot_at", "status", "completeness", "timing", "tasks", "roles",
        "checks", "coverage", "actual_model", "tokens", "activity_seconds", "tracked_seconds",
        "count", "boundaries", "executed", "reused", "inherited", "reviews", "unavailable",
        "by_raw_result_class", "by_result_class", "missing_count", "complete", "created", "stopped",
        "by_status", "index", "started_at", "ended_at", "execution_ended", "running",
        "check_records", "host_observation_records", "shared_log_source_candidates", "source_identity_unknown",
        "adoption", "mode", "managed", "current", "caller_acknowledged", "stale",
        *R.ACTIVITIES, *R.FINAL, *RESULTS}
import run_latency as L
KEYS |= {"latency", "samples", "truncated", *L.SAMPLE_KEYS}
DETAIL_COUNTS = {"index", "executed", "reused", "inherited", "host_observation_records",
                 "expert_attempts", "expert_prepared", "host_model_feedback", "host_effort_feedback",
                 "machine_qa_records", "qa_pass_before_tester_return",
                 "qa_boundary_mismatch", "release_checks"}
DETAIL_FLAGS = {"release_plan_prepared", "release_plan_before_checks", "release_closeout"}
DETAIL_BUCKETS = {"by_result_class": RESULTS, "reuse_reasons": REUSE_REASONS, "qa_gates": QA_GATES}
QA_HANDOFF_TIMES = {"last_check_success_seconds", "last_tester_return_seconds",
                    "last_qa_seconds", "last_acceptance_seconds"}
RELEASE_PHASES = {"preflight", "compatibility", "build", "activate", "smoke"}
RELEASE_STATES = {"running", "passed", "failed"}
WORDS = {NOTICE, "partial", "recorded_only", "unknown", "complete", "running", "execution_ended", "managed", "legacy_untracked", *R.FINAL}
WORDS |= L.CANDIDATES


def _validate(value, key=None):
    if isinstance(value, dict):
        if value.keys() - KEYS:
            raise AuditError("invalid_audit")
        for name, child in value.items():
            _validate(child, name)
    elif isinstance(value, list) and key in {"boundaries", "samples", "review_candidates"}:
        for child in value:
            _validate(child)
    elif value is None or type(value) is bool:
        return
    elif type(value) in {int, float}:
        if not math.isfinite(value) or value < 0:
            raise AuditError("invalid_audit")
    elif isinstance(value, str):
        valid = (re.fullmatch(r"[0-9a-f]{32}", value) if key == "run_id" else
                 re.fullmatch(r"[0-9a-f]{64}", value) if key in {"source_state_hash", "implementation_hash"} else
                 _stamp(value) == value if key in {"created_at", "snapshot_at", "started_at", "ended_at"} else value in WORDS)
        if not valid:
            raise AuditError("invalid_audit")
    else:
        raise AuditError("invalid_audit")


def _validate_document(document):
    if not isinstance(document, dict):
        raise AuditError("invalid_audit")
    _validate({k: v for k, v in document.items() if k != "task_details"})
    required = {"schema_version", "run_id", "notice", "source_state_hash", "implementation_hash",
                "created_at", "snapshot_at", "status", "completeness", "timing", "tasks", "roles",
                "checks", "coverage", "actual_model", "tokens"}
    if (not required <= set(document)
            or set(document) - required - {"adoption", "latency", "task_details"}
            or type(document["schema_version"]) is not int or document["schema_version"] != 1
            or document["notice"] != NOTICE or document["actual_model"] is not None or document["tokens"] is not None
            or document["status"] not in R.FINAL | {"running", "unknown"}
            or document["completeness"] not in {"partial", "recorded_only"}):
        raise AuditError("invalid_audit")
    if document.get("task_details") is not None:
        _validate_details(document["task_details"])
    if "latency" in document:
        try:
            L.validate(document["latency"])
        except (ValueError, TypeError):
            raise AuditError("invalid_audit") from None
    if "adoption" in document:
        adoption = document["adoption"]
        if (not isinstance(adoption, dict)
                or set(adoption) != {"mode", "managed", "current", "caller_acknowledged", "stale"}
                or adoption["mode"] not in {"managed", "legacy_untracked", "unknown"}
                or any(value is not None and type(value) is not bool
                       for key, value in adoption.items() if key != "mode")):
            raise AuditError("invalid_audit")
    for key in ("source_state_hash", "implementation_hash"):
        if document[key] is None and key == "implementation_hash":
            continue
        if not isinstance(document[key], str) or not re.fullmatch(r"[0-9a-f]{64}", document[key]):
            raise AuditError("invalid_audit")
    for key in ("created_at", "snapshot_at"):
        if document[key] is not None and not isinstance(document[key], str):
            raise AuditError("invalid_audit")


def _validate_details(details):
    def counts(value, keys):
        return isinstance(value, dict) and value.keys() <= keys and all(type(n) is int and n >= 0 for n in value.values())

    if not isinstance(details, list) or len(details) > 20:
        raise AuditError("invalid_audit")
    expected = DETAIL_COUNTS | DETAIL_FLAGS | DETAIL_BUCKETS.keys() | {"dispatches", "release_phases", "role_union_seconds"}
    for sample in details:
        if (not isinstance(sample, dict) or not expected <= sample.keys()
                or sample.keys() - expected - {"qa_handoff", "host_diagnostics", "release_command_seconds"}):
            raise AuditError("invalid_audit")
        seconds = sample["role_union_seconds"]
        if (not counts({k: sample[k] for k in DETAIL_COUNTS}, DETAIL_COUNTS)
                or any(sample[k] is not None and type(sample[k]) is not bool for k in DETAIL_FLAGS)
                or any(not counts(sample[k], keys) for k, keys in DETAIL_BUCKETS.items())
                or (seconds is not None and (type(seconds) not in {int, float} or not math.isfinite(seconds) or seconds < 0))):
            raise AuditError("invalid_audit")
        for key, groups, statuses in (("dispatches", ROLE_NAMES, DISPATCH_STATES),
                                      ("release_phases", RELEASE_PHASES, RELEASE_STATES)):
            if (not isinstance(sample[key], dict) or sample[key].keys() - groups
                    or any(not counts(value, statuses) for value in sample[key].values())):
                raise AuditError("invalid_audit")
        if "qa_handoff" in sample:
            handoff = sample["qa_handoff"]
            if (not isinstance(handoff, dict) or handoff.keys() != QA_HANDOFF_TIMES | {"acceptance_gates", "linked_acceptances"}
                    or not counts(handoff["acceptance_gates"], QA_GATES)
                    or not counts({"linked_acceptances": handoff["linked_acceptances"]}, {"linked_acceptances"})
                    or any(value is not None and (type(value) not in {int, float} or not math.isfinite(value) or value < 0)
                           for value in (handoff[key] for key in QA_HANDOFF_TIMES))):
                raise AuditError("invalid_audit")
        if "host_diagnostics" in sample:
            host = sample["host_diagnostics"]
            outcomes = {"recovery_successes", "recovery_failures"}
            if (not isinstance(host, dict) or host.keys() != outcomes | {"failures_by_operation", "failure_causes"}
                    or not counts({key: host[key] for key in outcomes}, outcomes)
                    or not counts(host["failures_by_operation"], OPERATIONS | {"unknown"})
                    or not counts(host["failure_causes"], FAILURE_CAUSES | {"unknown"})):
                raise AuditError("invalid_audit")
        if "release_command_seconds" in sample:
            durations = sample["release_command_seconds"]
            if (not isinstance(durations, dict) or durations.keys() - RELEASE_LABELS
                    or any(type(value) not in {int, float} or not math.isfinite(value) or value < 0
                           for value in durations.values())):
                raise AuditError("invalid_audit")


def audit(project_root, run_id, *, live=False):
    """Read existing snapshots only; never repairs, adopts, hashes logs or writes."""
    try:
        _, state_path, target = _paths(project_root, run_id)
        state = R._read(state_path)
        if state["run_id"] != run_id:
            raise AuditError("identity_mismatch")
        current = _build(Path(project_root).resolve(strict=True), state) if live else None
        if not target.exists():
            if R._read(state_path) != state:
                raise AuditError("state_changed")
            result = {"run_id": run_id, "status": "missing", "legacy": True}
            if live:
                result["live"] = current
            return result
        document = json.loads(target.read_text(encoding="utf-8"))
        _validate_document(document)
        if (not isinstance(document, dict) or document.get("schema_version") != 1
                or document.get("run_id") != run_id):
            raise AuditError("identity_mismatch")
        # Detect state replacement or concurrent writes without acquiring a lock.
        if R._read(state_path) != state:
            raise AuditError("state_changed")
        result = {"run_id": run_id, "status": "current" if document.get("source_state_hash") == _hash(state) else "stale",
                  "audit": document}
        if live:
            result["live"] = current
        return result
    except Exception as error:
        return {"run_id": run_id if isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{32}", run_id) else None,
                "status": "error", "error": _error(error)}


def scan(project_root, limit=20, *, live=False):
    if type(limit) is not int or not 1 <= limit <= 100:
        return {"status": "error", "error": "invalid_limit"}
    try:
        root = Path(project_root).resolve(strict=True)
        for relative in ("tmp", "tmp/pipeline", "tmp/pipeline/runs"):
            _local(root, relative)
        root, base = R._paths(root)
        directory = _local(root, "tmp/pipeline/runs")
        identities, truncated = [], False
        if directory.exists():
            with os.scandir(directory) as entries:
                for number, entry in enumerate(entries):
                    if number >= 1000:
                        truncated = True
                        break
                    if re.fullmatch(r"[0-9a-f]{32}\.json", entry.name):
                        identities.append((entry.stat(follow_symlinks=False).st_mtime_ns, entry.name[:-5]))
        identities.sort(reverse=True)
        return {"status": "ok", "runs": [audit(root, identity, live=live) for _, identity in identities[:limit]],
                "truncated": truncated or len(identities) > limit, "limit": limit}
    except Exception as error:
        return {"status": "error", "error": _error(error)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["audit", "scan"])
    parser.add_argument("--project", default=".")
    parser.add_argument("--run-id")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    result = audit(args.project, args.run_id, live=args.live) if args.action == "audit" else scan(args.project, args.limit)
    print(json.dumps(result, sort_keys=True))
    return int(result["status"] == "error")


if __name__ == "__main__":
    raise SystemExit(main())
