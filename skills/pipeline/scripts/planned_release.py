"""Bind a monolithic release's original execution to a current live readback."""
import hashlib
from pathlib import Path
import re
import shlex

import check_plan as P
import delivery as D
import managed_qa as Q
import run_evidence as E
import run_state as R

ACTIVATION = "release_activation"
IDENTITY = "release_identity"


def _references(root, run_id, snapshot, plan_ref, check_id):
    source, binding = snapshot["bindings"].get((plan_ref, check_id), (None, None))
    if source is None:
        raise ValueError("both checks require original planned execution evidence")
    return {"binding": D._reference(root, run_id, source),
            "check": D._reference(root, run_id, binding["result"]["report"])}


def readback_prerequisite(root, state, snapshot, plan_ref, check_id, check):
    """Permit the dedicated readback to observe an already completed activation."""
    if "run_policy" not in state or check["coverage"] != [ACTIVATION] or "argv" not in check:
        raise ValueError("readback requires an original managed activation check")
    root = Path(root).resolve(strict=True)
    run_id = state["run_id"]
    if not Q._current(Q._read(root, state, plan_ref, "check_plan"), state):
        raise ValueError("readback requires the original plan in the current task")
    source = _references(root, run_id, snapshot, plan_ref, check_id)
    _source(root, state, snapshot, plan_ref, check_id, check, source, historical=True)


def _identity_marker(log, marker, *, target):
    if (not isinstance(marker, list) or len(marker) != 2
            or any(not isinstance(part, str) or len(part) > 160
                   or any(not ch.isprintable() for ch in part) for part in marker)
            or not marker[0].strip()):
        raise ValueError("release markers require a literal prefix and delimiter (empty for end of line)")
    prefix, delimiter = marker
    # A separator inside a URI (for example ? or #) is part of the value.
    field_boundaries = " ：，；"
    if prefix[-1] not in field_boundaries or (delimiter and delimiter[0] not in field_boundaries):
        raise ValueError("release markers must isolate a complete target or version token")
    alphabet = r"[A-Za-z0-9._:/+@-]" if target else r"[A-Za-z0-9._:+-]"
    matches = [(number, line[len(prefix):]) for number, line in enumerate(log.splitlines())
               if line.startswith(prefix)]
    if len(matches) != 1:
        raise ValueError("release marker must identify exactly one complete line")
    number, value = matches[0]
    if delimiter:
        value, found, _ = value.partition(delimiter)
        if not found:
            raise ValueError("release marker delimiter was not found")
    max_length = 240 if target else 128
    if len(value) > max_length or not re.fullmatch(r"[A-Za-z0-9]" + alphabet + "*", value):
        raise ValueError("invalid release target or version in log")
    return number, value


def _log_identity(log, source):
    version_line, version = _identity_marker(log, source["version_marker"], target=False)
    target_line, target = _identity_marker(log, source["target_marker"], target=True)
    if version_line == target_line:
        raise ValueError("release version and target require separate log lines")
    return version, target


def _source(root, state, snapshot, plan_ref, check_id, check, source, *, historical):
    binding_ref, binding = snapshot["bindings"].get((plan_ref, check_id), (None, {}))
    label = ACTIVATION if historical else IDENTITY
    if not binding_ref or source["binding"]["path"] != binding_ref:
        raise ValueError("latest original planned execution is required")
    checked_binding = D._read_reference(root, source["binding"], "planned_check")
    result = binding["result"]
    if (checked_binding != binding or binding.get("evidence_only") or binding.get("carried_from_plan")
            or result["reused"] is not False or result["exit_code"] != 0
            or result["report"] != source["check"]["path"]
            or binding["plan_sha256"] != P._cached_digest(root, plan_ref, snapshot)):
        raise ValueError("release evidence must come from an actual successful execution of this plan")
    ref = result["report"]
    run_id = state["run_id"]
    actual = Q._read(root, state, ref, "check")
    proof = D._read_reference(root, source["check"], "check")
    if (any(reference["run_id"] != run_id for reference in source.values())
            or Path(binding_ref).stem != Path(ref).stem
            or proof != actual["payload"] or not Q._current(actual, state)
            or not Q._current(Q._read(root, state, binding_ref, "planned_check"), state)
            or proof.get("inherited_from") or proof.get("observation_kind")
            or proof.get("result_class") != "success" or proof.get("inputs_stable") is not True
            or proof.get("argv") != check["argv"] or proof.get("scope") != check["scope"]
            or proof.get("intent") != check.get("intent", "test") or proof.get("cwd", ".") != check["cwd"]):
        raise ValueError("original release check identity is invalid")
    started, finished = (R._timestamp(proof[key]) for key in ("started_at", "finished_at"))
    task_start = Q.boundary(state)
    if finished < started or (task_start and started < R._timestamp(task_start)):
        raise ValueError("release check timestamps do not belong to the current task")
    # Legacy plans already have a runner-owned operation span, independently
    # of the report payload accepted by their older generic report command.
    label_name = Path(ref).stem
    recorded = R._timestamp(actual["recorded_at"])
    bound_at = R._timestamp(Q._read(root, state, binding_ref, "planned_check")["recorded_at"])
    expected_log = f"tmp/pipeline/runs/{run_id}/artifacts/logs/{label_name}.log"
    if (proof["log_path"] != expected_log or not any(
            span["category"] == "verify" and span["label"] == label_name and span["status"] == "executed"
            and R._timestamp(span["start"]) <= started <= finished <= recorded <= R._timestamp(span["end"]) <= bound_at
            for span in state.get("operation_spans", []))):
        raise ValueError("release proof has no matching completed runner operation")
    if (snapshot["observations"].get(P._identity(proof)) != ref
            or snapshot["release_observations"].get(label) != ref):
        raise ValueError("newer release evidence requires a new binding")
    env = P._environment(root, state["owner_token"], check)
    if historical:
        # Preserve the recorded process identity only for the historical fact.
        # Current executable, context, cwd, inputs and log still have to match.
        env["process_env_sha256"] = proof["environment"]["process_env_sha256"]
    verdict = E.reusable_check(root, run_id, ref, command=shlex.join(check["argv"]),
                               environment=env, impact_covered=True, coverage=check["coverage"])
    if not verdict["reusable"]:
        raise ValueError("release evidence is invalid: " + verdict["reason"])
    log = E._local(root, proof["log_path"]).read_bytes()
    if hashlib.sha256(log).hexdigest() != proof["log_sha256"]:
        raise ValueError("release log changed during validation")
    return proof, log.decode("utf-8"), started, finished


def validate(root, state, payload, snapshot):
    """Apply the historical exception only to the selected activation check."""
    if "run_policy" not in state:
        raise ValueError("planned release binding requires a managed run")
    if not isinstance(payload, dict) or set(payload) != {"plan", "activation", "readback"}:
        raise ValueError("invalid planned release binding")
    run_id = state["run_id"]
    plan_ref = payload["plan"]["path"]
    plan = P._read_plan(root, run_id, state["owner_token"], plan_ref, state, snapshot)
    if (payload["plan"]["run_id"] != run_id
            or payload["plan"]["sha256"] != P._cached_digest(root, plan_ref, snapshot)
            or not Q._current(Q._read(root, state, plan_ref, "check_plan"), state)):
        raise ValueError("release binding requires the original immutable plan in this task")
    activation, readback = payload["activation"], payload["readback"]
    for side in (activation, readback):
        if set(side) != {"id", "binding", "check", "version_marker", "target_marker"}:
            raise ValueError("invalid release evidence declaration")
    activate_check, live_check = (plan["checks"][side["id"]] for side in (activation, readback))
    if ("argv" not in activate_check or "argv" not in live_check
            or activate_check["coverage"] != [ACTIVATION]
            or IDENTITY not in live_check["coverage"] or ACTIVATION in live_check["coverage"]):
        raise ValueError("select an activation-only check and a separate live identity check")
    mutation, activation_log, _, activation_end = _source(
        root, state, snapshot, plan_ref, activation["id"], activate_check,
        {key: activation[key] for key in ("binding", "check")}, historical=True)
    live, live_log, live_start, _ = _source(
        root, state, snapshot, plan_ref, readback["id"], live_check,
        {key: readback[key] for key in ("binding", "check")}, historical=False)
    if live_start <= activation_end:
        raise ValueError("live readback must start after activation finished")
    version, target = _log_identity(activation_log, activation)
    if any(readback[key][1:] != [""] for key in ("version_marker", "target_marker")):
        raise ValueError("live markers must contain the full value through end of line")
    if ((version, target) != _log_identity(live_log, readback)
            or any(proof["environment"].get("context:release_target", target) != target
                   for proof in (mutation, live))):
        raise ValueError("release version or target does not match live readback")
    return version, target


def bind(root, run_id, owner, plan_ref, name, activation_id, readback_id, markers):
    """Register evidence only. This entry point never executes a planned command."""
    root = Path(root).resolve(strict=True)
    state = P._owned(root, run_id, owner)
    snapshot = P._snapshot(root, run_id, state)
    if not isinstance(markers, dict) or set(markers) != {"activation", "readback"}:
        raise ValueError("activation and readback markers are required")
    payload = {"plan": D._reference(root, run_id, plan_ref)}
    for side, key in (("activation", activation_id), ("readback", readback_id)):
        declaration = markers[side]
        if not isinstance(declaration, dict) or set(declaration) != {"version_marker", "target_marker"}:
            raise ValueError("each release check requires version and target markers")
        payload[side] = {**declaration, "id": key, **_references(root, run_id, snapshot, plan_ref, key)}
    return E.write_report(root, run_id, owner, "planned_release", name, payload)
