"""Narrow versioned rule snapshots and explicit caller acknowledgements.

An acknowledgement records a caller assertion, never proof of model reading or
understanding. Legacy runs remain untracked until their owner explicitly opts in.
"""
import hashlib
import json
import re
from pathlib import Path

import run_evidence as E
import run_state as R

SKILL_ROOT = Path(__file__).resolve().parents[1]
RULES = ("SKILL.md", *(f"references/{name}.md" for name in
         ("progress", "evidence", "check-plan", "standard", "codex", "role-dispatch", "visual", "review")))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def snapshot(project_root):
    """Hash a fixed small set; resolve the installation root, reject child links."""
    project = Path(project_root).resolve(strict=True)
    skill = SKILL_ROOT.resolve(strict=True)
    sources = {}
    try:
        for prefix, root, files in (("skill", skill, RULES),
                                     ("project", project, ("PIPELINE.md", "AGENTS.md", "CLAUDE.md"))):
            for relative in files:
                path = E._local(root, relative)
                required = prefix == "skill" or relative == "PIPELINE.md"
                if not path.exists() and not required:
                    sources[f"{prefix}:{relative}"] = "missing"
                else:
                    if not path.is_file():
                        raise ValueError("missing rule source")
                    sources[f"{prefix}:{relative}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        raise ValueError("policy rule sources missing or unsafe; restore sources and refresh policy") from None
    return {"revision": _digest(sources),
            "source_revision": _digest({k: v for k, v in sources.items() if k.startswith("skill:")}),
            "sources": sources}


def initialize(project_root):
    return {"mode": "managed", **snapshot(project_root), "caller_acknowledged": False,
            "acknowledged_revision": None, "managed_since": R._now()}


def inspect(project_root, state):
    policy = state.get("run_policy") if state else None
    if not state or "run_policy" not in state:
        try:
            revision = snapshot(project_root)["revision"]
        except (OSError, ValueError):
            revision = None
        return {"mode": "legacy_untracked" if state else "unknown", "managed": False if state else None,
                "current": None, "caller_acknowledged": None, "stale": None,
                "revision": revision, "pending_ack": None}
    if not isinstance(policy, dict):
        return {"mode": "unknown", "managed": None, "current": False,
                "caller_acknowledged": False, "stale": True, "revision": None, "pending_ack": True}
    try:
        current = snapshot(project_root)
    except (OSError, ValueError):
        return {"mode": "managed", "managed": True, "current": False,
                "caller_acknowledged": False, "stale": True, "revision": None, "pending_ack": True}
    sources = policy.get("sources")
    optional = {"project:AGENTS.md", "project:CLAUDE.md"}
    valid_sources = (isinstance(sources, dict) and sources.keys() == current["sources"].keys()
                     and all(isinstance(v, str) and (bool(re.fullmatch(r"[0-9a-f]{64}", v))
                                 or (k in optional and v == "missing")) for k, v in sources.items()))
    coherent = (valid_sources and policy.get("revision") == _digest(sources)
                and policy.get("source_revision") == _digest({k: v for k, v in sources.items()
                                                                if k.startswith("skill:")}))
    if not coherent:
        return {"mode": "managed", "managed": True, "current": False,
                "caller_acknowledged": False, "stale": True, "revision": current["revision"], "pending_ack": True}
    recorded_ack = (policy.get("mode") == "managed" and policy.get("caller_acknowledged") is True
                    and policy.get("acknowledged_revision") == policy.get("revision"))
    project = {k: v for k, v in current["sources"].items() if k.startswith("project:")}
    saved_project = {k: v for k, v in sources.items() if k.startswith("project:")}
    latest = policy.get("revision") == current["revision"]
    # Shared guidance updates do not interrupt an acknowledged in-flight task.
    # Project contracts remain immediate; a new task must adopt the new rules.
    matches = policy.get("mode") == "managed" and (latest or (recorded_ack and project == saved_project))
    acknowledged = matches and recorded_ack
    return {"mode": "managed", "managed": True, "current": matches,
            "caller_acknowledged": acknowledged, "stale": not matches,
            "revision": current["revision"], "effective_revision": policy.get("revision"),
            "update_available": not latest, "adoption_boundary": "next_task",
            "pending_ack": not acknowledged}


def require(project_root, state):
    if "run_policy" not in state:
        return  # Historical adoption is unknown, not retroactively asserted.
    view = inspect(project_root, state)
    if not view["caller_acknowledged"]:
        raise ValueError("policy requires current caller acknowledgement; inspect policy then policy-ack --revision (refresh without restarting)")


def require_owned(project_root, run_id, owner):
    root, base = R._paths(project_root)
    _, state = R._load_owned(base, run_id, owner)
    require(root, state)


def require_new_task(project_root, state):
    require(project_root, state)
    if "run_policy" in state and inspect(project_root, state).get("update_available"):
        raise ValueError("policy update available: acknowledge current rules before a new task; keep valid existing checks")


def acknowledge(project_root, run_id, owner, revision):
    with R._write_guard(project_root) as base:
        path, state = R._load_owned(base, run_id, owner)
        if state["status"] != "running":
            raise ValueError("policy acknowledgement requires an active owned run")
        current = snapshot(project_root)
        if not isinstance(revision, str) or revision != current["revision"]:
            raise ValueError("policy revision does not match current sources; inspect policy and acknowledge the current revision")
        now = R._now()
        previous = state.get("run_policy")
        since = previous.get("managed_since", now) if isinstance(previous, dict) else now
        state["run_policy"] = {"mode": "managed", **current, "caller_acknowledged": True,
                               "acknowledged_revision": revision, "acknowledged_at": now,
                               "managed_since": since}
        state["updated_at"] = now
        R._atomic(path, state)
        return inspect(project_root, state)


def audit_summary(project_root, state):
    if "run_policy" not in state:
        return {"mode": "legacy_untracked", "managed": False, "current": None,
                "caller_acknowledged": None, "stale": None}
    view = inspect(project_root, state)
    return {key: view[key] for key in ("mode", "managed", "current", "caller_acknowledged", "stale")}
