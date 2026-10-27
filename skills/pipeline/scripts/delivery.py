"""Same-project delivery receipts across completed runs; no deployment actions."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import run_evidence as E
import run_state as R

POINTER = "tmp/pipeline/delivery.json"


def _reference(root, run_id, path):
    return {"run_id": run_id, "path": path,
            "sha256": hashlib.sha256(E._local(root, path).read_bytes()).hexdigest()}


def _read_reference(root, reference, kind):
    path = E._local(root, reference["path"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
        raise ValueError("delivery reference changed")
    return E.read_report(root, reference["run_id"], reference["path"], kind=kind)["payload"]


def validated_receipt(root, reference):
    root = Path(root).resolve(strict=True)
    packet = _read_reference(root, reference, "delivery")
    source = R.status(root, reference["run_id"])["state"]
    if (not source or source["status"] != "completed" or reference["path"] not in source["artifacts"]
            or packet["project_root"] != str(root)):
        raise ValueError("delivery source must be a completed run in this project")
    for approval in packet["approvals"]:
        if approval["path"] not in source["artifacts"] or _read_reference(root, approval, approval["kind"]).get("gate") != "PASS":
            raise ValueError("delivery acceptance changed or not passed")
    if not any(a["kind"] == "qa" for a in packet["approvals"]):
        raise ValueError("delivery requires explicit QA acceptance")
    if E.fingerprint(root, packet["context_scope"]) != packet["context_fingerprint"]:
        raise ValueError("delivery contract changed")
    return packet


def seal(root, base, state):
    """Called by finish under its existing ownership lock; fail closed on gaps."""
    root = Path(root).resolve(strict=True)
    approvals, checks, seen = {}, [], set()
    for path in reversed(state["artifacts"]):
        if not isinstance(path, str) or f"/runs/{state['run_id']}/artifacts/" not in path:
            continue
        report = E.read_report(root, state["run_id"], path)
        kind, payload = report["kind"], report["payload"]
        if kind in {"qa", "acceptance"} and kind not in approvals:
            if not isinstance(payload, dict) or payload.get("gate") != "PASS":
                raise ValueError("latest acceptance is not PASS")
            approvals[kind] = {**_reference(root, state["run_id"], path), "kind": kind}
        elif kind == "check":
            key = json.dumps([payload.get("command"), payload.get("scope"), payload.get("intent")], sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            if E.reusable_check(root, state["run_id"], path, command=payload["command"],
                                environment=payload["environment"], impact_covered=True)["reusable"]:
                checks.append(_reference(root, state["run_id"], path))
    if "qa" not in approvals or not checks:
        raise ValueError("no accepted, reusable check evidence to hand off")
    context = ["PIPELINE.md"] if (root / "PIPELINE.md").is_file() else []
    # Without a project contract there is no stable scope/authorization baseline.
    if not context:
        raise ValueError("project contract required for delivery")
    # Carry the original certification forward; changed assets stay invalid
    # unless the caller explicitly lists them as newly verified handoff files.
    assets_by_path = {item["path"]: item for item in state.get("inherited_assets", [])}
    for path in state.get("handoff_files", []):
        assets_by_path[path] = {"path": path, "fingerprint": E.fingerprint(root, [path])}
    assets = list(assets_by_path.values())
    packet = {"project_root": str(root), "approvals": list(approvals.values()), "checks": checks,
              "context_scope": context, "context_fingerprint": E.fingerprint(root, context), "assets": assets}
    digest = hashlib.sha256(json.dumps(packet, sort_keys=True).encode()).hexdigest()[:16]
    relative = f"tmp/pipeline/runs/{state['run_id']}/artifacts/delivery/handoff-{digest}.json"
    path = E._local(root, relative)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        R._atomic(path, {"schema_version": 1, "run_id": state["run_id"], "kind": "delivery",
                         "recorded_at": R._now(), "payload": packet})
    elif E.read_report(root, state["run_id"], relative, kind="delivery")["payload"] != packet:
        raise ValueError("delivery receipt collision")
    if relative not in state["artifacts"]:
        state["artifacts"].append(relative)
    reference = _reference(root, state["run_id"], relative)
    R._atomic(E._local(root, POINTER), reference)
    state["delivery_status"] = {"sealed": True, "reference": reference}


def adopt(root, base, state, reference):
    """Called by start under its lock for explicit continuation, never new work."""
    root = Path(root).resolve(strict=True)
    if reference == "latest":
        reference = json.loads(E._local(root, POINTER).read_text())
        if isinstance(reference, dict) and reference.get("blocked"):
            raise ValueError("latest run has no accepted delivery; do not fall back to older PASS")
    if not isinstance(reference, dict) or reference.get("run_id") == state["run_id"]:
        raise ValueError("invalid predecessor delivery")
    packet = validated_receipt(root, reference)
    imported = []
    for check in packet["checks"]:
        payload = _read_reference(root, check, "check")
        E.validate_lineage(root, payload)
        inherited = deepcopy(payload)
        inherited["inherited_from"] = {"check": check, "receipt": reference}
        relative = f"tmp/pipeline/runs/{state['run_id']}/artifacts/check/inherited-{check['sha256'][:16]}.json"
        path = E._local(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        R._atomic(path, {"schema_version": 1, "run_id": state["run_id"], "kind": "check",
                         "recorded_at": R._now(), "payload": inherited})
        state["artifacts"].append(relative)
        imported.append(relative)
    assets = []
    for item in packet["assets"]:
        try:
            matches = E.fingerprint(root, [item["path"]]) == item["fingerprint"]
        except (OSError, ValueError):
            matches = False
        assets.append({**item, "unchanged": matches})
    state["delivery_status"] = {"adopted": True, "reference": reference,
                                "checks": imported, "assets": assets}
    state["inherited_assets"] = deepcopy(packet["assets"])


def block_latest(root, run_id, reason):
    """A newer incomplete delivery must not silently expose an older PASS."""
    path = E._local(Path(root).resolve(strict=True), POINTER)
    R._atomic(path, {"run_id": run_id, "blocked": True, "reason": reason})
