"""Run-bound local reports and check reuse. Does not run commands or models."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re

import run_state
from input_fingerprint import fingerprint as _input_fingerprint


class EvidenceError(ValueError):
    """Stable diagnostic code, preserving ValueError compatibility."""
    def __init__(self, reason, message):
        self.reason = reason
        super().__init__(message)


def _local(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("expected project-relative path without ..")
    path = root
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("symlinks are not evidence inputs")
    return path


def _label(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValueError("report kind/name must be a short lowercase identifier")
    return value


def coverage_labels(value=None):
    """Canonical caller-declared categories; labels never infer a PASS."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("coverage labels must be a list")
    return sorted({_label(item) for item in value})


def _no_owner_token(value):
    if isinstance(value, dict):
        if "owner_token" in value:
            raise ValueError("owner token must not appear in reports")
        for item in value.values():
            _no_owner_token(item)
    elif isinstance(value, list):
        for item in value:
            _no_owner_token(item)


def write_report(project_root, run_id, owner_token, kind, name, payload):
    """Create an immutable JSON envelope and register it with the owned run.

    JSON payload may contain Markdown. Report and state are separate atomic
    writes: if registration fails, preserve the report and inspect before retry.
    """
    _label(kind)
    _label(name)
    encoded_payload = json.dumps(payload, allow_nan=False)
    payload = json.loads(encoded_payload)  # Normalize tuples and custom JSON containers before checking.
    _no_owner_token(payload)
    if isinstance(owner_token, str) and owner_token and owner_token in encoded_payload:
        raise ValueError("owner token must not appear in report text")
    with run_state._write_guard(project_root) as base:
        state_path, state = run_state._load_owned(base, run_id, owner_token)
        if state["status"] != "running":
            raise ValueError("only active runs can record reports")
        root = Path(project_root).resolve(strict=True)
        relative = f"tmp/pipeline/runs/{run_id}/artifacts/{kind}/{name}.json"
        path = _local(root, relative)
        if path.exists():
            raise FileExistsError("report exists; use a new round/name")
        path.parent.mkdir(parents=True, exist_ok=True)
        report = {"schema_version": 1, "run_id": run_id, "kind": kind,
                  "recorded_at": run_state._now(), "payload": deepcopy(payload)}
        run_state._atomic(path, report)
        if relative not in state["artifacts"]:
            state["artifacts"].append(relative)
        state["updated_at"] = run_state._now()
        run_state._atomic(state_path, state)
    return relative


def read_report(project_root, run_id, relative, *, kind=None):
    """Reject legacy, cross-run and unbound reports rather than adopting them."""
    root, base = run_state._paths(project_root)
    run_state._state_path(base, run_id)  # validate identity without reading owner data
    path = _local(root, relative)
    namespace = base / "runs" / run_id / "artifacts"
    if not path.is_relative_to(namespace):
        raise ValueError("unconfirmed report: not in this run's artifact namespace")
    report = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(report, dict) or type(report.get("schema_version")) is not int
            or report["schema_version"] != 1 or report.get("run_id") != run_id
            or not isinstance(report.get("recorded_at"), str) or not report["recorded_at"]
            or "payload" not in report):
        raise ValueError("unconfirmed report: missing or mismatched run metadata")
    _label(report.get("kind"))
    if kind is not None and report["kind"] != kind:
        raise ValueError("report kind mismatch")
    return report


def fingerprint(project_root, scope):
    """Hash declared files/directories, including additions and missing paths.

    Callers choose the affected code/config/dependency scope. This helper cannot
    infer whether that declaration covers all semantic dependencies.
    """
    if not isinstance(scope, list) or not scope:
        raise ValueError("nonempty scope required")
    return _input_fingerprint(project_root, scope, narrow=True)


def _environment(environment):
    if (not isinstance(environment, dict) or not environment
            or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v
                   for k, v in environment.items())):
        raise ValueError("nonempty environment identity required; do not include secrets")
    return deepcopy(environment)


def record_check(project_root, run_id, owner_token, name, *, command, scope,
                 environment, exit_code, log_path, started_fingerprint, seconds=None, coverage=None):
    """Record an observed command result; never execute or infer a PASS."""
    if not isinstance(command, str) or not command.strip() or type(exit_code) is not int:
        raise ValueError("command and observed integer exit code required")
    if seconds is not None and (type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0):
        raise ValueError("seconds must be an observed nonnegative duration or None")
    coverage = coverage_labels(coverage)
    root = Path(project_root).resolve(strict=True)
    log = _local(root, log_path)
    current = fingerprint(root, scope)
    if not isinstance(started_fingerprint, str) or current != started_fingerprint:
        raise ValueError("inputs changed during check or pre-check fingerprint missing")
    payload = {"command": command, "scope": deepcopy(scope),
               "fingerprint": current, "environment": _environment(environment),
               "exit_code": exit_code, "seconds": seconds, "log_path": log_path, "coverage": coverage,
               "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest()}
    return write_report(root, run_id, owner_token, "check", name, payload)


def reusable_check(project_root, run_id, relative, *, command, environment, impact_covered=False, coverage=None):
    """Fail closed if scope coverage, run, inputs, environment or log is unknown."""
    if impact_covered is not True:
        return {"reusable": False, "reason": "impact_not_confirmed"}
    coverage = coverage_labels(coverage)
    try:
        state = run_state.status(project_root, run_id)["state"]
        if state is None or relative not in state.get("artifacts", []):
            return {"reusable": False, "reason": "unregistered_evidence"}
        payload = read_report(project_root, run_id, relative, kind="check")["payload"]
        validate_lineage(project_root, payload)
        if not _local(Path(project_root).resolve(strict=True), payload.get("cwd", ".")).is_dir():
            return {"reusable": False, "reason": "working_directory_unavailable"}
        if coverage_labels(payload.get("coverage")) != coverage:
            return {"reusable": False, "reason": "coverage_changed"}
        if payload.get("inputs_stable", True) is not True:
            return {"reusable": False, "reason": "inputs_changed_during_check"}
        if type(payload["exit_code"]) is not int or payload["exit_code"] != 0:
            return {"reusable": False, "reason": "check_not_passed"}
        if payload["command"] != command or payload["environment"] != _environment(environment):
            return {"reusable": False, "reason": "command_or_environment_changed"}
        if fingerprint(project_root, payload["scope"]) != payload["fingerprint"]:
            return {"reusable": False, "reason": "inputs_changed"}
        root = Path(project_root).resolve(strict=True)
        log = _local(root, payload["log_path"])
        if hashlib.sha256(log.read_bytes()).hexdigest() != payload["log_sha256"]:
            return {"reusable": False, "reason": "log_changed"}
    except EvidenceError as error:
        return {"reusable": False, "reason": error.reason}
    except (OSError, ValueError, KeyError, TypeError):
        return {"reusable": False, "reason": "unconfirmed_evidence"}
    return {"reusable": True, "reason": "same_run_inputs_environment_and_log"}


def validate_lineage(project_root, payload, seen=None):
    """Inherited results retain immutable source and acceptance references."""
    origin = payload.get("inherited_from")
    if origin is None:
        return
    import delivery
    root = Path(project_root).resolve(strict=True)
    check, receipt = origin["check"], origin["receipt"]
    key = (check["run_id"], check["path"])
    seen = set() if seen is None else set(seen)
    if key in seen or len(seen) >= 16:
        raise EvidenceError("delivery_lineage_invalid", "cyclic or excessive delivery lineage")
    seen.add(key)
    packet = delivery.validated_receipt(root, receipt, refresh_coverage=False)
    if check not in packet["checks"] or check["run_id"] != receipt["run_id"]:
        raise EvidenceError("delivery_check_not_accepted", "check not included in predecessor acceptance")
    previous = delivery._read_reference(root, check, "check")
    if ({k: v for k, v in payload.items() if k != "inherited_from"}
            != {k: v for k, v in previous.items() if k != "inherited_from"}):
        raise EvidenceError("inherited_result_changed", "inherited result changed")
    validate_lineage(root, previous, seen)


def write_paths_conflict(project_root, left, right):
    """Unknown write sets conflict; explicit disjoint sets may run concurrently."""
    if left is None or right is None:
        return True
    if not isinstance(left, list) or not isinstance(right, list):
        raise ValueError("write sets must be lists or unknown (None)")
    root = Path(project_root).resolve(strict=True)
    left_paths = [_local(root, item) for item in left]
    right_paths = [_local(root, item) for item in right]
    return any(a == b or a in b.parents or b in a.parents for a in left_paths for b in right_paths)
