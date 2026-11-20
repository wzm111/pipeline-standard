"""Bind project-owned activation guards to the existing release candidate."""
import hashlib
import json
import sys

import run_evidence as E


DECLARATION = "pipeline.release.json"


def snapshot(root):
    declaration = E._local(root, DECLARATION)
    if not declaration.exists():
        return None
    try:
        config = json.loads(declaration.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("invalid project release gate declaration") from exc
    if not isinstance(config, dict) or not {"activation_guard"} <= config.keys() <= {"activation_guard", "inputs"}:
        raise ValueError("invalid project release gate declaration")
    script = config["activation_guard"]
    extra = config.get("inputs", [])
    if not isinstance(script, str) or not script.endswith(".py") or not isinstance(extra, list):
        raise ValueError("invalid project release gate inputs")
    sources = [script, *extra]
    if any(not E._local(root, source).is_file() for source in sources):
        raise ValueError("project release gate input missing")
    return {"script": script, "fingerprint": E.fingerprint(root, [DECLARATION, *sources]),
            "input_sha256": {source: candidate_file_hash(root, source)["sha256"] for source in extra}}


def command_identity(context):
    return hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()


def candidate_file_hash(root, relative):
    path = E._local(root, relative)
    if not path.is_file():
        return {}
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"sha256": digest.hexdigest()}


def activation_command(root, state, name, context):
    plan = state.get("release_plan", {})
    gate = snapshot(root)
    if plan.get("project_activation_gate") != gate:
        raise ValueError("project release gate changed or was not prepared")
    if gate is None:
        return None
    candidate = plan.get("candidate")
    attempt = next((item for item in plan.get("attempts", [])
                    if item["name"] == name and item["phase"] == "activate" and item["status"] == "running"), None)
    if (context is None or not candidate or not attempt
            or attempt["candidate_id"] != candidate["id"]
            or attempt.get("guarded_command") != command_identity(context)):
        raise ValueError("project release gate requires release_checks activate with a validated candidate")
    if any(E.fingerprint(root, [asset["path"]]) != asset["fingerprint"] for asset in candidate["assets"]):
        raise ValueError("project release gate candidate changed before activation")
    payload = {"project_root": str(root), "candidate_id": candidate["id"],
               "candidate_assets": [asset["path"] for asset in candidate["assets"]],
               "candidate_asset_sha256": {asset["path"]: asset["sha256"] for asset in candidate["assets"]
                                          if "sha256" in asset},
               "project_guard_input_sha256": gate["input_sha256"],
               "environment": context["environment"]}
    return [sys.executable, str(E._local(root, gate["script"])), json.dumps(payload), "--", *context["argv"]]
