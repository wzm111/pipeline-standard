"""Local run ownership and durable state. No executor/process management."""
import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import uuid

# Keep file-based imports usable when finish/start load the optional delivery adapter.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

FINAL = {"paused", "aborted", "completed"}
ACTIVITIES = {"execution", "testing", "human_wait", "role_wait", "handoff"}
FIELDS = {"stage", "activity", "batches", "pending", "next", "artifacts", "handoff_files"}


def _validate_changes(changes):
    if not isinstance(changes, dict) or changes.keys() - FIELDS:
        raise ValueError("unsupported state fields")
    if "stage" in changes and (not isinstance(changes["stage"], str) or not changes["stage"].strip()):
        raise ValueError("stage must be a nonempty string")
    if "activity" in changes and (not isinstance(changes["activity"], str) or changes["activity"] not in ACTIVITIES):
        raise ValueError("unsupported activity")


def _event(state, name, at, **extra):
    return {"event": name, "at": at, "stage": state.get("stage"),
            "activity": state.get("activity", "execution"), **extra}


def _paths(project_root):
    root = Path(project_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("project root must be a directory")
    base = root / "tmp/pipeline"
    paths = (root / "tmp", base, base / "runs", base / ".active", root / ".pipeline.models.lock")
    for path in paths:
        if path.is_symlink():
            raise ValueError(f"symlink is not supported: {path}")
    return root, base


def _state_path(base, run_id):
    if not isinstance(run_id, str) or uuid.UUID(run_id).hex != run_id:
        raise ValueError("run_id must be canonical UUID hex")
    path = base / "runs" / f"{run_id}.json"
    if path.is_symlink():
        raise ValueError("state symlink is not supported")
    return path


def _read(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("invalid run document or schema version")
    if not isinstance(data.get("run_id"), str) or not isinstance(data.get("owner_token"), str) or not data["owner_token"]:
        raise ValueError("run ownership missing")
    return data


def _owned(data, run_id, owner_token):
    if (data.get("run_id") != run_id or not isinstance(owner_token, str)
            or not secrets.compare_digest(data["owner_token"], owner_token)):
        raise ValueError("run ownership mismatch")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _atomic(path, data):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".run-state.", delete=False, mode="w", encoding="utf-8") as stream:
            temp_path = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            raise ValueError("control file became a symlink")
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


@contextmanager
def _write_guard(project_root):
    root, base = _paths(project_root)
    lock = root / ".pipeline.models.lock"
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.close(fd)
        _paths(root)
        yield base
    finally:
        lock.unlink()


def _claim(base, state):
    """Exclusive .active creation; even legacy/malformed markers block it."""
    active = base / ".active"
    with active.open("x", encoding="utf-8") as stream:
        json.dump({key: state[key] for key in ("schema_version", "run_id", "owner_token")}, stream)
        stream.flush()
        os.fsync(stream.fileno())


def status(project_root, run_id=None):
    """Read-only snapshot; never creates paths, adopts ownership or cleans locks."""
    _, base = _paths(project_root)
    active = None
    marker = base / ".active"
    if marker.exists():
        try:
            active = _read(marker)
        except (OSError, ValueError) as error:
            return {"active": "unrecognized", "error": str(error), "state": None}
    if run_id is None and active:
        run_id = active["run_id"]
    state = _read(_state_path(base, run_id)) if run_id is not None else None
    return {"active": active, "state": state}


def start(project_root, initial=None):
    initial = deepcopy({} if initial is None else initial)
    delivery_from = initial.pop("delivery_from", None) if isinstance(initial, dict) else None
    _validate_changes(initial)
    with _write_guard(project_root) as base:
        if (base / ".active").exists():
            raise ValueError("active run exists; inspect ownership before recovery")
        (base / "runs").mkdir(parents=True, exist_ok=True)
        now = _now()
        state = {"schema_version": 1, "run_id": uuid.uuid4().hex,
                 "owner_token": secrets.token_hex(32), "status": "running",
                 "stage": "start", "activity": "execution", "batches": [], "pending": [], "next": None,
                 "artifacts": [], "created_at": now, "history": [], "updated_at": now}
        state.update(initial)
        state["history"].append(_event(state, "start", now))
        if delivery_from is not None:
            import delivery
            before_adoption = deepcopy(state)
            try:
                delivery.adopt(project_root, base, state, delivery_from)
            except (OSError, ValueError, KeyError, TypeError) as error:
                state = before_adoption
                state["delivery_status"] = {"adopted": False, "reason": getattr(error, "reason", type(error).__name__), "detail": str(error)[:160]}
        path = _state_path(base, state["run_id"])
        if path.exists():
            raise ValueError("run_id collision")
        _atomic(path, state)
        _claim(base, state)
        return deepcopy(state)


def _load_owned(base, run_id, owner_token, *, require_active=True):
    path = _state_path(base, run_id)
    state = _read(path)
    _owned(state, run_id, owner_token)
    if state.get("status") not in FINAL | {"running"} or not isinstance(state.get("history"), list):
        raise ValueError("invalid run state")
    marker = base / ".active"
    if marker.exists():
        _owned(_read(marker), run_id, owner_token)
    elif require_active:
        raise ValueError("run is not active")
    return path, state


def update(project_root, run_id, owner_token, changes):
    _validate_changes(changes)
    with _write_guard(project_root) as base:
        path, state = _load_owned(base, run_id, owner_token)
        if state.get("status") != "running":
            raise ValueError("only a running run can be updated")
        previous = (state.get("stage"), state.get("activity", "execution"))
        state.update(deepcopy(changes))
        state["updated_at"] = _now()
        if previous != (state.get("stage"), state.get("activity", "execution")):
            state["history"].append(_event(state, "transition", state["updated_at"]))
        _atomic(path, state)
        return deepcopy(state)


def _stopped(executors_stopped, stop_evidence):
    if executors_stopped is not True or not isinstance(stop_evidence, str) or not stop_evidence.strip():
        raise ValueError("caller must confirm all executors stopped and supply evidence")


def finish(project_root, run_id, owner_token, outcome, *, executors_stopped=False, stop_evidence=None):
    if outcome not in FINAL:
        raise ValueError("outcome must be paused, aborted or completed")
    _stopped(executors_stopped, stop_evidence)
    with _write_guard(project_root) as base:
        path, state = _load_owned(base, run_id, owner_token)
        if any(item.get("status") in {"dispatching", "working"} for item in state.get("role_dispatches", [])):
            raise ValueError("close observed role dispatches after host stop before finishing")
        if outcome == "completed":
            if state.get("continuity_wait", {}).get("status") == "waiting":
                raise ValueError("resolve the registered human wait before completing")
            if state.get("release_plan"):
                import release_checks
                release_checks.validate_closeout(project_root, state)
            import delivery
            try:
                delivery.seal(project_root, base, state)
            except (OSError, ValueError, KeyError, TypeError) as error:
                delivery.block_latest(project_root, run_id, "latest_delivery_unaccepted")
                state["delivery_status"] = {"sealed": False, "reason": getattr(error, "reason", type(error).__name__), "detail": str(error)[:160]}
        else:
            import delivery
            delivery.block_latest(project_root, run_id, outcome)
        state["status"] = outcome
        if outcome == "completed":
            state.update(stage="done", activity="execution")
        state["updated_at"] = _now()
        state["history"].append(_event(state, outcome, state["updated_at"], stop_evidence=stop_evidence))
        _atomic(path, state)
        # Recheck ownership before removing only this run's marker.
        _owned(_read(base / ".active"), run_id, owner_token)
        (base / ".active").unlink()
        return deepcopy(state)


def resume(project_root, run_id, owner_token, *, executors_stopped=False, stop_evidence=None):
    """Explicit ownership recovery with caller evidence; never infer from age."""
    _stopped(executors_stopped, stop_evidence)
    with _write_guard(project_root) as base:
        path, state = _load_owned(base, run_id, owner_token, require_active=False)
        if state.get("status") not in {"paused", "aborted", "running"}:
            raise ValueError("completed or unknown state cannot resume")
        state["status"] = "running"
        state["updated_at"] = _now()
        # Resumption defaults to execution; an earlier human/role wait does not
        # silently continue after the caller has explicitly resumed work.
        state["activity"] = "execution"
        # Resume requires fresh evidence that all previous executors stopped.
        state.pop("running_checks", None)
        state.pop("check_previous_progress", None)
        state["history"].append(_event(state, "resumed", state["updated_at"], stop_evidence=stop_evidence))
        # Keep ownership stable so a crash between state and marker writes can
        # be retried with the same explicit ownership and fresh stop evidence.
        _atomic(path, state)
        if (base / ".active").exists():
            _owned(_read(base / ".active"), run_id, owner_token)
        else:
            _claim(base, state)
        return deepcopy(state)


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("missing timeline timestamp")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timeline timestamp must include timezone")
    return result


def timing(project_root, run_id=None):
    """Read-only wall-time partition, never a sum of parallel role durations.

    Old updated_at values cannot establish missing starts or transitions. Only
    observed event boundaries contribute; unowned running tails are excluded.
    """
    snapshot = status(project_root, run_id)
    state, active = snapshot["state"], snapshot["active"]
    measured_at = _now()
    result = {"run_id": state.get("run_id") if state else run_id,
              "status": state.get("status") if state else None, "coverage": "unknown",
              "reason": "no_state", "measured_at": measured_at,
              "activity_seconds": {name: 0.0 for name in sorted(ACTIVITIES)}, "stage_seconds": {},
              "tracked_seconds": 0.0, "paused_seconds": 0.0, "intervals": []}
    if state is None:
        if active == "unrecognized":
            result["reason"] = "active_owner_unrecognized"
        return result
    if run_id is not None and state["run_id"] != run_id:
        raise ValueError("run identity mismatch")
    if isinstance(active, dict) and active["run_id"] == state["run_id"]:
        if not secrets.compare_digest(active["owner_token"], state["owner_token"]):
            result["reason"] = "active_owner_mismatch"
            return result
    owns_active = isinstance(active, dict) and active["run_id"] == state["run_id"]
    history = state.get("history", [])
    if not isinstance(history, list):
        result["reason"] = "invalid_timeline"
        return result
    cursor = None
    previous_at = None
    complete = bool(history and isinstance(history[0], dict) and
                    state.get("created_at") == history[0].get("at") and history[0].get("event") == "start")
    intervals = []
    try:
        now = _timestamp(measured_at)
        for event in history:
            if not isinstance(event, dict) or event.get("event") not in {"start", "transition", "resumed", *FINAL}:
                raise ValueError("invalid timeline event")
            at = _timestamp(event.get("at"))
            if (previous_at is not None and at < previous_at) or at > now:
                raise ValueError("nonmonotonic timeline")
            kind = event["event"]
            if cursor is not None:
                if kind == "resumed" and cursor["activity"] != "paused":
                    # A recovered running run has no reliable interruption
                    # boundary; do not label that gap as execution.
                    complete = False
                else:
                    intervals.append((cursor, at))
            if kind in {"paused", "aborted"}:
                cursor = {"at": at, "activity": "paused", "stage": None}
            elif kind == "completed":
                cursor = None
            elif (isinstance(event.get("stage"), str) and event["stage"].strip()
                  and event.get("activity") in ACTIVITIES):
                cursor = {"at": at, "activity": event["activity"], "stage": event["stage"]}
            else:
                cursor = None
                complete = False
            previous_at = at
        if cursor is not None:
            if state.get("status") == "paused" and cursor["activity"] == "paused":
                intervals.append((cursor, now))
            elif state.get("status") == "running" and owns_active and cursor["activity"] != "paused":
                intervals.append((cursor, now))
            elif state.get("status") == "aborted" and history[-1]["event"] == "aborted":
                # Abort is a measured terminal boundary. Its inactive gap is
                # counted only if a later explicit resume closes that gap.
                pass
            else:
                complete = False
    except (ValueError, TypeError):
        result["reason"] = "invalid_timeline"
        return result
    if state.get("status") == "running" and not owns_active:
        complete = False
        reason = "active_owner_unconfirmed"
    else:
        reason = "observed_timeline" if complete else "history_incomplete"
    for interval, end in intervals:
        seconds = (end - interval["at"]).total_seconds()
        if interval["activity"] == "paused":
            result["paused_seconds"] += seconds
            continue
        result["activity_seconds"][interval["activity"]] += seconds
        stage = interval["stage"]
        result["stage_seconds"][stage] = result["stage_seconds"].get(stage, 0.0) + seconds
        result["tracked_seconds"] += seconds
        result["intervals"].append({"start": interval["at"].isoformat(), "end": end.isoformat(),
                                    "stage": stage, "activity": interval["activity"], "seconds": seconds})
    result["coverage"] = "complete" if complete else ("partial" if intervals else "unknown")
    result["reason"] = reason
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--run-id")
    parser.add_argument("--timing", action="store_true", help="read timing summary without ownership tokens")
    args = parser.parse_args()
    try:
        print(json.dumps((timing if args.timing else status)(args.project, args.run_id), ensure_ascii=False, indent=2))
    except (OSError, ValueError) as error:
        print(f"无法读取运行状态：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
