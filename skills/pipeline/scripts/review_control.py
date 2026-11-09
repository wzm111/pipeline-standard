"""Explicit review obligations across tasks and accepted deliveries."""
from copy import deepcopy

import run_evidence as E
import run_state as R


def decision(state):
    tasks = state.get("tasks", [])
    return state.get("review_decisions", {}).get(tasks[-1]["task_id"], {}) if tasks else state.get("run_review_decision", {})


def required(state):
    current = decision(state)
    if state.get("inherited_review") and not state.get("tasks"):
        return True
    return current["required"] if "required" in current else bool(state.get("inherited_review"))


def task_decision(root, state, risk, reason, continuation, retrospective, independent_goal=False):
    import managed_qa as Q
    previous_required = required(state) or bool(Q._review_reports(root, state, "dual_review"))
    if risk is None and continuation is None and not retrospective:
        # Legacy API calls cannot silently discard an outstanding obligation.
        if previous_required:
            return {**deepcopy(decision(state)), "required": True}
        return None
    if risk is None or not isinstance(reason, str) or not reason.strip():
        raise ValueError("review decision requires risk and reason")
    if independent_goal and (continuation is not None or not state.get("tasks")):
        raise ValueError("independent goal requires a prior task and no continuation link")
    source = None
    if continuation is not None:
        task = next((t for t in state.get("tasks", []) if t["task_id"] == continuation), None)
        if task is None or task["status"] == "running":
            raise ValueError("continuation requires an ended task in this run")
        source = state.get("review_decisions", {}).get(continuation, {})
        # Manual declarations are obligations too.
        for ref in state["artifacts"]:
            if "/artifacts/dual_review/" not in ref:
                continue
            report = E.read_report(root, state["run_id"], ref)
            if R._timestamp(task["started_at"]) <= R._timestamp(report["recorded_at"]) <= R._timestamp(task["ended_at"]):
                source = {**source, "required": True}
    return {"risk": risk, "reason": reason, "required": risk in {"high", "unknown"} or retrospective
            or bool(source and source.get("required"))
            or (previous_required and not independent_goal),
            "retrospective": bool(retrospective), "continuation_of": continuation,
            "independent_goal": bool(independent_goal)}


def validate_development(root, state):
    import managed_qa as Q
    if not required(state) and not Q._review_reports(root, state, "dual_review"):
        return
    if decision(state).get("retrospective"):
        raise ValueError("retrospective review is read-only; start an authorized repair task for implementation")
    reports = Q._review_reports(root, state, "solution_review")
    requirements = Q._review_reports(root, state, "dual_review")
    if not reports or not requirements:
        raise ValueError("solution review is required before implementation")
    _, report = reports[-1]
    dispatch = Q._review_executor(state, reports, "scope_guardian", requirements[-1][1]["recorded_at"])
    payload = report["payload"]
    if (not dispatch or payload.get("requirement_ref") != requirements[-1][0]
            or payload.get("plan_ref") not in Q._plans(root, state)):
        raise ValueError("current passed solution review is required before implementation")


def validate_pre_activation(root, state):
    """Only pre-activation obligations, never post-release smoke or closeout."""
    import release_blockers
    release_blockers.require_clear(root, state)
    import managed_qa as Q
    if not required(state) and not Q._review_reports(root, state, "dual_review"):
        return
    verifications = Q._review_reports(root, state, "verification_review")
    solutions = Q._review_reports(root, state, "solution_review")
    if not verifications or not solutions:
        raise ValueError("dual expert verification is required before activation")
    plan_ref = solutions[-1][1]["payload"].get("plan_ref")
    if plan_ref not in Q._plans(root, state):
        raise ValueError("activation requires current reviewed source plan")
    import check_plan as P
    plan = P._read_plan(root, state["run_id"], state["owner_token"], plan_ref, state,
                        P._snapshot(root, state["run_id"], state))
    refs = verifications[-1][1]["payload"].get("checks")
    if not isinstance(refs, list) or not refs or any(ref not in state["artifacts"] for ref in refs):
        raise ValueError("activation requires complete observed verification checks")
    coverage = set()
    applicable, _ = Q._checks(root, state)
    for ref in refs:
        if ref not in applicable:
            raise ValueError("activation requires current task checks or explicit reuse")
        proof = Q._validate_ref(root, state, ref)
        coverage.update(proof.get("coverage", []))
    release = state["release_plan"]
    snapshot = P._snapshot(root, state["run_id"], state)
    post_only = set(release["required_after"]) | {"release_activation"}
    for check_id, check in plan["checks"].items():
        if set(check["coverage"]) <= post_only:
            continue
        observed, reason = P._valid(root, state["run_id"], state["owner_token"],
                                    state, plan_ref, check_id, check, snapshot)
        if observed is None or observed not in refs:
            raise ValueError("reviewed pre-activation check missing, failed or not verified: " + check_id)
    release_refs = {attempt.get("report") for attempt in release.get("attempts", [])}
    if any(ref not in release_refs for ref in Q.unplanned_checks(root, state, plan)):
        raise ValueError("unplanned source checks require reviewed plan coverage before activation")
    source_qa = Q._review_reports(root, state, "qa")
    if source_qa:
        judgment = source_qa[-1][1]["payload"]
        if judgment.get("gate") == "FAIL":
            raise ValueError("latest source QA failed; resolve before activation")
        if judgment.get("gate") == "CONCERNS":
            post_checks = {key for key, check in plan["checks"].items()
                           if set(check["coverage"]) <= post_only}
            failures = set(judgment.get("check_failures", {}))
            missing = set(judgment.get("missing_checks", []))
            if (not (failures or missing) or not failures <= post_checks
                    or not missing <= set(release["required_after"])):
                raise ValueError("source QA has unresolved pre-activation concerns")
    required_before = set(release["required_before"]) | (
        set(plan["required_checks"]) - set(release["required_after"]) - {"build", "release_activation"})
    if not required_before <= coverage:
        raise ValueError("independent verification has missing pre-activation coverage")
    result = Q._apply_expert_reviews(root, state, {"gate": "PASS", "plan_ref": plan_ref,
                                                  "checks": refs})
    if result["gate"] != "PASS":
        raise ValueError("current independent verification is required before activation")
