"""Local model preferences; no provider calls or account/model discovery."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile

ROLES = {"developer", "planner", "scope_guardian", "tester", "releaser"}
RUNTIMES = {"codex", "claude"}
DEFAULT_MODELS = Path(__file__).resolve().parents[1] / "defaults/models.json"
TASK_KINDS = {
    "routine_planning": {"planner"},
    "routine_review": {"scope_guardian"},
    "scoped_fix": {"developer"},
    "ui_validation": {"tester"},
    "release_summary": {"releaser"},
}


def validate_preference(config, *, role=None):
    if not isinstance(config, dict):
        raise ValueError("role must be an object")
    model = config.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("role requires a nonempty model")
    effort = config.get("effort")
    if "effort" in config and (not isinstance(effort, str) or not effort.strip() or model == "inherit"):
        raise ValueError("invalid effort")
    if "fallback" in config and config["fallback"] != "inherit":
        raise ValueError("fallback must be inherit when supplied")
    if "task_routes" in config:
        routes = config["task_routes"]
        if not isinstance(routes, dict):
            raise ValueError("task_routes must be an object")
        for task_kind, route in routes.items():
            if task_kind not in TASK_KINDS or (role is not None and role not in TASK_KINDS[task_kind]):
                raise ValueError(f"invalid task kind for role: {task_kind}")
            if not isinstance(route, dict) or route.keys() - {"model", "effort", "fallback"}:
                raise ValueError("task route accepts only model, effort and fallback")
            validate_preference(route)
    return config


def validate_document(data):
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported schema_version")
    runtimes = data.get("runtimes", {})
    if not isinstance(runtimes, dict):
        raise ValueError("runtimes must be an object")
    for runtime in RUNTIMES & runtimes.keys():
        roles = runtimes[runtime]
        if not isinstance(roles, dict):
            raise ValueError(f"{runtime} must be an object")
        for role, config in roles.items():
            if role not in ROLES:
                raise ValueError(f"unknown role {role}")
            validate_preference(config, role=role)
    return data


def _no_symlinks(*paths):
    for path in paths:
        if path.is_symlink():
            raise ValueError(f"symlink is not supported: {path}")


def read_document(path):
    """Return a validated copy and revision token, including the missing state."""
    path = Path(path).absolute()
    _no_symlinks(path)
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            raw = stream.read()
            after = os.fstat(stream.fileno())
        if (before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size):
            raise ValueError("configuration changed while reading")
    except FileNotFoundError:
        return {"schema_version": 1, "runtimes": {}}, None
    data = validate_document(json.loads(raw))
    revision = f"{after.st_dev}:{after.st_ino}:{after.st_mtime_ns}:{hashlib.sha256(raw).hexdigest()}"
    return data, revision


def read_layer(path, runtime):
    if runtime not in RUNTIMES:
        raise ValueError("unknown runtime")
    return read_document(path)[0].get("runtimes", {}).get(runtime, {})


def resolve(runtime, personal, project, temporary=None, *, defaults=None):
    if runtime not in RUNTIMES:
        raise ValueError("unknown runtime")
    result = {}
    defaults = DEFAULT_MODELS if defaults is None else Path(defaults)
    if not defaults.is_file():
        raise ValueError(f"skill default models missing: {defaults}; restore the skill installation")
    for source, path in (("skill_default", defaults), ("personal", personal), ("project", project)):
        for role, config in read_layer(path, runtime).items():
            result[role] = {"preference": deepcopy(config), "source": source, "path": str(path)}
    if temporary is not None:
        validate_document({"schema_version": 1, "runtimes": {runtime: temporary}})
        for role, config in temporary.items():
            result[role] = {"preference": deepcopy(config), "source": "temporary", "path": None}
    return {"runtime": runtime, "roles": result, "unconfigured": "runtime_default"}


def prepare_update(document, runtime, role, changes=None, *, remove=False, effective=None):
    """Produce an editable document. remove exposes lower layers; inherit masks them.

    effective is the resolved preference, required for effort-only edits when the
    role is absent locally. Explicit model changes clear old effort, fallback and routes.
    """
    validate_document(document)
    if runtime not in RUNTIMES or role not in ROLES:
        raise ValueError("unknown runtime or role")
    result = deepcopy(document)
    roles = result.setdefault("runtimes", {}).setdefault(runtime, {})
    if remove:
        if changes is not None:
            raise ValueError("remove cannot include changes")
        roles.pop(role, None)
    else:
        if not isinstance(changes, dict) or not changes:
            raise ValueError("nonempty changes required")
        config = deepcopy(roles.get(role, effective or {}))
        if "model" in changes:
            config.pop("effort", None)
            config.pop("fallback", None)
            config.pop("task_routes", None)
        config.update(deepcopy(changes))
        roles[role] = validate_preference(config, role=role)
    return validate_document(result)


def select_task_route(role, preference, *, task_kind=None, risk="unknown", complexity="unknown"):
    """Pure selection only; selected preferences still require host preflight.

    Routing replaces the model/effort/fallback tuple, without inheriting base
    effort or fallback authorization. Unknown classifications keep the base.
    """
    if not isinstance(role, str) or role not in ROLES:
        raise ValueError("unknown role")
    validate_preference(preference, role=role)
    if not isinstance(risk, str) or risk not in {"low", "medium", "high", "unknown"}:
        raise ValueError("invalid risk")
    if not isinstance(complexity, str) or complexity not in {"simple", "moderate", "complex", "unknown"}:
        raise ValueError("invalid complexity")
    if task_kind is not None and (not isinstance(task_kind, str) or task_kind not in TASK_KINDS or role not in TASK_KINDS[task_kind]):
        raise ValueError("task kind does not apply to role")
    effective = deepcopy(preference)
    routes = effective.pop("task_routes", {})
    applied = False
    if task_kind is None:
        reason = "task_kind_missing"
    elif risk != "low":
        reason = "risk_not_low"
    elif complexity != "simple":
        reason = "complexity_not_simple"
    elif task_kind not in routes:
        reason = "route_not_configured"
    else:
        effective = deepcopy(routes[task_kind])
        applied = True
        reason = "matched"
    return {"preference": effective,
            "routing": {"task_kind": task_kind, "risk": risk, "complexity": complexity,
                        "applied": applied, "reason": reason, "base_model": preference["model"]}}


def save_project(project_root, document, expected_revision):
    """Explicit project-only save after authorization/contract checks by caller.

    Reject active runs and symlinks. A sibling exclusive lock serializes callers;
    revision comparison detects intervening edits. No CLI mutation entrypoint.
    """
    validate_document(document)
    # Resolve directory aliases such as macOS /tmp and /var, then constrain all
    # writes to this project identity. Never follow the target/control symlinks.
    root = Path(project_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("project root must exist")
    path = root / "pipeline.models.json"
    active = root / "tmp/pipeline/.active"
    lock = root / ".pipeline.models.lock"
    _no_symlinks(root / "tmp", active.parent, active, path, lock)
    if active.exists() or active.is_symlink():
        raise ValueError("active pipeline run blocks configuration save")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    temp_path = None
    try:
        os.close(fd)
        _, revision = read_document(path)
        if revision != expected_revision:
            raise ValueError("configuration changed; reread before saving")
        raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with tempfile.NamedTemporaryFile(dir=root, prefix=".pipeline.models.", delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Check again immediately before atomic replace, including active-run state.
        if read_document(path)[1] != expected_revision:
            raise ValueError("configuration changed; reread before saving")
        if active.exists() or active.is_symlink():
            raise ValueError("active pipeline run blocks configuration save")
        os.replace(temp_path, path)
        temp_path = None
        saved, revision = read_document(path)
        if saved != document:
            raise ValueError("configuration readback mismatch; inspect current file")
        return revision
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        lock.unlink()


def assess_capability(preference, host):
    """Four states from current host evidence, never a provider model allowlist.

    host: agent_spawn/model_override/effort_override booleans (or unknown), models mapping
    with per-model permission boolean and efforts list; models_complete boolean.
    Model visibility alone does not establish account permission or startup.
    """
    validate_preference(preference)
    def status(state, reason):
        return {"state": state, "reason": reason, "verified": False}
    if host.get("agent_spawn") is False:
        return status("host_unsupported", "host cannot spawn agents")
    if host.get("agent_spawn") is not True:
        return status("unverifiable", "host agent startup capability unknown")
    if preference["model"] == "inherit":
        return status("available_precheck", "inherit requires no model override")
    if host.get("model_override") is False:
        return status("host_unsupported", "host cannot override models")
    if host.get("model_override") is not True:
        return status("unverifiable", "host model override capability unknown")
    models = host.get("models")
    if not isinstance(models, dict):
        return status("unverifiable", "model list unreadable")
    model = models.get(preference["model"])
    if model is None:
        return status("unavailable" if host.get("models_complete") is True else "unverifiable", "model absent from host evidence")
    if not isinstance(model, dict):
        return status("unverifiable", "model evidence malformed")
    if model.get("permission") is False:
        return status("unavailable", "account permission denied")
    if "effort" in preference:
        if host.get("effort_override") is False:
            return status("host_unsupported", "host cannot override effort")
        if host.get("effort_override") is not True or not isinstance(model.get("efforts"), list):
            return status("unverifiable", "effort support unknown")
        if preference["effort"] not in model["efforts"]:
            return status("unavailable", "effort unsupported for model")
    if model.get("permission") is not True:
        return status("unverifiable", "account permission unknown")
    return status("available_precheck", "host and permission precheck only")


def codex_spawn_parameters(preference, host):
    """Pure preflight conversion; callers actually launch and record feedback."""
    capability = assess_capability(preference, host)
    fallback = False
    if capability["state"] != "available_precheck":
        if preference.get("fallback") != "inherit" or host.get("agent_spawn") is not True:
            raise ValueError(f'{capability["state"]}: {capability["reason"]}')
        fallback = True
    parameters = {}
    if preference["model"] != "inherit" and not fallback:
        parameters["model"] = preference["model"]
        if "effort" in preference:
            parameters["reasoning_effort"] = preference["effort"]
        if host.get("fork_with_override") is not True:
            parameters["fork_turns"] = "none"
    return {"parameters": parameters, "capability": capability, "fallback": fallback,
            "fallback_reason": capability["reason"] if fallback else None}


def claude_role_parameters(preference, host):
    """Pure frontmatter patch, never a role file writer or loader.

    The caller must merge into a dedicated role: None means remove an old field.
    role_frontmatter and role_inherit must reflect the current Claude host.
    """
    capability = assess_capability(preference, host)
    if host.get("role_frontmatter") is not True:
        raise ValueError("Claude role frontmatter support unavailable or unknown")
    fallback = False
    if capability["state"] != "available_precheck":
        if preference.get("fallback") != "inherit" or host.get("agent_spawn") is not True:
            raise ValueError(f'{capability["state"]}: {capability["reason"]}')
        fallback = True
    inherit = preference["model"] == "inherit" or fallback
    if inherit and host.get("role_inherit") is not True:
        raise ValueError("Claude role inheritance support unavailable or unknown")
    # Always include model: omitting it could retain the source role's fixed model.
    parameters = {"model": "inherit" if inherit else preference["model"],
                  "effort": None if inherit else preference.get("effort")}
    return {"parameters": parameters, "application": "claude_role_frontmatter",
            "capability": capability, "fallback": fallback,
            "fallback_reason": capability["reason"] if fallback else None}


def execution_record(runtime, role, preference, plan, reported=None):
    """Record requested and returned values separately; silence means unknown."""
    reported = reported or {}
    return {"runtime": runtime, "role": role,
            "requested_model": preference["model"], "requested_effort": preference.get("effort", "runtime_default"),
            "parameters": deepcopy(plan["parameters"]), "fallback": plan["fallback"],
            "fallback_reason": plan["fallback_reason"],
            "actual_model": reported.get("actual_model") or "unknown",
            "actual_effort": reported.get("actual_effort") or "unknown",
            "startup_status": reported.get("startup_status") or "unknown",
            "error": deepcopy(reported.get("error"))}
