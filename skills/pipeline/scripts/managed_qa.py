"""Managed QA acceptance reuses registered checks/plans under the caller's lock."""
import run_evidence as E
import run_state as R
import review_control


def boundary(state):
    import run_tasks
    tasks = run_tasks._tasks(state)
    return tasks[-1]["started_at"] if tasks else None


def _current(report, state):
    start = boundary(state)
    return start is None or R._timestamp(report["recorded_at"]) >= R._timestamp(start)


def _read(root, state, ref, kind=None):
    if not isinstance(ref, str) or ref not in state["artifacts"]:
        raise ValueError("QA requires registered current-run report references")
    return E.read_report(root, state["run_id"], ref, kind=kind)


def _plans(root, state):
    return [ref for ref in state.get("check_plans", {})
            if _current(_read(root, state, ref, "check_plan"), state)]


def required_for_state(root, state, snapshot=None):
    import check_plan as P
    snapshot = snapshot if snapshot is not None else P._snapshot(root, state["run_id"], state)
    required = set(E.coverage_labels(state.get("inherited_required_checks")))
    for ref in _plans(root, state):
        plan = P._read_plan(root, state["run_id"], state["owner_token"], ref, state, snapshot)
        required.update(plan["required_checks"])
    return sorted(required)


def _checks(root, state):
    """Metadata only. Reuse records bind older evidence to the current task."""
    import check_plan as P
    current, latest = set(), {}
    for ref in state["artifacts"]:
        if not any(f"/artifacts/{kind}/" in ref for kind in ("check", "check_reuse", "planned_check")):
            continue
        report = _read(root, state, ref)
        proof = report["payload"]
        if report["kind"] == "check":
            if proof.get("intent", "test") not in {"test", "check"}:
                continue
            latest[P._identity(proof)] = ref
            source = ref
        elif report["kind"] == "check_reuse":
            source = proof["source"]
        else:
            source = proof["result"]["report"]
        observed = _read(root, state, source, "check")["payload"]
        if observed.get("intent", "test") in {"test", "check"} and _current(report, state):
            current.add(source)
    return current, latest


def _validate_ref(root, state, ref):
    import check_plan as P
    proof = _read(root, state, ref, "check")["payload"]
    environment = proof["environment"]
    if "argv" in proof:
        environment = P._environment(root, state["owner_token"], {
            "argv": proof["argv"], "cwd": proof.get("cwd", "."),
            "environment": {k[8:]: v for k, v in environment.items() if k.startswith("context:")}})
    # Host/manual identities retain their caller-observed contract. This process
    # cannot infer a browser's current environment from its own OS environment.
    verdict = E.reusable_check(root, state["run_id"], ref, command=proof["command"],
                               environment=environment, impact_covered=True, coverage=proof.get("coverage"))
    if not verdict["reusable"]:
        raise ValueError("QA evidence is not current: " + verdict["reason"])
    return proof


def raw_unplanned_checks(root, state, plan):
    """Current-task obligations omitted by a plan, without executing checks."""
    import check_plan as P
    planned = set()
    for check in plan["checks"].values():
        try:
            planned.add(P.planned_identity(root, state["run_id"], state, check))
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Observation validation retains its own failure.
    applicable, _ = _checks(root, state)
    return sorted(source for source in applicable
                  if not _read(root, state, source, "check")["payload"].get("inherited_from")
                  and P._identity(_read(root, state, source, "check")["payload"]) not in planned)


def unplanned_checks(root, state, plan_ref, plan, snapshot):
    import check_supersession
    raw = raw_unplanned_checks(root, state, plan)
    approved = check_supersession.approved(root, state, plan_ref, raw, snapshot)
    return [ref for ref in raw if ref not in approved]


def canonical(root, state, payload, *, release_attempts=()):
    import check_plan as P
    import run_policy
    if not isinstance(payload, dict):
        raise ValueError("managed QA requires structured evidence")
    if payload.get("plan_ref") is None and payload.get("gate") in {"CONCERNS", "FAIL"}:
        # Failure recording needs no refreshed ack. A caller cannot supply its
        # own independent engineering verdict through this path.
        return {key: value for key, value in payload.items() if key != "engineering_gate"}
    run_policy.require(root, state)
    if payload.get("scope") == "engineering_release; pending external outcomes are not passed":
        import release_checks
        expected = release_checks._acceptance(root, state, E.coverage_labels(payload.get("external_pending")))
        if payload != expected:
            raise ValueError("release QA must match current candidate and complete acceptance evidence")
        return expected
    if payload.get("plan_ref") is not None:
        ref = payload["plan_ref"]
        if ref not in _plans(root, state):
            raise ValueError("QA requires a registered plan for the current task")
        snapshot = P._snapshot(root, state["run_id"], state)
        plan = P._read_plan(root, state["run_id"], state["owner_token"], ref, state,
                            snapshot)
        result = P.inspect_state(root, state["run_id"], state["owner_token"], ref, state,
                                 payload.get("summary", ""), snapshot=snapshot)
        if any(path not in release_attempts for path in unplanned_checks(root, state, ref, plan, snapshot)):
            result["gate"] = "CONCERNS"
            result["check_failures"]["unplanned_checks"] = "current_task_checks_not_in_plan"
        if payload.get("gate") in {"CONCERNS", "FAIL"}:
            result["gate"] = payload["gate"]  # A machine pass cannot override a caller's failed business judgment.
        mode = "plan"
    else:
        refs, required = payload.get("checks"), E.coverage_labels(payload.get("required_checks"))
        if not isinstance(refs, list) or len(refs) != 1 or len(required) != 1:
            raise ValueError("managed QA requires a plan or one complete check reference and one required category")
        if _plans(root, state):
            raise ValueError("planned checks require plan-based QA")
        applicable, latest = _checks(root, state)
        proof = _read(root, state, refs[0], "check")["payload"]
        if refs[0] not in applicable:
            raise ValueError("QA check has no observation or explicit reuse in the current task")
        identities = {P._identity(_read(root, state, ref, "check")["payload"]) for ref in applicable}
        labels = {label for ref in applicable for label in E.coverage_labels(
            _read(root, state, ref, "check")["payload"].get("coverage"))}
        if len(identities) != 1 or labels != set(required) or not set(required_for_state(root, state)) <= set(required):
            raise ValueError("multiple checks or required categories require plan-based QA")
        latest_ref = latest.get(P._identity(proof))
        if latest_ref is not None and latest_ref != refs[0]:
            _validate_ref(root, state, latest_ref)
        _validate_ref(root, state, refs[0])
        if E.coverage_labels(proof.get("coverage")) != required:
            raise ValueError("QA required categories do not match observed coverage")
        result = {"gate": "PASS", "checks": refs, "required_checks": required,
                  "summary": payload.get("summary", "")}
        mode = "single"
    result["machine_validation"] = {"schema": 1, "mode": mode, "task_started_at": boundary(state)}
    import visual_evidence
    result = visual_evidence.apply(root, state, result)
    if state.get("supervision_from") and review_control.decision(state).get("risk") == "high":
        result["engineering_gate"] = result["gate"]
    return _apply_expert_reviews(root, state, result)


def validate_finish(root, state):
    current, _ = _checks(root, state)
    qa_refs = [ref for ref in state["artifacts"] if "/artifacts/qa/" in ref]
    exploration_only = all(any(f"/artifacts/{kind}/" in ref for kind in ("check", "check_reuse", "check_review"))
                           for ref in state["artifacts"])
    if not current and not qa_refs and not _plans(root, state) and exploration_only and not _review_reports(root, state, "dual_review") and not review_control.required(state):
        return  # An empty run is not a delivered or accepted product.
    if not qa_refs:
        raise ValueError("current machine-validated QA is required before completing")
    report = _read(root, state, qa_refs[-1], "qa")
    payload = report["payload"]
    mode = ("release" if isinstance(payload.get("machine_validation"), dict) and payload["machine_validation"].get("mode") == "release"
            else "plan" if payload.get("plan_ref") else "single") if isinstance(payload, dict) else None
    if (not _current(report, state) or not isinstance(payload, dict) or payload.get("gate") != "PASS"
            or payload.get("machine_validation") != {"schema": 1, "mode": mode,
                                                       "task_started_at": boundary(state)}):
        raise ValueError("current task machine-validated QA PASS is required before completing")
    if canonical(root, state, payload) != payload:
        raise ValueError("QA evidence changed after acceptance; revalidate current evidence before completing")


def _covered_ancestor(root, state, plan_ref, leaf, plans, snapshot, failures, replacements):
    """A reviewed amendment carries unused slots; an actual failure needs its exact replacement."""
    import check_plan as P
    for key, reason in failures.items():
        if reason == "not_executed":
            continue
        binding = snapshot["bindings"].get((plan_ref, key), (None, {}))[1]
        old = binding.get("result", {}).get("report")
        if old not in replacements or reason not in {
                "check_not_passed", "superseded_observation", "inputs_changed",
                "inputs_changed_during_check", "command_or_environment_changed"}:
            return False
        check = plans[plan_ref]["checks"][key]
        expected = P.planned_identity(root, state["run_id"], state, check)
        proof = _read(root, state, old, "check")["payload"]
        replacement, _ = P._valid(root, state["run_id"], state["owner_token"], state,
                                  leaf, key, plans[leaf]["checks"][key], snapshot)
        if expected != P._identity(proof) or replacements[old] != replacement:
            return False
    return True


def release_requirements(root, state):
    return release_source_scope(root, state)[0]


def release_source_scope(root, state):
    """Keep current source plans and observed categories when release QA joins them."""
    import check_plan as P
    required = set(required_for_state(root, state))
    source_qa = {}
    latest = None
    for ref in state["artifacts"]:
        if "/artifacts/qa/" not in ref:
            continue
        report = _read(root, state, ref, "qa")
        payload = report["payload"]
        if (_current(report, state) and isinstance(payload, dict)
                and payload.get("scope") != "engineering_release; pending external outcomes are not passed"):
            source_qa[payload.get("plan_ref", "single")] = latest = payload
    if any(payload.get("gate") != "PASS" for payload in source_qa.values()):
        raise ValueError("current source QA has unresolved concerns or failure")
    plans = _plans(root, state)
    snapshot = P._snapshot(root, state["run_id"], state)
    definitions = {ref: P._read_plan(root, state["run_id"], state["owner_token"], ref, state, snapshot) for ref in plans}
    replacements, ancestors, attempts = {}, set(), set()
    leaf = latest.get("plan_ref") if latest else None
    if state.get("supersession_reviews") and leaf in definitions:
        import check_supersession
        raw = raw_unplanned_checks(root, state, definitions[leaf])
        replacements = check_supersession.approved(root, state, leaf, raw, snapshot)
        if replacements:
            ancestors = {solution["payload"]["plan_ref"] for _, solution, _ in solution_chain(root, state)[:-1]}
            # The release gate independently rechecks each official phase, candidate and post-release source.
            attempts = {item.get("report") for item in state.get("release_plan", {}).get("attempts", [])}
    if review_control.required(state) or _review_reports(root, state, "dual_review"):
        if not latest or not latest.get("expert_reviews") or canonical(root, state, latest, release_attempts=attempts) != latest:
            raise ValueError("current dual expert QA is required before release acceptance")
    results = {ref: P.inspect_state(root, state["run_id"], state["owner_token"], ref, state, snapshot=snapshot) for ref in plans}
    for index, ref in enumerate(plans):
        if results[ref]["gate"] == "PASS":
            continue
        if (ref in ancestors and results[leaf]["gate"] == "PASS"
                and _covered_ancestor(root, state, ref, leaf, definitions, snapshot,
                                      results[ref]["check_failures"], replacements)):
            continue
        recovered = any(results[later]["gate"] == "PASS" and P.equivalent_observed_recovery(
                        root, state["run_id"], state, definitions[ref], definitions[later])
                        for later in plans[index + 1:])
        if not recovered:
            raise ValueError("current source plan acceptance is incomplete")
    current, _ = _checks(root, state)
    for ref in current:
        required.update(E.coverage_labels(_read(root, state, ref, "check")["payload"].get("coverage")))
    return required, replacements


def validate_release_sources(root, state, coverage):
    current, _ = _checks(root, state)
    for label in coverage["required"]:
        sources = [ref["path"] for ref in coverage["sources"].get(label, []) if ref["path"] in current]
        if not sources:
            raise ValueError("release acceptance needs current-task observation or explicit reuse")
        for ref in sources:
            _validate_ref(root, state, ref)


def _review_reports(root, state, kind):
    reports = []
    for ref in state["artifacts"]:
        if f"/artifacts/{kind}/" not in ref:
            continue
        digest = state.get("review_digests", {}).get(ref)
        if digest:
            import check_plan as P
            if P._digest(root, ref) != digest:
                raise ValueError("registered expert review evidence changed")
        reports.append((ref, _read(root, state, ref, kind)))
    return [(ref, report) for ref, report in reports if _current(report, state)]


def _review_executor(state, reports, role, earliest):
    _, report = reports[-1]
    payload = report["payload"]
    if not isinstance(payload, dict) or payload.get("gate") != "PASS":
        return None
    lower_bound = R._timestamp(earliest)
    for _, prior in reports[:-1]:
        judgment = prior["payload"]
        if isinstance(judgment, dict) and judgment.get("gate") in {"FAIL", "CONCERNS"}:
            lower_bound = max(lower_bound, R._timestamp(prior["recorded_at"]))
    for dispatch in state.get("role_dispatches", []):
        if (dispatch["id"] == payload.get("dispatch_id") and dispatch["role"] == role
                and dispatch["status"] == "completed" and dispatch.get("accepted_at")
                and lower_bound <= R._timestamp(dispatch["accepted_at"])
                <= R._timestamp(dispatch["completed_at"]) <= R._timestamp(report["recorded_at"])):
            return dispatch
    return None


def _executor_ids(dispatch):
    return {dispatch.get("host_executor_id"), dispatch["executor_id"]} - {None}


def _developers(state):
    start = boundary(state)
    return {identity for item in state.get("role_dispatches", [])
            if item["role"] == "developer" and item.get("accepted_at")
            and (start is None or R._timestamp(item["accepted_at"]) >= R._timestamp(start))
            for identity in _executor_ids(item)}


def solution_chain(root, state):
    """Resolve the current leaf back to its timely, independent same-task root."""
    import check_plan as P
    requirements = _review_reports(root, state, "dual_review")
    reports = _review_reports(root, state, "solution_review")
    if not requirements or not reports:
        return []
    requirement_ref, requirement = requirements[-1]
    start = boundary(state)
    implementation = [d["requested_at"] for d in state.get("role_dispatches", []) if d["role"] == "developer"]
    implementation.extend(e["at"] for e in state["history"] if e.get("stage") == "implementation")
    times = [R._timestamp(at) for at in implementation if not start or R._timestamp(at) >= R._timestamp(start)]
    developers = _developers(state)
    plans = set(_plans(root, state))
    snapshot = {"digests": {}, "reports": {}}
    chain, used_dispatches, used_plans = [], set(), set()
    child_plan = None
    for index in range(len(reports) - 1, -1, -1):
        ref, report = reports[index]
        payload = report["payload"]
        if not isinstance(payload, dict) or payload.get("requirement_ref") != requirement_ref:
            return []
        plan_ref = payload.get("plan_ref")
        if not isinstance(plan_ref, str) or plan_ref not in plans or plan_ref in used_plans:
            return []
        plan = P._read_plan(root, state["run_id"], state["owner_token"], plan_ref, state, snapshot)
        plan_time = _read(root, state, plan_ref, "check_plan")["recorded_at"]
        earliest = requirement["recorded_at"]
        if "amends" in payload:
            if index == 0 or payload["amends"] != reports[index - 1][0]:
                return []
            earliest = max(earliest, plan_time, reports[index - 1][1]["recorded_at"], key=R._timestamp)
        elif (times and not review_control.decision(state).get("retrospective")
              and R._timestamp(report["recorded_at"]) > min(times)):
            return []
        planner = _review_executor(state, reports[:index+1], "scope_guardian", earliest)
        if (not planner or planner["id"] in used_dispatches or _executor_ids(planner) & developers
                or R._timestamp(plan_time) > R._timestamp(report["recorded_at"])):
            return []
        if child_plan is not None and (
                not set(plan["required_checks"]) <= set(child_plan["required_checks"])
                or any(key not in child_plan["checks"] or
                       not set(check["coverage"]) <= set(child_plan["checks"][key]["coverage"])
                       for key, check in plan["checks"].items())):
            return []
        chain.append((ref, report, planner))
        used_plans.add(plan_ref)
        used_dispatches.add(planner["id"])
        if "amends" not in payload:
            return list(reversed(chain))
        child_plan = plan
    return []


def _apply_expert_reviews(root, state, result):
    requirements = _review_reports(root, state, "dual_review")
    import review_control
    if not requirements:
        if review_control.required(state):
            if result["gate"] == "PASS":
                result["gate"] = "CONCERNS"
            result.setdefault("check_failures", {})["dual_review"] = "review_required_but_not_declared"
        return result
    feedback = _review_reports(root, state, "progress_feedback")
    if not feedback or feedback[-1][1]["payload"].get("format_valid") is not True:
        if result["gate"] == "PASS":
            result["gate"] = "CONCERNS"
        result.setdefault("check_failures", {})["progress_feedback"] = "missing_or_invalid_visible_feedback"
    else:
        result["feedback_ref"] = feedback[-1][0]
    failure = "missing_solution_review"
    requirement_ref = requirements[-1][0]
    solutions = solution_chain(root, state)
    verifications = _review_reports(root, state, "verification_review")
    if solutions:
        solution_ref, solution, _ = solutions[-1]
        plan_ref = result.get("plan_ref")
        if solution["payload"].get("plan_ref") == plan_ref:
            failure = "missing_current_independent_verification"
            if verifications:
                verification_ref, verification = verifications[-1]
                checks = result["checks"]
                tester = _review_executor(state, verifications, "tester", solution["recorded_at"])
                reviewers = set().union(*(_executor_ids(planner) for _, _, planner in solutions))
                if (tester and not _executor_ids(tester) & (reviewers | _developers(state))
                        and verification["payload"].get("solution_ref") == solution_ref
                        and verification["payload"].get("checks") == checks
                        and all(R._timestamp(_read(root, state, ref, "check")["recorded_at"])
                                <= R._timestamp(tester["completed_at"]) for ref in checks)):
                    result["expert_reviews"] = [requirement_ref, *(ref for ref, _, _ in solutions), verification_ref]
                    return result
    if result["gate"] == "PASS":
        result["gate"] = "CONCERNS"
    result.setdefault("check_failures", {})["dual_review"] = failure
    return result
