"""Reuse certified browser recipes, then check host-observed page preconditions.

No browser is opened and no acceptance PASS is inferred from a recipe hit.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

import run_evidence as E
import run_state as R


def _conditions(value):
    if not isinstance(value, dict) or set(value) - {"origin", "path", "markers", "version"}:
        raise ValueError("explicit browser conditions required")
    origin = urlsplit(value.get("origin", ""))
    if (origin.scheme not in {"http", "https"} or not origin.netloc or origin.username or origin.password
            or origin.path or origin.query or origin.fragment):
        raise ValueError("origin must omit path, credentials and query")
    path = value.get("path")
    if not isinstance(path, str) or not path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("exact pathname required")
    markers = E.coverage_labels(value.get("markers"))
    if not markers or ("version" in value and (not isinstance(value["version"], str) or not value["version"].strip())):
        raise ValueError("at least one observed marker and nonempty optional version required")
    return {**value, "markers": markers}


def record_recipe(project_root, run_id, owner_token, name, *, script, fixtures, preconditions, validated_check):
    root = Path(project_root).resolve(strict=True)
    E._label(name)
    conditions = _conditions(preconditions)
    if not isinstance(fixtures, list) or any(not isinstance(p, str) for p in fixtures):
        raise ValueError("fixture paths must be a list")
    if not E._local(root, script).is_file():
        raise ValueError("recipe script must be an existing regular file")
    proof = E.read_report(root, run_id, validated_check, kind="check")["payload"]
    labels = E.coverage_labels(proof.get("coverage"))
    if "browser" not in labels or not E.reusable_check(root, run_id, validated_check, command=proof["command"],
            environment=proof["environment"], impact_covered=True, coverage=labels)["reusable"]:
        raise ValueError("recipe requires current successful browser evidence")
    paths = sorted(set([script, *fixtures]))
    for path in paths:
        if not E._local(root, path).exists() or not any(
                path == scope or (E._local(root, scope).is_dir() and Path(path).is_relative_to(Path(scope)))
                for scope in proof["scope"]):
            raise ValueError("recipe assets must belong to the validated check scope")
    assets = [{"path": path, "fingerprint": E.fingerprint(root, [path])} for path in paths]
    if not E.reusable_check(root, run_id, validated_check, command=proof["command"],
            environment=proof["environment"], impact_covered=True, coverage=labels)["reusable"]:
        raise ValueError("browser inputs changed while recording recipe")
    payload = {"script": script, "fixtures": fixtures, "assets": assets, "preconditions": conditions,
               "probe_timeout_ms": 1500, "validated_check": {"run_id": run_id, "path": validated_check,
               "sha256": hashlib.sha256(E._local(root, validated_check).read_bytes()).hexdigest()}}
    relative = E.write_report(root, run_id, owner_token, "browser", name, payload)
    with R._write_guard(root) as base:
        state_path, state = R._load_owned(base, run_id, owner_token)
        if state["status"] != "running":
            raise ValueError("recipe requires an active owned run")
        state["handoff_files"] = sorted(set(state.get("handoff_files", [])) | set(paths) | {relative})
        state["updated_at"] = R._now()
        R._atomic(state_path, state)
    return {"run_id": run_id, "path": relative, "sha256": hashlib.sha256(E._local(root, relative).read_bytes()).hexdigest()}


def preflight(project_root, run_id, reference, snapshot):
    """Read-only and immediate; caller obtains a fresh, short host probe first."""
    root = Path(project_root).resolve(strict=True)
    result = {"ready": False, "reasons": [], "probe_timeout_ms": 1500, "acceptance": "not_run"}
    try:
        state = R.status(root, run_id)["state"]
        if not state:
            raise ValueError("current run missing")
        path = reference["path"]
        if reference["run_id"] != run_id or path not in state["artifacts"]:
            inherited = next((a for a in state.get("inherited_assets", []) if a["path"] == path), None)
            if inherited is None or E.fingerprint(root, [path]) != inherited["fingerprint"]:
                raise ValueError("recipe not carried into current run")
        if hashlib.sha256(E._local(root, path).read_bytes()).hexdigest() != reference["sha256"]:
            raise ValueError("recipe report changed")
        origin_state = R.status(root, reference["run_id"])["state"]
        if origin_state is None or path not in origin_state["artifacts"]:
            raise ValueError("unregistered recipe")
        packet = E.read_report(root, reference["run_id"], path, kind="browser")["payload"]
        proof = packet["validated_check"]
        if hashlib.sha256(E._local(root, proof["path"]).read_bytes()).hexdigest() != proof["sha256"]:
            raise ValueError("original recipe evidence changed")
        original = E.read_report(root, proof["run_id"], proof["path"], kind="check")["payload"]
        if (original["exit_code"] != 0 or "browser" not in E.coverage_labels(original.get("coverage"))
                or hashlib.sha256(E._local(root, original["log_path"]).read_bytes()).hexdigest() != original["log_sha256"]):
            raise ValueError("original browser result changed")
        for asset in packet["assets"]:
            if E.fingerprint(root, [asset["path"]]) != asset["fingerprint"]:
                raise ValueError("recipe script or fixture changed")
        expected = _conditions(packet["preconditions"])
    except (OSError, ValueError, KeyError, TypeError):
        result["reasons"] = ["recipe_evidence_invalid"]
        return result
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("url"), str):
        result["reasons"] = ["page_observation_missing"]
        return result
    try:
        actual = urlsplit(snapshot["url"])
    except ValueError:
        result["reasons"] = ["page_observation_invalid"]
        return result
    if f"{actual.scheme}://{actual.netloc}" != expected["origin"] or actual.path != expected["path"]:
        result["reasons"].append("page_mismatch")
    markers = snapshot.get("markers", {})
    if not isinstance(markers, dict) or any(markers.get(name) is not True for name in expected["markers"]):
        result["reasons"].append("required_marker_missing_or_unknown")
    if "version" in expected and snapshot.get("version") != expected["version"]:
        result["reasons"].append("version_mismatch_or_unknown")
    if not result["reasons"]:
        result.update(ready=True, script=packet["script"], fixtures=deepcopy(packet["fixtures"]))
    return result
