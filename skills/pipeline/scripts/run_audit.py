"""Lifecycle-only, privacy-filtered run snapshots. Never acceptance or reuse evidence."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re

import run_state as R

RESULTS = {"success", "search_no_match", "path_lookup_failure", "reproduction_nonzero",
           "test_failure", "command_failure", "command_unavailable", "timeout", "interrupted",
           "environment_failure", "fixture_failure", "regression_failure", "unclassified"}
KINDS = {"check", "check_reuse", "check_review"}
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
        for name in ("run_audit.py", "run_state.py", "pipeline_cli.py"):
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


def _checks(root, state):
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
        match = re.fullmatch(r"tmp/pipeline/runs/([0-9a-f]{32})/artifacts/(check|check_reuse|check_review)/[a-z][a-z0-9_-]{0,63}\.json", ref)
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
                checks[ref] = category if category in RESULTS else "unclassified"
        except (OSError, ValueError, TypeError, AttributeError):
            result["unavailable"] += 1
    result["executed"] = len(checks)
    for ref, category in checks.items():
        result["by_raw_result_class"][category] += 1
        result["by_result_class"][reviews.get(ref, category)] += 1
    result["coverage"] = "partial" if result["unavailable"] else "recorded_only"
    return result


def _build(root, state):
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
            "checks": _checks(root, state),
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
        *R.ACTIVITIES, *R.FINAL, *RESULTS}
WORDS = {NOTICE, "partial", "recorded_only", "unknown", "complete", "running", "execution_ended", *R.FINAL}


def _validate(value, key=None):
    if isinstance(value, dict):
        if value.keys() - KEYS:
            raise AuditError("invalid_audit")
        for name, child in value.items():
            _validate(child, name)
    elif isinstance(value, list) and key == "boundaries":
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
    _validate(document)
    required = {"schema_version", "run_id", "notice", "source_state_hash", "implementation_hash",
                "created_at", "snapshot_at", "status", "completeness", "timing", "tasks", "roles",
                "checks", "coverage", "actual_model", "tokens"}
    if (not isinstance(document, dict) or set(document) != required
            or type(document["schema_version"]) is not int or document["schema_version"] != 1
            or document["notice"] != NOTICE or document["actual_model"] is not None or document["tokens"] is not None
            or document["status"] not in R.FINAL | {"running", "unknown"}
            or document["completeness"] not in {"partial", "recorded_only"}):
        raise AuditError("invalid_audit")
    for key in ("source_state_hash", "implementation_hash"):
        if document[key] is None and key == "implementation_hash":
            continue
        if not isinstance(document[key], str) or not re.fullmatch(r"[0-9a-f]{64}", document[key]):
            raise AuditError("invalid_audit")
    for key in ("created_at", "snapshot_at"):
        if document[key] is not None and not isinstance(document[key], str):
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


def scan(project_root, limit=20):
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
        return {"status": "ok", "runs": [audit(root, identity) for _, identity in identities[:limit]],
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
