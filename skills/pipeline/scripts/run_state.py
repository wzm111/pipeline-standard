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

FINAL = {"paused", "aborted", "completed"}
FIELDS = {"stage", "batches", "pending", "next", "artifacts"}


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
    if not isinstance(initial, dict) or initial.keys() - FIELDS:
        raise ValueError("unsupported initial state fields")
    with _write_guard(project_root) as base:
        if (base / ".active").exists():
            raise ValueError("active run exists; inspect ownership before recovery")
        (base / "runs").mkdir(parents=True, exist_ok=True)
        state = {"schema_version": 1, "run_id": uuid.uuid4().hex,
                 "owner_token": secrets.token_hex(32), "status": "running",
                 "stage": "start", "batches": [], "pending": [], "next": None,
                 "artifacts": [], "history": [], "updated_at": _now()}
        state.update(initial)
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
    if not isinstance(changes, dict) or changes.keys() - FIELDS:
        raise ValueError("unsupported update fields")
    with _write_guard(project_root) as base:
        path, state = _load_owned(base, run_id, owner_token)
        if state.get("status") != "running":
            raise ValueError("only a running run can be updated")
        state.update(deepcopy(changes))
        state["updated_at"] = _now()
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
        state["status"] = outcome
        state["updated_at"] = _now()
        state["history"].append({"event": outcome, "stop_evidence": stop_evidence, "at": state["updated_at"]})
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
        state["history"].append({"event": "resumed", "stop_evidence": stop_evidence, "at": state["updated_at"]})
        # Keep ownership stable so a crash between state and marker writes can
        # be retried with the same explicit ownership and fresh stop evidence.
        _atomic(path, state)
        if (base / ".active").exists():
            _owned(_read(base / ".active"), run_id, owner_token)
        else:
            _claim(base, state)
        return deepcopy(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--run-id")
    args = parser.parse_args()
    try:
        print(json.dumps(status(args.project, args.run_id), ensure_ascii=False, indent=2))
    except (OSError, ValueError) as error:
        print(f"无法读取运行状态：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
