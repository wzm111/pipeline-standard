"""Source-bound context references; cache presence never means a host loaded it."""
import hashlib
import json
from pathlib import Path

import operation_metrics as O
import run_evidence as E
import run_state as R


def _owned(root, run_id, owner):
    state = R.status(root, run_id)["state"]
    if state is None:
        raise ValueError("context run not found")
    R._owned(state, run_id, owner)
    if state["status"] != "running":
        raise ValueError("context requires an active owned run")
    return state


def _pointer(key):
    return f"tmp/pipeline/context/cache/{E._label(key)}.json"


def _sources(key, sources):
    E._label(key)
    if not isinstance(sources, list) or any(not isinstance(p, str) or not p for p in sources):
        raise ValueError("explicit source path list required")
    # A base must track newly added local instructions too, not just existing ones.
    return sorted(set(sources) | ({"PIPELINE.md", "AGENTS.md", "CLAUDE.md"} if key == "base" else set()))


def _packet(root, reference, expected=None):
    if not isinstance(reference, dict) or set(reference) != {"run_id", "path", "sha256"}:
        raise ValueError("invalid context reference")
    path = E._local(root, reference["path"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
        raise ValueError("context artifact changed")
    state = R.status(root, reference["run_id"])["state"]
    if state is None or reference["path"] not in state["artifacts"]:
        raise ValueError("unregistered context")
    packet = E.read_report(root, reference["run_id"], reference["path"], kind="context")["payload"]
    if packet["sources"] != _sources(packet["key"], packet["sources"]):
        raise ValueError("incomplete context source baseline")
    current = expected[1] if expected is not None and packet["sources"] == expected[0] else E.fingerprint(root, packet["sources"])
    if (packet["project_root"] != str(root) or not isinstance(packet["content"], str)
            or current != packet["fingerprint"]):
        raise ValueError("context sources changed")
    return packet


def lookup(project_root, run_id, owner_token, key, sources):
    """Return only a compact hit or a refresh ticket, without emitting content."""
    root = Path(project_root).resolve(strict=True)
    _owned(root, run_id, owner_token)
    pointer = _pointer(key)
    sources = _sources(key, sources)
    with O.span(root, run_id, owner_token, "read", "context-lookup"):
        current = E.fingerprint(root, sources)
        try:
            reference = json.loads(E._local(root, pointer).read_text())
            packet = _packet(root, reference, expected=(sources, current))
            if packet["sources"] != sources or packet["fingerprint"] != current or packet["key"] != key:
                raise ValueError("context declaration changed")
            return {"status": "unchanged", "reference": reference,
                    "content_bytes": len(packet["content"].encode()), "content_included": False}
        except (OSError, ValueError, KeyError, TypeError):
            return {"status": "refresh_required", "key": key, "sources": sources,
                    "fingerprint": current, "content_included": False}


def save(project_root, run_id, owner_token, ticket, content):
    """Save one caller-created summary only if its pre-read inputs remain stable."""
    root = Path(project_root).resolve(strict=True)
    _owned(root, run_id, owner_token)
    if (not isinstance(ticket, dict) or ticket.get("status") != "refresh_required"
            or not isinstance(content, str) or not content.strip()):
        raise ValueError("refresh ticket and nonempty summary required")
    pointer = _pointer(ticket["key"])
    if ticket["sources"] != _sources(ticket["key"], ticket["sources"]):
        raise ValueError("invalid context source baseline")
    with O.span(root, run_id, owner_token, "write", "context-save"):
        if E.fingerprint(root, ticket["sources"]) != ticket["fingerprint"]:
            raise ValueError("context sources changed during summary creation")
        payload = {"project_root": str(root), "key": ticket["key"], "sources": ticket["sources"],
                   "fingerprint": ticket["fingerprint"], "content": content}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
        name = f"context-{digest}"
        relative = f"tmp/pipeline/runs/{run_id}/artifacts/context/{name}.json"
        path = E._local(root, relative)
        if path.exists():
            if E.read_report(root, run_id, relative, kind="context")["payload"] != payload:
                raise ValueError("context artifact collision")
        else:
            E.write_report(root, run_id, owner_token, "context", name, payload)
        reference = {"run_id": run_id, "path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        with R._write_guard(root) as base:
            _, state = R._load_owned(base, run_id, owner_token)
            if state["status"] != "running":
                raise ValueError("context requires an active run")
            _packet(root, reference)  # Recheck inputs before publishing the pointer.
            target = E._local(root, pointer)
            target.parent.mkdir(parents=True, exist_ok=True)
            R._atomic(target, reference)
        return {"status": "saved", "reference": reference, "content_bytes": len(content.encode()),
                "content_included": False}


def load(project_root, reference):
    """Explicit cold-context read; validates provenance and current source identity."""
    root = Path(project_root).resolve(strict=True)
    packet = _packet(root, reference)
    return {"reference": reference, "content": packet["content"], "sources": packet["sources"],
            "fingerprint": packet["fingerprint"]}
