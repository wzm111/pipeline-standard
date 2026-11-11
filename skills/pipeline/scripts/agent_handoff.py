"""Save and inspect a short, project-local checkpoint for another agent."""
import hashlib
import json
from pathlib import Path
import subprocess

import run_continuity
import run_evidence as E
import run_state as R

SUMMARY_TARGET_BYTES = 8 * 1024
MAX_INPUT_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 4 * MAX_INPUT_BYTES
REPORT_KINDS = {"check", "check_plan", "planned_check", "check_reuse", "check_review",
                "qa", "acceptance", "verification_review",
                "solution_review", "release_blocker", "release_recovery"}


def _git(root, *args):
    return subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args],
                          check=True, capture_output=True, timeout=10).stdout


def _git_state(root):
    scope = ("--", ".", ":(exclude).pipeline.models.lock")
    try:
        head = _git(root, "rev-parse", "HEAD").decode().strip()
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").decode().strip()
        prefix = _git(root, "rev-parse", "--show-prefix").removesuffix(b"\n")
        status = _git(root, "status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all", *scope)
        staged = _git(root, "diff", "--cached", "--raw", "-z", "--no-abbrev", "--no-renames",
                      "--no-ext-diff", "--no-textconv", *scope)
        unstaged = _git(root, "diff", "--name-only", "-z", "--relative", "--no-renames",
                        "--no-ext-diff", "--no-textconv", *scope)
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None
    digest = hashlib.sha256()
    for value in (status, staged, unstaged):
        digest.update(hashlib.sha256(value).digest())
    untracked = [entry[3 + len(prefix):] for entry in status.split(b"\0")
                 if entry.startswith(b"?? " + prefix)]
    changed = list(dict.fromkeys(untracked + [name for name in unstaged.split(b"\0") if name]))
    remaining = 8 * 1024 * 1024
    complete = len(changed) <= 64
    for entry in changed[:64]:
        try:
            path = E._local(root, entry.decode())
            if not path.exists():
                digest.update(b"missing")
                continue
            if not path.is_file() or path.stat().st_size > remaining:
                complete = False
                continue
            with path.open("rb") as stream:
                content = stream.read(remaining + 1)
            if len(content) > remaining:
                complete = False
                break
            remaining -= len(content)
            digest.update(hashlib.sha256(content).digest())
        except (OSError, ValueError, UnicodeError):
            complete = False
    return {"head": head, "branch": branch, "dirty": bool(status),
            "fingerprint": digest.hexdigest(), "content_complete": complete}


def _path(root, run_id):
    return E._local(root, f"tmp/pipeline/runs/{run_id}/handoff.json")


def _notes(value, owner):
    if not isinstance(value, dict) or set(value) - {"goal", "done", "decisions", "next", "blockers", "evidence", "details"}:
        raise ValueError("invalid handoff fields")
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    if len(encoded) > MAX_INPUT_BYTES:
        raise ValueError("handoff input exceeds the 1 MiB safety limit")
    if owner.encode() in encoded:
        raise ValueError("handoff must omit ownership credentials")
    for key in ("goal", "next"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError("handoff requires a goal and a concrete next step")
    for key in ("done", "decisions", "blockers", "evidence"):
        entries = value.get(key, [])
        if (not isinstance(entries, list) or (key == "evidence" and len(entries) > 8)
                or any(not isinstance(item, str) or not item.strip() for item in entries)):
            raise ValueError("handoff lists require nonempty strings and at most eight evidence references")
    details = value.get("details", {})
    if not isinstance(details, dict):
        raise ValueError("handoff details must be an object of named text sections")
    for name, content in details.items():
        E._label(name)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("handoff detail must be nonempty text")
    return value


def _summary(notes):
    return {key: value for key, value in notes.items() if key != "details"}


def _summary_size(notes):
    size = len(json.dumps(_summary(notes), ensure_ascii=False).encode())
    return {"bytes": size, "target_bytes": SUMMARY_TARGET_BYTES, "over_target": size > SUMMARY_TARGET_BYTES}


def _evidence(root, state, refs):
    result = {}
    for ref in refs:
        if ref not in state["artifacts"]:
            raise ValueError("handoff evidence must be registered in this run")
        report = E.read_report(root, state["run_id"], ref)
        if report["kind"] not in REPORT_KINDS:
            raise ValueError("unsupported handoff evidence kind")
        result[ref] = hashlib.sha256(E._local(root, ref).read_bytes()).hexdigest()
    return result


def _task(state):
    tasks = state.get("tasks", [])
    if not tasks:
        return None
    return {key: tasks[-1][key] for key in ("task_id", "started_at", "status")}


def save(root, run_id, owner, notes):
    root = Path(root).resolve(strict=True)
    notes = _notes(notes, owner)
    with R._write_guard(root) as base:
        _, state = R._load_owned(base, run_id, owner)
        if state["status"] != "running":
            raise ValueError("save the checkpoint before stopping the active run")
        evidence = _evidence(root, state, notes.get("evidence", []))
        git = _git_state(root)
        path = _path(root, run_id)
        if git is not None:
            try:
                _git(root, "check-ignore", "--quiet", "--", path.relative_to(root).as_posix())
            except (OSError, subprocess.SubprocessError):
                raise ValueError("cannot verify handoff path is ignored by Git") from None
        record = {"schema_version": 1, "run_id": run_id, "saved_at": R._now(),
                  "run_updated_at": state["updated_at"], "task": _task(state),
                  "policy_revision": state.get("run_policy", {}).get("revision"),
                  "git": git, "notes": notes,
                  "evidence": evidence}
        R._atomic(path, record)
    return {"run_id": run_id, "handoff": path.relative_to(root).as_posix(), "summary_size": _summary_size(notes)}


def read(root, run_id, *, detail=None):
    root = Path(root).resolve(strict=True)
    before = R.status(root, run_id)
    state = before["state"]
    if state is None:
        raise ValueError("handoff requires its original run")
    record = None
    try:
        with _path(root, run_id).open("rb") as stream:
            raw = stream.read(MAX_RECORD_BYTES + 1)
    except FileNotFoundError:
        if detail is not None:
            raise ValueError("handoff detail not found") from None
    else:
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError("oversized handoff record")
        record = json.loads(raw)
        if not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("run_id") != run_id:
            raise ValueError("invalid handoff identity")
    notes = _notes(record["notes"], state["owner_token"]) if record is not None else {}
    details = notes.get("details", {})
    if detail is not None:
        if detail not in details:
            raise ValueError("handoff detail not found")
        return {"run_id": run_id, "detail": detail, "text": details[detail],
                "task_changed": _task(state) != record.get("task")}
    git = _git_state(root)
    saved_git = record.get("git") if record is not None else None
    git_status = ("unknown" if git is None or not isinstance(saved_git, dict)
                  else "changed" if git != saved_git
                  else "matched" if git["content_complete"] else "partial")
    evidence = {}
    for ref in notes.get("evidence", []):
        try:
            current = _evidence(root, state, [ref])[ref]
            evidence[ref] = "matched" if current == record["evidence"].get(ref) else "changed"
        except (OSError, ValueError, KeyError, TypeError):
            evidence[ref] = "unavailable"
    recovery = run_continuity.recovery(root, run_id, _snapshot=before)
    import run_policy

    release = recovery["release"]
    live = {"roles": recovery["live_role_count"], "checks": recovery["running_check_count"],
            "observations": sum(t.get("status") == "running" for t in state.get("observed_checks", {}).values()),
            "release_attempts": sum(a.get("status") == "running"
                                    for a in state.get("release_plan", {}).get("attempts", [])),
            "release_holds": release["unresolved_hold_count"] if release else 0}
    return {"run_id": run_id, "source": "saved_notes" if record is not None else "run_state",
            "notes": _summary(notes) if record is not None else None,
            "summary_size": _summary_size(notes) if record is not None else None,
            "registered_evidence": {key: recovery[key] for key in ("checks", "reports", "plans")},
            "detail_sections": sorted(details), "git": git, "git_status": git_status, "evidence": evidence,
            "run_changed": state["updated_at"] != record["run_updated_at"] if record is not None else None, "progress": recovery["progress"],
            "task": _task(state), "task_changed": _task(state) != record.get("task") if record is not None else None,
            "stored_policy_revision_changed": (
                state.get("run_policy", {}).get("revision") != record.get("policy_revision")
                if record is not None else None
            ),
            "policy": run_policy.audit_summary(root, state),
            "selected_run_active": recovery["selected_run_active"],
            "other_or_unknown_active": recovery["active"] and not recovery["selected_run_active"],
            "registered_in_flight": live, "registered_roles": recovery["roles"],
            "running_checks": recovery["running_checks"], "release": release,
            "notice": "Read project rules and confirm host state before continuing. Matched evidence refers to report bytes only."}
