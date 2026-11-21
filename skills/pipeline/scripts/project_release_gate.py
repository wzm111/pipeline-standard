"""Bind project-owned activation guards to the existing release candidate."""
import hashlib
import json
import sys

from input_fingerprint import fingerprint_details
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
    if not isinstance(config, dict) or not {"activation_guard"} <= config.keys() <= {
            "activation_guard", "inputs", "readiness_probe"}:
        raise ValueError("invalid project release gate declaration")
    script = config["activation_guard"]
    probe = config.get("readiness_probe")
    extra = config.get("inputs", [])
    if not isinstance(script, str) or not script.endswith(".py") or not isinstance(extra, list):
        raise ValueError("invalid project release gate inputs")
    sources = [script, *extra]
    if "readiness_probe" in config:
        if not isinstance(probe, str) or not probe.endswith(".py"):
            raise ValueError("invalid project release readiness probe")
        sources.append(probe)
    if any(not E._local(root, source).is_file() for source in sources):
        raise ValueError("project release gate input missing")
    digest, entries = fingerprint_details(root, [DECLARATION, *sources], narrow=True)
    return {"script": script, "fingerprint": digest,
            "input_sha256": {source: entries[E._local(root, source).relative_to(root).as_posix()]["sha256"]
                             for source in extra},
            **({"readiness_probe": probe} if probe is not None else {})}


def readiness_request(root, gate, options):
    """Invoke an explicitly declared, project-owned read-only probe."""
    context = E._environment(options.get("environment") or {"scope": "local"})
    script = gate["readiness_probe"]
    return {"argv": [sys.executable, "-B", str(E._local(root, script)), json.dumps(context, sort_keys=True)],
            "scope": list(dict.fromkeys([DECLARATION, script, gate["script"], *gate["input_sha256"]])),
            "environment": context, "write_paths": [], "timeout": options.get("timeout")}


def command_identity(context):
    return hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()


def candidate_asset(root, relative):
    digest, entries = fingerprint_details(root, [relative], narrow=True)
    metadata = entries[E._local(root, relative).relative_to(root).as_posix()]
    if metadata.get("missing"):
        raise ValueError("build did not produce the declared artifacts")
    return {"path": relative, "fingerprint": digest,
            **({"sha256": metadata["sha256"]} if "sha256" in metadata else {})}


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
    if gate.get("readiness_probe"):
        import release_checks
        release_checks.validate_readiness_binding(root, state, attempt.get("readiness_binding"))
    if any(E.fingerprint(root, [asset["path"]]) != asset["fingerprint"] for asset in candidate["assets"]):
        raise ValueError("project release gate candidate changed before activation")
    payload = {"project_root": str(root), "candidate_id": candidate["id"],
               "candidate_assets": [asset["path"] for asset in candidate["assets"]],
               "candidate_asset_sha256": {asset["path"]: asset["sha256"] for asset in candidate["assets"]
                                          if "sha256" in asset},
               "project_guard_input_sha256": gate["input_sha256"],
               "environment": context["environment"]}
    return [sys.executable, str(E._local(root, gate["script"])), json.dumps(payload), "--", *context["argv"]]
