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


def _validate(root, checks, required, owner, inherited, *, scan_inputs=True):
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
        scope = value.get("scope")
        project = Path(root).resolve(strict=True)
        if not isinstance(scope, list) or not scope:
            raise ValueError("nonempty scope required")
        for relative in scope:
            if E._local(project, relative) == project:
                raise ValueError("declare narrow input directories, not the project root")
        if scan_inputs:
            E.fingerprint(root, scope)
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


def _read_plan(root, run_id, owner, plan_ref, state, snapshot):
    if (plan_ref not in state.get("artifacts", []) or plan_ref not in state.get("check_plans", {})
            or _cached_digest(root, plan_ref, snapshot) != state["check_plans"][plan_ref]):
        raise ValueError("unregistered or changed plan")
    payload = _report(root, run_id, plan_ref, "check_plan", snapshot)
    checked = _validate(root, payload["checks"], payload["required_checks"], owner,
                        state.get("inherited_required_checks", []), scan_inputs=False)
    if checked != payload:
        raise ValueError("invalid plan")
    return payload


def read_plan(root, run_id, owner, plan_ref):
    return _read_plan(root, run_id, owner, plan_ref, _owned(root, run_id, owner),
                      {"reports": {}, "digests": {}})


def _report(root, run_id, ref, kind, snapshot):
    key = (ref, kind)
    if key not in snapshot["reports"]:
        snapshot["reports"][key] = E.read_report(root, run_id, ref, kind=kind)["payload"]
    return snapshot["reports"][key]


def _cached_digest(root, ref, snapshot):
    if ref not in snapshot["digests"]:
        snapshot["digests"][ref] = _digest(root, ref)
    return snapshot["digests"][ref]


def _identity(proof):
    return json.dumps([proof.get("command"), proof.get("scope"), proof.get("intent", "test"),
                       proof.get("cwd", ".")], sort_keys=True)


def _snapshot(root, run_id, state):
    # One operation-local index; never reuse across a later execution or QA.
    snapshot = {"reports": {}, "digests": {}, "bindings": {}, "observations": {},
                "positions": {ref: i for i, ref in enumerate(state["artifacts"])}}
    for ref in state["artifacts"]:
        if "/artifacts/planned_check/" in ref:
            p = _report(root, run_id, ref, "planned_check", snapshot)
            snapshot["bindings"][(p["plan_ref"], p["check_id"])] = (ref, p)
        elif "/artifacts/check/" in ref:
            p = _report(root, run_id, ref, "check", snapshot)
            snapshot["observations"][_identity(p)] = ref
    return snapshot


def run_planned(root, run_id, owner, plan_ref, check_id, name, impact_covered=False):
    state = _owned(root, run_id, owner)
    snapshot = _snapshot(root, run_id, state)
    plan = _read_plan(root, run_id, owner, plan_ref, state, snapshot)
    check = plan["checks"][check_id]
    candidate_plan = plan_ref
    previous = snapshot["bindings"].get((plan_ref, check_id), (None, None))[1]
    if previous is None and impact_covered is True:
        candidates = sorted(snapshot["bindings"].items(),
                            key=lambda item: snapshot["positions"][item[1][0]], reverse=True)
        for (old_plan, old_id), (_, binding) in candidates:
            if old_id != check_id or old_plan == plan_ref:
                continue
            try:
                source = _read_plan(root, run_id, owner, old_plan, state, snapshot)
                if source["checks"].get(check_id) == check:
                    candidate_plan, previous = old_plan, binding
                    break
            except (OSError, ValueError, KeyError, TypeError):
                # Unconfirmed history cannot authorize reuse.
                continue
    args = dict(check)
    reusable = previous is not None and _valid(root, run_id, owner, state,
                                               candidate_plan, check_id, check, snapshot)[0] is not None
    result = C.execute_check(root, run_id, owner, name, **args,
                             reuse_from=previous["result"]["report"] if previous else None,
                             impact_covered=impact_covered is True and reusable)
    payload = {"plan_ref": plan_ref, "plan_sha256": _cached_digest(root, plan_ref, snapshot),
               "check_id": check_id, "result": result}
    if result["reused"] and (candidate_plan != plan_ref or previous.get("carried_from_plan")):
        origin = previous.get("carried_from_plan", candidate_plan)
        payload.update(carried_from_plan=origin,
                       carried_from_plan_sha256=_cached_digest(root, origin, snapshot))
    binding = E.write_report(root, run_id, owner, "planned_check", name, payload)
    return {**result, "plan_ref": plan_ref, "check_id": check_id, "planned_report": binding}


def _valid(root, run_id, owner, state, plan_ref, check_id, check, snapshot=None):
    snapshot = snapshot if snapshot is not None else _snapshot(root, run_id, state)
    latest = snapshot["bindings"].get((plan_ref, check_id), (None, None))[1]
    if latest is None:
        return None, "not_executed"
    if latest.get("plan_sha256") != _cached_digest(root, plan_ref, snapshot):
        return None, "plan_changed"
    if latest.get("carried_from_plan"):
        origin = latest["carried_from_plan"]
        original = _read_plan(root, run_id, owner, origin, state, snapshot)
        if (latest.get("carried_from_plan_sha256") != _cached_digest(root, origin, snapshot)
                or original["checks"].get(check_id) != check):
            return None, "carried_plan_changed"
    ref = latest["result"]["report"]
    if ref not in state["artifacts"]:
        return None, "unregistered_evidence"
    proof = _report(root, run_id, ref, "check", snapshot)
    command = shlex.join(check["argv"])
    env = _environment(root, owner, check)
    if (proof.get("argv") != check["argv"] or proof.get("scope") != check["scope"]
            or proof.get("intent") != check.get("intent", "test")
            or proof.get("cwd", ".") != check.get("cwd", ".")):
        return None, "check_identity_changed"
    # A later observation of this command supersedes an older bound PASS,
    # including unplanned failure observations with different coverage labels.
    candidate = snapshot["observations"].get(_identity(proof))
    if candidate and snapshot["positions"][candidate] > snapshot["positions"][ref]:
        newer = _report(root, run_id, candidate, "check", snapshot)
        verdict = E.reusable_check(root, run_id, candidate, command=command, environment=env,
                                   impact_covered=True, coverage=newer.get("coverage"))
        if not verdict["reusable"]:
            return None, "superseded_observation"
    verdict = E.reusable_check(root, run_id, ref, command=command, environment=env,
                               impact_covered=True, coverage=check["coverage"])
    return (ref, None) if verdict["reusable"] else (None, verdict["reason"])


def _environment(root, owner, check):
    child_env = {k: v for k, v in os.environ.items() if k != "PIPELINE_OWNER_TOKEN" and owner not in v}
    cwd = E._local(Path(root).resolve(strict=True), check.get("cwd", "."))
    return C._environment(cwd, check["argv"], child_env, check.get("environment"),
                          project_root=Path(root).resolve(strict=True))


def _inherited(root, run_id, owner, state, snapshot):
    covered, refs, seen = set(), [], set()
    for ref in reversed(state["artifacts"]):
        if "/artifacts/check/" not in ref:
            continue
        try:
            proof = _report(root, run_id, ref, "check", snapshot)
            key = _identity(proof)
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


def inspect_plan(root, run_id, owner, plan_ref, summary=""):
    """Read-only current evidence evaluation; never invent a business judgment."""
    state = _owned(root, run_id, owner)
    snapshot = _snapshot(root, run_id, state)
    plan = _read_plan(root, run_id, owner, plan_ref, state, snapshot)
    checks, failures, diagnostics = [], {}, {}
    for check_id, check in plan["checks"].items():
        try:
            ref, reason = _valid(root, run_id, owner, state, plan_ref, check_id, check, snapshot)
        except (OSError, ValueError, KeyError, TypeError):
            ref, reason = None, "unconfirmed_evidence"
        if ref:
            checks.append(ref)
        else:
            failures[check_id] = reason
            if reason in {"command_or_environment_changed", "superseded_observation"}:
                binding = snapshot["bindings"].get((plan_ref, check_id), (None, None))[1]
                if binding:
                    proof = _report(root, run_id, binding["result"]["report"], "check", snapshot)
                    observed = snapshot["observations"].get(_identity(proof), binding["result"]["report"])
                    latest = _report(root, run_id, observed, "check", snapshot)
                    child_env = {k: v for k, v in os.environ.items()
                                 if k != "PIPELINE_OWNER_TOKEN" and owner not in v}
                    diagnostics[check_id] = C.environment_changes(
                        latest.get("observed_environment_diagnostics"), C.environment_diagnostics(child_env))
    covered = {label for key, check in plan["checks"].items() if key not in failures for label in check["coverage"]}
    inherited, inherited_refs = _inherited(root, run_id, owner, state, snapshot)
    covered.update(inherited & set(E.coverage_labels(state.get("inherited_required_checks"))))
    checks.extend(ref for ref in inherited_refs if ref not in checks)
    required = sorted(set(plan["required_checks"])
                      | set(E.coverage_labels(state.get("planned_required_checks"))))
    missing = sorted(set(required) - covered)
    current = _owned(root, run_id, owner)
    if current["artifacts"] != state["artifacts"] or current.get("check_plans") != state.get("check_plans"):
        failures["snapshot"] = "evidence_changed_during_qa"
    return {"gate": "CONCERNS" if failures or missing else "PASS", "plan_ref": plan_ref,
            "required_checks": required, "missing_checks": missing,
            "checks": checks, "check_failures": failures,
            "environment_diagnostics": diagnostics, "summary": summary}


def qa_from_plan(root, run_id, owner, plan_ref, name, summary=""):
    return E.write_report(root, run_id, owner, "qa", name,
                          inspect_plan(root, run_id, owner, plan_ref, summary))


def verify_plan(root, run_id, owner, plan_ref, name, summary=""):
    """Execute/revalidate one plan then record QA in this actual environment.

    Stops scheduling after the first failed command. Existing evidence is only
    reused through run_planned's full current validation; no environment replay.
    This records evidence, not independent business acceptance or run completion.
    """
    E._label(name)
    plan = read_plan(root, run_id, owner, plan_ref)
    attempts = [(check_id, f"{name}-c{index}")
                for index, check_id in enumerate(plan["checks"], 1)]
    for _, attempt in attempts:
        E._label(attempt)
    results = []
    for check_id, attempt in attempts:
        result = run_planned(root, run_id, owner, plan_ref, check_id, attempt, impact_covered=True)
        results.append({key: result[key] for key in ("check_id", "exit_code", "reused", "report", "planned_report")})
        if result["exit_code"] != 0:
            break
    R.update(root, run_id, owner, {"stage": "acceptance", "activity": "testing"})
    ref = qa_from_plan(root, run_id, owner, plan_ref, name, summary)
    payload = E.read_report(root, run_id, ref, kind="qa")["payload"]
    return {"run_id": run_id, "plan_ref": plan_ref, "report": ref,
            "results": results, "skipped_checks": list(plan["checks"])[len(results):],
            **{key: payload[key] for key in ("gate", "required_checks", "missing_checks",
                                             "check_failures", "environment_diagnostics")},
            "notice": "Plan evidence judgment only; independent business QA and original completion guards still apply."}
