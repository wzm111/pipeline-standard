"""Immutable, owned check plans: one declaration drives execution and QA."""
import hashlib
import json
import os
from pathlib import Path
import shlex

import run_checks as C
import run_evidence as E
import run_state as R


def _owned(root, run_id, owner):
    with R._write_guard(root) as base:
        _, state = R._load_owned(base, run_id, owner)
        if state["status"] != "running":
            raise ValueError("plan requires an active owned run")
        return state


def _digest(root, ref):
    return hashlib.sha256(E._local(Path(root).resolve(strict=True), ref).read_bytes()).hexdigest()


def _validate(root, checks, required, owner, inherited):
    inherited = E.coverage_labels(inherited)
    if not isinstance(checks, dict) or not checks:
        raise ValueError("nonempty checks required")
    allowed = {"argv", "scope", "coverage", "cwd", "timeout", "write_paths", "environment", "intent"}
    normalized = {}
    for key, value in checks.items():
        E._label(key)
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError("unknown check fields")
        argv = value.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a or "\0" in a for a in argv):
            raise ValueError("nonempty argv required")
        labels = E.coverage_labels(value.get("coverage"))
        if not labels:
            raise ValueError("nonempty coverage required")
        E.fingerprint(root, value.get("scope"))
        E.write_paths_conflict(root, value.get("write_paths"), [])
        project = Path(root).resolve(strict=True)
        cwd = C._workdir(project, value.get("cwd")).relative_to(project).as_posix()
        C.classify_result(0, value.get("intent", "test"))
        timeout = value.get("timeout")
        if timeout is not None and (type(timeout) not in (int, float) or not 0 < timeout < float("inf")):
            raise ValueError("invalid timeout")
        if "environment" in value:
            E._environment(value["environment"])
        normalized[key] = {**value, "coverage": labels, "cwd": cwd}
    encoded = json.dumps(normalized, allow_nan=False)
    # Plans are reports, never a credential transport. Environment is identity
    # context only; actual child environment remains controlled by run_checks.
    if owner in encoded:
        raise ValueError("owner credential must not appear in plans")
    E._no_owner_token(normalized)
    coverage = sorted({label for value in normalized.values() for label in value["coverage"]})
    required = coverage if required is None else E.coverage_labels(required)
    if not set(required) <= set(coverage) | set(inherited):
        raise ValueError("required coverage has no planned or inherited check")
    required = sorted(set(required) | set(coverage) | set(inherited))
    return {"checks": normalized, "required_checks": required}


def save_plan(root, run_id, owner, name, checks, required_checks=None):
    state = _owned(root, run_id, owner)
    payload = _validate(root, checks, required_checks, owner, state.get("inherited_required_checks", []))
    ref = E.write_report(root, run_id, owner, "check_plan", name, payload)
    with R._write_guard(root) as base:
        path, state = R._load_owned(base, run_id, owner)
        state.setdefault("check_plans", {})[ref] = _digest(root, ref)
        state["planned_required_checks"] = sorted(set(E.coverage_labels(state.get("planned_required_checks")))
                                                   | set(payload["required_checks"]))
        R._atomic(path, state)
    return ref


def read_plan(root, run_id, owner, plan_ref):
    state = _owned(root, run_id, owner)
    if (plan_ref not in state.get("artifacts", []) or plan_ref not in state.get("check_plans", {})
            or _digest(root, plan_ref) != state["check_plans"][plan_ref]):
        raise ValueError("unregistered or changed plan")
    payload = E.read_report(root, run_id, plan_ref, kind="check_plan")["payload"]
    checked = _validate(root, payload["checks"], payload["required_checks"], owner,
                        state.get("inherited_required_checks", []))
    if checked != payload:
        raise ValueError("invalid plan")
    return payload


def _bindings(root, run_id, state, plan_ref, check_id):
    for ref in reversed(state["artifacts"]):
        if "/artifacts/planned_check/" in ref:
            payload = E.read_report(root, run_id, ref, kind="planned_check")["payload"]
            if payload.get("plan_ref") == plan_ref and payload.get("check_id") == check_id:
                yield payload


def run_planned(root, run_id, owner, plan_ref, check_id, name, impact_covered=False):
    plan = read_plan(root, run_id, owner, plan_ref)
    check = plan["checks"][check_id]
    previous = next(_bindings(root, run_id, _owned(root, run_id, owner), plan_ref, check_id), None)
    args = dict(check)
    reusable = previous is not None and _valid(root, run_id, owner, _owned(root, run_id, owner),
                                               plan_ref, check_id, check)[0] is not None
    # Restrict automatic reuse to this exact plan/check; no relabeling older work.
    result = C.execute_check(root, run_id, owner, name, **args,
                             reuse_from=previous["result"]["report"] if previous else None,
                             impact_covered=impact_covered is True and reusable)
    binding = E.write_report(root, run_id, owner, "planned_check", name,
                             {"plan_ref": plan_ref, "plan_sha256": _digest(root, plan_ref),
                              "check_id": check_id, "result": result})
    return {**result, "plan_ref": plan_ref, "check_id": check_id, "planned_report": binding}


def _valid(root, run_id, owner, state, plan_ref, check_id, check):
    latest = next(_bindings(root, run_id, state, plan_ref, check_id), None)
    if latest is None:
        return None, "not_executed"
    if latest.get("plan_sha256") != _digest(root, plan_ref):
        return None, "plan_changed"
    ref = latest["result"]["report"]
    if ref not in state["artifacts"]:
        return None, "unregistered_evidence"
    proof = E.read_report(root, run_id, ref, kind="check")["payload"]
    command = shlex.join(check["argv"])
    env = _environment(root, owner, check)
    if (proof.get("argv") != check["argv"] or proof.get("scope") != check["scope"]
            or proof.get("intent") != check.get("intent", "test")
            or proof.get("cwd", ".") != check.get("cwd", ".")):
        return None, "check_identity_changed"
    # A later observation of this command supersedes an older bound PASS,
    # including unplanned failure observations with different coverage labels.
    for candidate in reversed(state["artifacts"]):
        if candidate == ref:
            break
        if "/artifacts/check/" in candidate:
            newer = E.read_report(root, run_id, candidate, kind="check")["payload"]
            if (newer.get("command"), newer.get("scope"), newer.get("intent", "test"), newer.get("cwd", ".")) == (
                    command, check["scope"], check.get("intent", "test"), check.get("cwd", ".")):
                verdict = E.reusable_check(root, run_id, candidate, command=command, environment=env,
                                           impact_covered=True, coverage=newer.get("coverage"))
                if not verdict["reusable"]:
                    return None, "superseded_observation"
                break
    verdict = E.reusable_check(root, run_id, ref, command=command, environment=env,
                               impact_covered=True, coverage=check["coverage"])
    return (ref, None) if verdict["reusable"] else (None, verdict["reason"])


def _environment(root, owner, check):
    child_env = {k: v for k, v in os.environ.items() if k != "PIPELINE_OWNER_TOKEN" and owner not in v}
    cwd = E._local(Path(root).resolve(strict=True), check.get("cwd", "."))
    return C._environment(cwd, check["argv"], child_env, check.get("environment"),
                          project_root=Path(root).resolve(strict=True))


def _inherited(root, run_id, owner, state):
    covered, refs, seen = set(), [], set()
    for ref in reversed(state["artifacts"]):
        if "/artifacts/check/" not in ref:
            continue
        try:
            proof = E.read_report(root, run_id, ref, kind="check")["payload"]
            key = json.dumps([proof.get("command"), proof.get("scope"), proof.get("intent", "test"),
                              proof.get("cwd", ".")], sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            if not proof.get("inherited_from"):
                continue
            environment = proof["environment"]
            if "argv" in proof:
                check = {"argv": proof["argv"], "cwd": proof.get("cwd", "."),
                         "environment": {k[8:]: v for k, v in environment.items() if k.startswith("context:")}}
                environment = _environment(root, owner, check)
            # Public manual/tool observations have caller-confirmed environment
            # identities, not argv. Preserve their existing validation contract;
            # do not infer a command or claim a fresh OS environment observation.
            verdict = E.reusable_check(root, run_id, ref, command=proof["command"],
                                       environment=environment, impact_covered=True,
                                       coverage=proof.get("coverage"))
            if verdict["reusable"]:
                covered.update(E.coverage_labels(proof.get("coverage")))
                refs.append(ref)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return covered, refs


def qa_from_plan(root, run_id, owner, plan_ref, name, summary=""):
    plan = read_plan(root, run_id, owner, plan_ref)
    state = _owned(root, run_id, owner)
    checks, failures = [], {}
    for check_id, check in plan["checks"].items():
        try:
            ref, reason = _valid(root, run_id, owner, state, plan_ref, check_id, check)
        except (OSError, ValueError, KeyError, TypeError):
            ref, reason = None, "unconfirmed_evidence"
        if ref:
            checks.append(ref)
        else:
            failures[check_id] = reason
    covered = {label for key, check in plan["checks"].items() if key not in failures for label in check["coverage"]}
    inherited, inherited_refs = _inherited(root, run_id, owner, state)
    covered.update(inherited & set(E.coverage_labels(state.get("inherited_required_checks"))))
    checks.extend(ref for ref in inherited_refs if ref not in checks)
    required = sorted(set(plan["required_checks"])
                      | set(E.coverage_labels(state.get("planned_required_checks"))))
    missing = sorted(set(required) - covered)
    return E.write_report(root, run_id, owner, "qa", name,
                          {"gate": "CONCERNS" if failures or missing else "PASS", "plan_ref": plan_ref,
                           "required_checks": required, "missing_checks": missing,
                           "checks": checks, "check_failures": failures, "summary": summary})
