"""Managed QA acceptance reuses registered checks/plans under the caller's lock."""
import run_evidence as E
import run_state as R


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


def canonical(root, state, payload):
    import check_plan as P
    import run_policy
    if not isinstance(payload, dict):
        raise ValueError("managed QA requires structured evidence")
    if payload.get("plan_ref") is None and payload.get("gate") in {"CONCERNS", "FAIL"}:
        return payload  # Failure recording never grants acceptance or needs a refreshed ack.
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
        import shlex
        planned = {P._identity({"command": shlex.join(check["argv"]), "scope": check["scope"],
                               "intent": check.get("intent", "test"), "cwd": check.get("cwd", ".")})
                   for check in plan["checks"].values()}
        applicable, _ = _checks(root, state)
        if any(P._identity(_read(root, state, source, "check")["payload"]) not in planned
               for source in applicable if not _read(root, state, source, "check")["payload"].get("inherited_from")):
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
    return result


def validate_finish(root, state):
    current, _ = _checks(root, state)
    qa_refs = [ref for ref in state["artifacts"] if "/artifacts/qa/" in ref]
    exploration_only = all(any(f"/artifacts/{kind}/" in ref for kind in ("check", "check_reuse", "check_review"))
                           for ref in state["artifacts"])
    if not current and not qa_refs and not _plans(root, state) and exploration_only:
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


def release_requirements(root, state):
    """Keep current source plans and observed categories when release QA joins them."""
    import check_plan as P
    required = set(required_for_state(root, state))
    source_qa = {}
    for ref in state["artifacts"]:
        if "/artifacts/qa/" not in ref:
            continue
        report = _read(root, state, ref, "qa")
        payload = report["payload"]
        if (_current(report, state) and isinstance(payload, dict)
                and payload.get("scope") != "engineering_release; pending external outcomes are not passed"):
            source_qa[payload.get("plan_ref", "single")] = payload
    if any(payload.get("gate") != "PASS" for payload in source_qa.values()):
        raise ValueError("current source QA has unresolved concerns or failure")
    for ref in _plans(root, state):
        result = P.inspect_state(root, state["run_id"], state["owner_token"], ref, state)
        if result["gate"] != "PASS":
            raise ValueError("current source plan acceptance is incomplete")
    current, _ = _checks(root, state)
    for ref in current:
        required.update(E.coverage_labels(_read(root, state, ref, "check")["payload"].get("coverage")))
    return required


def validate_release_sources(root, state, coverage):
    current, _ = _checks(root, state)
    for label in coverage["required"]:
        sources = [ref["path"] for ref in coverage["sources"].get(label, []) if ref["path"] in current]
        if not sources:
            raise ValueError("release acceptance needs current-task observation or explicit reuse")
        for ref in sources:
            _validate_ref(root, state, ref)
