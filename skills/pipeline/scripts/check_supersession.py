"""Exact diagnostic replacements attested by the leaf plan's independent tester."""
from pathlib import Path
import shlex

import check_plan as P
import managed_qa as Q
import planned_release
import review_control
import run_evidence as E
import run_state as R


def _observation(root, state, ref, snapshot):
    report = Q._read(root, state, ref, "check")
    proof = report["payload"]
    labels = E.coverage_labels(proof.get("coverage"))
    if (not Q._current(report, state) or proof.get("inherited_from")
            or proof.get("intent") not in {"test", "check"} or not labels
            or {"release_activation", "release_identity", "release_smoke", "release_readiness",
                "release_preflight", "runner_compatibility"} & set(labels)
            or any(attempt.get("report") == ref for attempt in state.get("release_plan", {}).get("attempts", []))):
        raise ValueError("supersession requires a current source test or check")
    if proof.get("observation_kind") == "host_tool":
        P._observed_proof(root, state["run_id"], state, dict(proof, observed_ref=ref))
        started, finished = (R._timestamp(proof[key]) for key in ("started_at", "finished_at"))
        available = R._timestamp(report["recorded_at"])
    else:
        if (proof.get("observation_kind") or not isinstance(proof.get("argv"), list)
                or not proof["argv"] or shlex.join(proof["argv"]) != proof["command"]
                or type(proof.get("exit_code")) is not int):
            raise ValueError("supersession requires original runner or authenticated host evidence")
        status = "executed" if proof["exit_code"] == 0 else "failed"
        started, finished, available = planned_release.executed(state, ref, report, status=status)
    boundary = Q.boundary(state)
    if boundary and started < R._timestamp(boundary):
        raise ValueError("supersession cannot adopt a previous task's execution")
    if P._cached_digest(root, proof["log_path"], snapshot) != proof["log_sha256"]:
        raise ValueError("supersession evidence log changed")
    return proof, started, finished, available


def _judgment(root, state, reports, plan_ref, snapshot, registering):
    ref, report = reports[-1]
    judgment = report["payload"]
    chain = Q.solution_chain(root, state)
    if ("run_policy" not in state or judgment.get("gate") != "PASS" or not chain
            or Q._plans(root, state)[-1] != plan_ref or chain[-1][1]["payload"]["plan_ref"] != plan_ref
            or judgment.get("solution_ref") != chain[-1][0]):
        raise ValueError("supersession requires the current independent solution and reviewed plan")
    requirement = chain[-1][1]["payload"]["requirement_ref"]
    pinned = [requirement, *(path for path, _, _ in chain)]
    if not registering:
        pinned.append(ref)
    if any(state.get("review_digests", {}).get(path) != P._cached_digest(root, path, snapshot) for path in pinned):
        raise ValueError("supersession requires registered digests for the independent reviews")
    result = P.inspect_state(root, state["run_id"], state["owner_token"], plan_ref, state, snapshot=snapshot)
    if judgment.get("checks") != result["checks"]:
        raise ValueError("supersession verification must bind the current ordered checks")
    earliest = max([chain[-1][1]["recorded_at"], *(Q._read(root, state, path, "check")["recorded_at"]
                    for path in result["checks"]), *(prior["recorded_at"] for _, prior in reports[:-1])],
                   key=R._timestamp)
    tester = Q._review_executor(state, reports, "tester", earliest)
    planners = [item for _, _, item in chain]
    identities = set().union(*(Q._executor_ids(item) for item in planners), Q._developers(state))
    if (not tester or not tester.get("host_executor_id") or Q._executor_ids(tester) & identities
            or any(not planner.get("host_executor_id") for planner in planners)
            or any(isinstance(prior["payload"], dict) and prior["payload"].get("dispatch_id") == tester["id"]
                   for _, prior in reports[:-1])):
        raise ValueError("supersession requires a fresh independent completed tester dispatch")
    if review_control.supervised_high_risk(state):
        import process_compliance
        if process_compliance.dispatch_issues(root, state, {"scope_guardian": planners, "tester": [tester]}):
            raise ValueError("supersession requires valid high-risk host dispatch evidence")
    return result["checks"], R._timestamp(tester["accepted_at"])


def _validate(root, state, reports, plan_ref, raw, snapshot, *, registering=False):
    checks, review_start = _judgment(root, state, reports, plan_ref, snapshot, registering)
    rows = reports[-1][1]["payload"]["supersessions"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("supersessions must be a nonempty list")
    replacements = {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) - {"obsolete_ref", "replacement_ref", "reason",
                                                     "obsolete_sha256", "replacement_sha256"}
                or not isinstance(row.get("reason"), str) or not row["reason"].strip()):
            raise ValueError("supersession requires an exact pair and a technical reason")
        old, new = row.get("obsolete_ref"), row.get("replacement_ref")
        if old not in raw or old in replacements or new not in checks:
            raise ValueError("supersession must map a unique unplanned source to a reviewed planned check")
        before, _, ended, old_available = _observation(root, state, old, snapshot)
        after, started, _, new_available = _observation(root, state, new, snapshot)
        if max(old_available, new_available) > review_start:
            raise ValueError("both original observations must be registered and completed before independent review")
        if (ended >= started or before["command"] == after["command"] or P._identity(before) == P._identity(after)
                or {Path(p) for p in before["scope"]} != {Path(p) for p in after["scope"]}
                or not set(before["coverage"]) <= set(after["coverage"]) or after["exit_code"] != 0):
            raise ValueError("supersession requires a later different execution with matching scope and coverage")
        for field, path in (("obsolete_sha256", old), ("replacement_sha256", new)):
            digest = P._cached_digest(root, path, snapshot)
            if registering and field not in row:
                row[field] = digest
            if row.get(field) != digest:
                raise ValueError("supersession check digest is missing or changed")
        replacements[old] = new
    if replacements.keys() & set(replacements.values()):
        raise ValueError("supersessions must be one-hop mappings")
    bindings = {}
    for (plan_path, _), (path, binding) in snapshot["bindings"].items():
        if plan_path == plan_ref and binding["result"]["report"] in replacements.values():
            exit_code = binding["result"].get("exit_code")
            if type(exit_code) is not int or exit_code != 0:
                raise ValueError("replacement binding conflicts with the original successful observation")
            recorded = Q._read(root, state, path, "planned_check")["recorded_at"]
            if R._timestamp(recorded) > review_start:
                raise ValueError("replacement plan binding must be registered before independent review")
            bindings[path] = P._cached_digest(root, path, snapshot)
    return replacements, bindings


def register(root, state, ref, report):
    import run_policy
    run_policy.require(root, state)
    solution = Q._read(root, state, report["payload"].get("solution_ref"), "solution_review")
    plan_ref = solution["payload"]["plan_ref"]
    snapshot = P._snapshot(root, state["run_id"], state)
    plan = P._read_plan(root, state["run_id"], state["owner_token"], plan_ref, state, snapshot)
    reports = Q._review_reports(root, state, "verification_review") + [(ref, report)]
    _, bindings = _validate(root, state, reports, plan_ref, Q.raw_unplanned_checks(root, state, plan),
                            snapshot, registering=True)
    state.setdefault("supersession_reviews", {})[ref] = {"plan_ref": plan_ref, "bindings": bindings}


def approved(root, state, plan_ref, raw, snapshot):
    """An invalid or superseded approval leaves every original obligation visible."""
    registry = state.get("supersession_reviews", {})
    if not registry:
        return {}
    reports = Q._review_reports(root, state, "verification_review")
    if not reports or reports[-1][0] not in registry:
        return {}
    registration = registry[reports[-1][0]]
    if registration["plan_ref"] != plan_ref:
        return {}
    try:
        replacements, bindings = _validate(root, state, reports, plan_ref, raw, snapshot)
        if bindings != registration["bindings"]:
            raise ValueError("current replacement bindings differ from the independent review")
        return replacements
    except (OSError, ValueError, KeyError, TypeError) as error:
        if not raw:
            raise ValueError("supersession evidence changed; revalidate the independent judgment") from error
        return {}
