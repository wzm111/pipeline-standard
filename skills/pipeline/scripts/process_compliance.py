"""Evidence gate for high-risk managed task closeout."""
from pathlib import Path

import model_config
import review_control
import run_state as R


def assess(root, state):
    if not state.get("supervision_from") or review_control.decision(state).get("risk") != "high":
        return None
    import managed_qa as Q

    since = R._timestamp(state["supervision_from"])
    dispatches = [item for item in state.get("role_dispatches", [])
                  if item.get("accepted_at") and R._timestamp(item["requested_at"]) >= since]
    issues = {}
    required = {}
    for role, kind in (("scope_guardian", "solution_review"), ("tester", "verification_review")):
        reports = Q._review_reports(root, state, kind)
        observed = Q._review_executor(state, reports, role, state["supervision_from"]) if reports else None
        required[role] = [observed] if observed else []
    developers = [item for item in dispatches if item["role"] == "developer" and item["status"] == "completed"]
    if developers or any(event.get("stage") == "implementation" and R._timestamp(event["at"]) >= since
                         for event in state["history"]):
        required["developer"] = developers

    project = Path(root).resolve() / "pipeline.models.json"
    personal = Path.home() / ".config/pipeline-standard/models.json"
    for role, items in required.items():
        if not items or any(item not in dispatches or item["status"] != "completed"
                            or not item.get("host_executor_id") for item in items):
            issues[role] = "missing_completed_host_dispatch"
            continue
        for item in items:
            runtime = item.get("routing_runtime")
            if runtime not in model_config.RUNTIMES:
                issues[role] = "missing_routing_runtime"
                break
            try:
                roles = model_config.resolve(runtime, personal, project)["roles"]
                kind = {"developer": "high_risk_implementation", "tester": "high_risk_verification"}.get(role)
                expected = model_config.select_task_route(role, roles[role]["preference"],
                                                          task_kind=kind, risk="high")["preference"]
            except (OSError, ValueError, KeyError):
                issues[role] = "route_configuration_unavailable"
                break
            actual = (item.get("actual_model"), item.get("actual_effort"))
            if actual[0] in {None, "unknown", "inherit"} or (
                    expected.get("effort") and actual[1] in {None, "unknown", "inherit"}):
                issues[role] = "missing_actual_model_or_effort"
                break
            if expected["model"] == "inherit":
                if item.get("requested_model") != "inherit" or item.get("native_parameters", {}).get("model") not in {None, "inherit"}:
                    issues[role] = "model_route_mismatch"
                    break
                continue
            # A follow-up may inherit parameters from a prior launch of the
            # same native executor. Its own actual feedback is still required.
            source = item
            if item.get("mode") == "reuse" and item.get("requested_model") == "inherit":
                source = next((prior for prior in reversed(state["role_dispatches"])
                               if prior.get("mode") == "create" and prior["role"] == role
                               and prior.get("host_executor_id") == item["host_executor_id"]
                               and prior.get("routing_runtime") == runtime
                               and prior.get("status") == "completed"
                               and R._timestamp(prior["completed_at"]) <= R._timestamp(item["accepted_at"])), {})
            native = source.get("native_parameters", {})
            native_effort = native.get("reasoning_effort", native.get("effort"))
            if (source.get("requested_model") != expected["model"] or native.get("model") != expected["model"]
                    or actual[0] != expected["model"] or (expected.get("effort") and
                    any(value != expected["effort"] for value in
                        (source.get("requested_effort"), native_effort, actual[1])))):
                issues[role] = "model_route_mismatch"
                break
    workflow = {}
    import progress_feedback
    if not progress_feedback.entered_verification(state):
        workflow["verification"] = "missing_current_verification_stage"
    reports = Q._review_reports(root, state, "qa")
    qa = reports[-1][1]["payload"] if reports else {}
    if isinstance(qa.get("machine_validation"), dict) and qa["machine_validation"].get("mode") == "release":
        qa = next((report["payload"] for _, report in reversed(reports[:-1])
                   if report["payload"].get("plan_ref")), {})
    review = Q._apply_expert_reviews(root, state, {"gate": "PASS", "plan_ref": qa.get("plan_ref"),
                                                  "checks": qa.get("checks") or []})
    workflow.update(review.get("check_failures", {}))
    return {"gate": "CONCERNS" if issues or workflow else "PASS",
            "dispatch_issues": issues, "workflow_issues": workflow}


def require(root, state):
    result = assess(root, state)
    if result and result["gate"] != "PASS":
        issues = result["dispatch_issues"] | result["workflow_issues"]
        details = ", ".join(f"{key}: {reason}" for key, reason in issues.items())
        raise ValueError("high-risk process compliance has unresolved evidence: " + details)
    return result
