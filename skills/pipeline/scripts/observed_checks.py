"""Tickets for one explicitly observed host-tool call; never launch a tool.

Call begin immediately before the host call, then finish only after its return
or confirmed stop. Logs must already be redacted by the owning project.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

import run_checks as C
import run_evidence as E
import run_state as R


def _safe(value, owner):
    encoded = json.dumps(value, allow_nan=False, sort_keys=True)
    E._no_owner_token(value)
    if owner and owner in encoded:
        raise ValueError("owner token must not appear in observation data")
    return json.loads(encoded)


def _observed_time(value):
    """Only accept the canonical timezone-aware timestamps written by this API."""
    if not isinstance(value, str):
        raise ValueError("observation timing is missing or invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("observation timing is missing or invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        raise ValueError("observation timing must be canonical and timezone-aware")
    return parsed


def _validate_timing(report):
    payload = report["payload"]
    started = _observed_time(payload.get("started_at"))
    ended = _observed_time(payload.get("finished_at"))
    seconds = payload.get("seconds")
    if (type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0
            or ended < started or seconds != (ended - started).total_seconds()
            or report.get("recorded_at") != payload["finished_at"]
            or ended > _observed_time(R._now())):
        raise ValueError("observation timing is inconsistent")


def begin(project_root, run_id, owner_token, observation_id, *, command, scope,
          environment, coverage=None, write_paths=None, intent="test"):
    """Persist start and reserve resources before exactly one host invocation.

    The explicit ID identifies the invocation, not its log content. Repeating
    begin only retrieves the existing ticket; it never authorizes another call.
    Host environment values are hashed as a whole and never saved in plaintext.
    """
    root = Path(project_root).resolve(strict=True)
    E._label(observation_id)
    if not isinstance(command, str) or not command.strip():
        raise ValueError("declared host command required")
    C.classify_result(0, intent)
    declaration = _safe({"command": command, "scope": scope, "intent": intent,
                         "coverage": E.coverage_labels(coverage), "write_paths": write_paths}, owner_token)
    env = _safe(E._environment(environment), owner_token)
    declaration["environment"] = {"host_identity_sha256": hashlib.sha256(
        json.dumps(env, sort_keys=True).encode()).hexdigest()}
    with R._write_guard(root) as base:
        path, state = R._load_owned(base, run_id, owner_token)
        before = E.fingerprint(root, declaration["scope"])
        declaration["fingerprint"] = before
        E.write_paths_conflict(root, write_paths, [])
        tickets = state.setdefault("observed_checks", {})
        if observation_id in tickets:
            ticket = tickets[observation_id]
            if ticket["declaration"] != declaration:
                raise ValueError("observation ID already has a different declaration or inputs")
            return deepcopy(ticket)
        C._claim_state(root, run_id, state, observation_id, write_paths)
        ticket = {"observation_id": observation_id, "status": "running",
                  "started_at": R._now(), "declaration": declaration}
        tickets[observation_id] = ticket
        state["updated_at"] = R._now()
        R._atomic(path, state)
        return deepcopy(ticket)


def finish(project_root, run_id, owner_token, observation_id, *, exit_code, log_path,
           host_stopped=False, stop_evidence=None, error=None):
    """Record a real return/stop, restore progress, and return its immutable report.

    Invalid logs/inputs leave the ticket held so the caller can explicitly close
    it with truthful evidence. Input drift is recorded as unusable evidence.
    If report registration fails, the claim remains; retry adopts only the exact
    matching orphan report instead of inventing another observation or duration.
    """
    root = Path(project_root).resolve(strict=True)
    E._label(observation_id)
    R._stopped(host_stopped, stop_evidence)
    _safe({"log_path": log_path, "stop_evidence": stop_evidence}, owner_token)
    with R._write_guard(root) as base:
        state_path, state = R._load_owned(base, run_id, owner_token)
        ticket = state.get("observed_checks", {}).get(observation_id)
        if ticket is None:
            raise ValueError("unknown observation ID; begin before invoking the host")
        if ticket["status"] not in {"running", "completed"}:
            raise ValueError("closed observation cannot record another result")
        if ticket["status"] == "running" and not any(
                c["name"] == observation_id for c in state.get("running_checks", [])):
            raise ValueError("observation claim was recovered; explicitly begin a new invocation")
        declaration = ticket["declaration"]
        result_class = C.classify_result(exit_code, declaration["intent"], error)
        if error is not None and exit_code == 0:
            raise ValueError("a host failure cannot have a successful exit code")
        log = E._local(root, log_path)
        if not log.is_file():
            raise ValueError("observed log must be an existing project file")
        log_hash = hashlib.sha256(log.read_bytes()).hexdigest()
        current = E.fingerprint(root, declaration["scope"])
        result = _safe({"exit_code": exit_code, "result_class": result_class,
                        "log_path": log_path, "log_sha256": log_hash,
                        "inputs_stable": current == declaration["fingerprint"],
                        "finished_fingerprint": current, "stop_evidence": stop_evidence}, owner_token)
        relative = f"tmp/pipeline/runs/{run_id}/artifacts/check/{observation_id}.json"
        report_path = E._local(root, relative)
        payload = {**declaration, **result, "observation_id": observation_id,
                   "observation_kind": "host_tool", "started_at": ticket["started_at"]}
        if report_path.exists():
            report = E.read_report(root, run_id, relative, kind="check")
            _validate_timing(report)
            old = report["payload"]
            if {k: v for k, v in old.items() if k not in {"finished_at", "seconds"}} != payload:
                raise ValueError("observation resubmission differs from its recorded result")
            if ticket["status"] == "completed":
                if relative not in state["artifacts"] or ticket.get("report") != relative:
                    raise ValueError("completed observation is not registered")
                return relative
        else:
            if ticket["status"] != "running":
                raise ValueError("closed observation cannot record another result")
            if not any(c["name"] == observation_id for c in state.get("running_checks", [])):
                raise ValueError("observation claim was recovered; explicitly begin a new invocation")
            ended = R._now()
            seconds = (_observed_time(ended) - _observed_time(ticket["started_at"])).total_seconds()
            if seconds < 0:
                raise ValueError("observation clock moved backwards")
            payload.update(finished_at=ended, seconds=seconds)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            R._atomic(report_path, {"schema_version": 1, "run_id": run_id, "kind": "check",
                                    "recorded_at": ended, "payload": payload})
        # Only one state write commits registration, completion and release. A
        # failed write leaves the original running ticket/claim for exact retry.
        if relative not in state["artifacts"]:
            state["artifacts"].append(relative)
        ticket.update(status="completed", report=relative)
        C._release_state(state, observation_id)
        state["updated_at"] = R._now()
        R._atomic(state_path, state)
        return relative
