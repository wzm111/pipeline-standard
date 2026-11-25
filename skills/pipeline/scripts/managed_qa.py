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
        # Registered release phases have their own coverage/failure gates. They
        # must not become unplanned source tests when source QA is refreshed
        # after pre-activation or post-release independent review.
        phase_reports = {attempt.get("report") for attempt in state.get("release_plan", {}).get("attempts", [])}
        if any(path not in set(release_attempts) | phase_reports
               for path in unplanned_checks(root, state, ref, plan, snapshot)):
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


def _covered_ancestor(root, state, plan_ref, leaf, plans, snapshot, failures, replacements, retained):
    """A reviewed amendment carries unused slots; an actual failure needs its exact replacement."""
    import check_plan as P
    for key, reason in failures.items():
        if reason == "not_executed" and (plan_ref, key) not in snapshot["bindings"]:
            continue
        binding = snapshot["bindings"].get((plan_ref, key), (None, {}))[1]
        old = binding.get("result", {}).get("report")
        check = plans[plan_ref]["checks"][key]
        expected = P.planned_identity(root, state["run_id"], state, check)
        if old in retained:
            declared = plans[leaf]["checks"][retained[old]]
            if (reason in {"inputs_changed", "check_not_passed", "superseded_observation"}
                    and expected == P.planned_identity(root, state["run_id"], state, declared)
                    and check.get("environment", {}) == declared.get("environment", {})
                    and set(check["coverage"]) <= set(declared["coverage"])):
                continue
        replacement, _ = P._valid(root, state["run_id"], state["owner_token"], state,
                                  leaf, key, plans[leaf]["checks"][key], snapshot)
        if replacement and reason in {"inputs_changed", "check_not_passed", "superseded_observation"}:
            current = _read(root, state, replacement, "check")["payload"]
            original = _read(root, state, old, "check")["payload"] if old else None
            # A reviewed amendment may strengthen coverage while rerunning the
            # same check against repaired inputs. Keep its failed ancestor, but
            # accept the leaf's current, fully validated observation.
            if (original and original["environment"] == current["environment"]
                    and check.get("environment", {}) == plans[leaf]["checks"][key].get("environment", {})
                    and expected == P._identity(original) == P._identity(current)
                    and P._cached_digest(root, original["log_path"], snapshot) == original["log_sha256"]
                    and R._timestamp(original["finished_at"]) < R._timestamp(current["started_at"])
                    and E.fingerprint(root, original["scope"], original["environment"]) != original["fingerprint"]
                    and set(check["coverage"]) <= set(current["coverage"])):
                continue
        if old not in replacements or reason not in {
                "check_not_passed", "superseded_observation", "inputs_changed",
                "inputs_changed_during_check", "command_or_environment_changed"}:
            return False
        proof = _read(root, state, old, "check")["payload"]
        if expected != P._identity(proof) or replacements[old] != replacement:
            return False
    return True


def _historical_sources(root, history, plan_ref, plan, qa):
    """Authenticate what the historical plan had bound without replaying its environment."""
    import check_plan as P
    run_id = history["run_id"]
    snapshot = P._snapshot(root, run_id, history)
    refs = []
    covered = set()
    for key, check in plan["checks"].items():
        if "observed_ref" in check:
            ref = check["observed_ref"]
        else:
            binding = snapshot["bindings"].get((plan_ref, key), (None, {}))[1]
            if binding.get("plan_sha256") != history["check_plans"][plan_ref]:
                return False
            result = binding["result"]
            if type(result.get("exit_code")) is not int or result["exit_code"] != 0:
                return False
            ref = result["report"]
        report = _read(root, history, ref, "check")
        proof = report["payload"]
        if "argv" in check:
            if proof.get("argv") != check["argv"]:
                return False
            if not proof.get("inherited_from"):
                import planned_release
                planned_release.executed(history, ref, report)
        if (type(proof.get("exit_code")) is not int or proof["exit_code"] != 0
                or proof.get("inputs_stable") is not True or proof.get("coverage") != check["coverage"]
                or P._identity(proof) != P.planned_identity(root, run_id, history, check)
                or P._cached_digest(root, proof["log_path"], snapshot) != proof["log_sha256"]):
            return False
        refs.append(ref)
        covered.update(proof["coverage"])
    inherited_labels, inherited = P._inherited(root, run_id, history["owner_token"], history, snapshot)
    refs.extend(ref for ref in inherited if ref not in refs)
    covered.update(inherited_labels & set(E.coverage_labels(history.get("inherited_required_checks"))))
    required = required_for_state(root, history, snapshot)
    return qa.get("checks") == refs and qa.get("required_checks") == required and set(required) <= covered


def _retained_checks(root, state, reports, leaf, plan, snapshot):
    """Authenticate explicit same-identity reruns in a pinned independent leaf judgment."""
    import check_plan as P
    import check_supersession as S
    checks, cutoff = S._judgment(root, state, reports, leaf, snapshot, registering=False)
    rows = reports[-1][1]["payload"]["retained_checks"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("retained checks require exact nonempty observation pairs")
    retained = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
                "obsolete_ref", "obsolete_sha256", "replacement_ref", "replacement_sha256"}:
            raise ValueError("retained checks require exact reference and digest pairs")
        old, new = row["obsolete_ref"], row["replacement_ref"]
        if not isinstance(old, str) or old in retained or new not in checks or old in checks:
            raise ValueError("retained checks require a unique historical observation and a reviewed leaf source")
        before, _, ended, old_available = S._observation(root, state, old, snapshot)
        after, started, _, new_available = S._observation(root, state, new, snapshot)
        if (P._cached_digest(root, old, snapshot) != row["obsolete_sha256"]
                or P._cached_digest(root, new, snapshot) != row["replacement_sha256"]
                or ended >= started or max(old_available, new_available) > cutoff
                or P._identity(before) != P._identity(after)
                or any(before.get(k) != after.get(k) for k in ("argv", "environment", "observation_kind"))
                or not set(before["coverage"]) <= set(after["coverage"]) or after["exit_code"] != 0
                or before.get("inputs_stable") is not True or after.get("inputs_stable") is not True
                or (before["exit_code"] != 0 and
                    E.fingerprint(root, before["scope"], before["environment"]) == before["fingerprint"])):
            raise ValueError("retained checks require a later valid rerun of the exact original check")
        # Resolve a renamed slot by its declared identity and environment. Ambiguity fails closed.
        declared_env = (after["environment"] if after.get("observation_kind") else
                        {k[8:]: v for k, v in after["environment"].items() if k.startswith("context:")})
        slots = [key for key, check in plan["checks"].items()
                 if P.planned_identity(root, state["run_id"], state, check) == P._identity(after)
                 and (check.get("environment") or {"scope": "local"}) == declared_env]
        if len(slots) != 1:
            raise ValueError("retained check must match one current leaf slot")
        key = slots[0]
        binding_ref, binding = snapshot["bindings"].get((leaf, key), (None, {}))
        result = binding.get("result", {})
        if (result.get("report") != new or type(result.get("exit_code")) is not int or result["exit_code"] != 0
                or binding.get("plan_sha256") != state["check_plans"][leaf]
                or R._timestamp(_read(root, state, binding_ref, "planned_check")["recorded_at"]) > cutoff
                or P._valid(root, state["run_id"], state["owner_token"], state, leaf,
                            key, plan["checks"][key], snapshot)[0] != new):
            raise ValueError("retained check must bind a valid leaf execution before tester acceptance")
        retained[old] = key
    return retained


def _historical_attempts(root, history, raw, qa_time, snapshot):
    """Only completed original release executions at this historical checkpoint are phase obligations."""
    from pathlib import Path
    import shlex
    import check_plan as P
    import planned_release
    from release_checks import PHASES
    official = set()
    task_start = boundary(history)
    for attempt in history.get("release_plan", {}).get("attempts", []):
        ref = attempt.get("report")
        if ref not in raw or Path(ref).stem != attempt["name"]:
            continue
        report = _read(root, history, ref, "check")
        proof = report["payload"]
        phase, code = attempt["phase"], proof.get("exit_code")
        if (phase not in PHASES or PHASES[phase] not in proof.get("coverage", [])
                or proof.get("inherited_from") or proof.get("observation_kind")
                or not proof.get("argv") or shlex.join(proof["argv"]) != proof["command"]
                or type(code) is not int or attempt.get("result", {}).get("exit_code") != code
                or attempt["result"].get("result_class") != proof.get("result_class")
                or attempt.get("status") != ("passed" if code == 0 else "failed")
                or (phase in {"activate", "smoke"} and (not attempt.get("candidate_id") or
                    proof["environment"].get("context:pipeline_release_candidate") != attempt["candidate_id"]))
                or P._cached_digest(root, proof["log_path"], snapshot) != proof["log_sha256"]):
            raise ValueError("historical release attempt conflicts with its original runner evidence")
        started, _, available = planned_release.executed(
            history, ref, report, status="executed" if code == 0 else "failed")
        if ((task_start and started < R._timestamp(task_start))
                or not R._timestamp(attempt["started_at"]) <= started <= available
                <= R._timestamp(attempt["finished_at"]) <= qa_time):
            raise ValueError("historical release attempt did not complete before source QA")
        official.add(ref)
    return official


def _covered_ancestor_qa(root, state, ref, payload, definitions, solution_ref, latest, replacements, retained, used, snapshot):
    plan_ref = payload["plan_ref"]
    if (payload.get("gate") != "CONCERNS" or payload.get("engineering_gate", "CONCERNS") != "CONCERNS"
            or payload.get("environment_diagnostics") != {} or payload.get("missing_checks") != []
            or ("visual" in payload and payload["visual"].get("complete") is not True)
            or payload.get("check_failures") != {
                "unplanned_checks": "current_task_checks_not_in_plan"}
            or payload.get("machine_validation") != {"schema": 1, "mode": "plan", "task_started_at": boundary(state)}):
        return False
    # Later checks and bindings cannot establish an earlier QA's factual obligations.
    prefix = state["artifacts"][:state["artifacts"].index(ref)]
    history = dict(state, artifacts=prefix, check_plans={path: digest for path, digest in state["check_plans"].items()
                                                         if path in prefix})
    plan = definitions[plan_ref]
    raw = set(raw_unplanned_checks(root, history, plan))
    remaining = raw - replacements.keys() - set(latest["checks"])
    if retained:
        qa_time = R._timestamp(_read(root, state, ref, "qa")["recorded_at"])
        remaining -= _historical_attempts(root, history, raw, qa_time, snapshot)
        used.update(remaining)
    if not raw or not remaining <= retained.keys():
        return False
    expert = _apply_expert_reviews(root, history, {"gate": "PASS", "plan_ref": plan_ref,
                                                   "checks": payload.get("checks", [])})
    reviews = expert.get("expert_reviews", [])
    current_reviews = latest["expert_reviews"]
    if (expert["gate"] != "PASS" or not reviews or reviews != payload.get("expert_reviews")
            or reviews[:-1] != current_reviews[:current_reviews.index(solution_ref) + 1]
            or not all(state.get("review_digests", {}).get(path) for path in reviews)
            or expert.get("feedback_ref") != payload.get("feedback_ref")):
        return False
    return _historical_sources(root, history, plan_ref, plan, payload)


def _executed_subset(plan, result, leaf_plan, leaf_result, required_then):
    """Only new global requirements may be absent from the earlier, fully executed plan."""
    missing = set(result["missing_checks"])
    if (result["check_failures"] or not missing or missing & required_then
            or not missing <= set(leaf_plan["required_checks"]) or leaf_result["gate"] != "PASS"):
        return False
    slots, leaf_slots = plan["checks"], leaf_plan["checks"]
    if not slots.keys() < leaf_slots.keys() or list(slots) != [key for key in leaf_slots if key in slots]:
        return False
    # With no failed slots, inspect_state returns every _valid source in declared order, then inherited sources.
    leaf_sources = dict(zip(leaf_slots, leaf_result["checks"]))
    return (len(result["checks"]) >= len(slots) and len(leaf_sources) == len(leaf_slots)
            and all(slots[key] == leaf_slots[key] and ref == leaf_sources[key]
                    for key, ref in zip(slots, result["checks"])))


def release_requirements(root, state):
    return release_source_scope(root, state)[0]


def release_source_scope(root, state):
    """Keep current source plans and observed categories when release QA joins them."""
    import check_plan as P
    required = set(required_for_state(root, state))
    source_qa, judged_plans = {}, set()
    latest = None
    for ref in state["artifacts"]:
        if "/artifacts/qa/" not in ref:
            continue
        report = _read(root, state, ref, "qa")
        payload = report["payload"]
        if _current(report, state) and isinstance(payload, dict):
            judged_plans.add(payload.get("plan_ref"))
            if payload.get("scope") != "engineering_release; pending external outcomes are not passed":
                source_qa[payload.get("plan_ref", "single")] = (ref, payload)
                latest = payload
    unresolved = [(ref, payload) for ref, payload in source_qa.values() if payload.get("gate") != "PASS"]
    if unresolved and (not latest or latest.get("gate") != "PASS"):
        raise ValueError("current source QA has unresolved concerns or failure")
    plans = _plans(root, state)
    snapshot = P._snapshot(root, state["run_id"], state)
    definitions = {ref: P._read_plan(root, state["run_id"], state["owner_token"], ref, state, snapshot) for ref in plans}
    replacements, ancestors = {}, {}
    leaf = latest.get("plan_ref") if latest else None
    if state.get("supersession_reviews") and leaf in definitions:
        import check_supersession
        raw = raw_unplanned_checks(root, state, definitions[leaf])
        replacements = check_supersession.approved(root, state, leaf, raw, snapshot)
        if replacements:
            judged_plans.update(report["payload"].get("plan_ref") for _, report in
                                _review_reports(root, state, "solution_review") if isinstance(report["payload"], dict))
    if review_control.required(state) or _review_reports(root, state, "dual_review"):
        # Only release-side source revalidation excludes these exact reports. Release phases validate them separately.
        attempts = {item["report"] for item in state.get("release_plan", {}).get("attempts", []) if item.get("report")}
        if (not latest or latest.get("gate") != "PASS" or not latest.get("expert_reviews")
                or canonical(root, state, latest, release_attempts=attempts) != latest):
            raise ValueError("current dual expert QA is required before release acceptance")
        chain = solution_chain(root, state)
        if chain and chain[-1][1]["payload"]["plan_ref"] == leaf:
            ancestors = {solution["payload"]["plan_ref"]: ref for ref, solution, _ in chain[:-1]}
    results = {ref: P.inspect_state(root, state["run_id"], state["owner_token"], ref, state, snapshot=snapshot) for ref in plans}
    retained, used = {}, set()
    verdict = {}
    if latest and latest.get("expert_reviews"):
        verdict = _read(root, state, latest["expert_reviews"][-1], "verification_review")["payload"]
        if "retained_checks" in verdict:
            reports = _review_reports(root, state, "verification_review")
            if (not ancestors or results[leaf]["gate"] != "PASS"
                    or reports[-1][0] != latest["expert_reviews"][-1]):
                raise ValueError("retained checks require current independent leaf QA")
            retained = _retained_checks(root, state, reports, leaf, definitions[leaf], snapshot)
    if replacements or retained or "ancestor_qa" in verdict:
        bindings = verdict.get("ancestor_qa", [])
        if not isinstance(bindings, list):
            raise ValueError("ancestor QA attestations must be a list")
        attested = set()
        for row in bindings:
            if not isinstance(row, dict) or set(row) != {"ref", "sha256"}:
                raise ValueError("ancestor QA requires an exact reference and digest")
            path = row["ref"]
            # Check every pin, even when edited QA metadata removes it from the source/concern grouping.
            _read(root, state, path, "qa")
            if path in attested or row["sha256"] != P._cached_digest(root, path, snapshot):
                raise ValueError("independently attested ancestor QA is duplicated or changed")
            attested.add(path)
        if attested != {path for path, _ in unresolved}:
            raise ValueError("ancestor QA requires its exact independent digest attestation")
    if unresolved:
        if not (replacements or retained) or results[leaf]["gate"] != "PASS" or any(
                qa.get("plan_ref") not in ancestors for _, qa in unresolved):
            raise ValueError("current source QA has unresolved concerns or failure")
        tester = next(item for item in state["role_dispatches"] if item["id"] == verdict["dispatch_id"])
        for path, qa in unresolved:
            when = _read(root, state, path, "qa")["recorded_at"]
            if (R._timestamp(when) > R._timestamp(tester["accepted_at"])
                    or not _covered_ancestor_qa(root, state, path, qa, definitions, ancestors[qa["plan_ref"]],
                                                latest, replacements, retained, used, snapshot)
                    or not _covered_ancestor(root, state, qa["plan_ref"], leaf, definitions, snapshot,
                                             results[qa["plan_ref"]]["check_failures"], replacements, retained)):
                raise ValueError("historical source QA has unresolved concerns or failure")
    if retained.keys() != used:
        raise ValueError("retained checks must attest exactly the outstanding historical source observations")
    required_then = set(E.coverage_labels(state.get("inherited_required_checks")))
    for index, ref in enumerate(plans):
        required_then.update(definitions[ref]["required_checks"])
        if results[ref]["gate"] == "PASS":
            continue
        if (ref in ancestors and results[leaf]["gate"] == "PASS"
                and _covered_ancestor(root, state, ref, leaf, definitions, snapshot,
                                      results[ref]["check_failures"], replacements, retained)):
            continue
        if (replacements and ref not in judged_plans and snapshot["positions"][ref] < snapshot["positions"][leaf]
                and _executed_subset(definitions[ref], results[ref], definitions[leaf], results[leaf], required_then)):
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


def _retry_parent(root, state, reports, index, requirement, plan_time, developers, used_dispatches):
    """Cross only contiguous early guardian attempts for the identical amendment."""
    first = index
    payload = reports[index][1]["payload"]
    while "retries" in reports[first][1]["payload"]:
        if first == 0 or reports[first][1]["payload"]["retries"] != reports[first - 1][0]:
            return None
        first -= 1
        if not isinstance(reports[first][1]["payload"], dict):
            return None
    if first == 0 or payload.get("amends") != reports[first - 1][0]:
        return None
    earliest = max(requirement["recorded_at"], reports[first - 1][1]["recorded_at"], key=R._timestamp)
    candidate_at = R._timestamp(plan_time)
    digests = state.get("review_digests", {})
    early_guardians = []
    for position in range(first, index + 1):
        ref, attempt = reports[position]
        review = attempt["payload"]
        if not digests.get(ref) or any(review.get(key) != payload.get(key)
                                       for key in ("plan_ref", "requirement_ref", "amends")):
            return None
        planner = _review_executor(state, reports[:position + 1], "scope_guardian", earliest)
        if (not planner or not planner.get("host_executor_id") or _executor_ids(planner) & developers
                or planner["id"] in used_dispatches or any(
                    other_ref != ref and isinstance(other["payload"], dict)
                    and other["payload"].get("dispatch_id") == planner["id"] for other_ref, other in reports)):
            return None
        requested, accepted, completed = (R._timestamp(planner[key])
                                          for key in ("requested_at", "accepted_at", "completed_at"))
        task_start = boundary(state)
        if requested > accepted or (task_start and requested < R._timestamp(task_start)):
            return None
        threshold = max(candidate_at, R._timestamp(reports[position - 1][1]["recorded_at"]))
        timely = accepted >= threshold if position == first else accepted > threshold
        if completed < threshold or timely != (position == index):
            return None
        if position < index:
            used_dispatches.add(planner["id"])
            early_guardians.append(planner)
    if review_control.supervised_high_risk(state):
        import process_compliance
        if process_compliance.dispatch_issues(root, state, {"scope_guardian": early_guardians}):
            return None
    return first - 1


def solution_chain(root, state, *, guardians=None):
    """Return accepted ancestry; optionally collect every participant for tester independence."""
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
    index = len(reports) - 1
    retried = False
    while index >= 0:
        ref, report = reports[index]
        payload = report["payload"]
        if not isinstance(payload, dict) or payload.get("requirement_ref") != requirement_ref:
            return []
        plan_ref = payload.get("plan_ref")
        if (not isinstance(plan_ref, str) or plan_ref not in plans
                or (plan_ref in used_plans and plan_ref != chain[-1][1]["payload"]["plan_ref"])):
            return []
        plan = P._read_plan(root, state["run_id"], state["owner_token"], plan_ref, state, snapshot)
        plan_time = _read(root, state, plan_ref, "check_plan")["recorded_at"]
        earliest = requirement["recorded_at"]
        parent_index = index - 1
        if "retries" in payload:
            retried = True
            parent_index = _retry_parent(root, state, reports, index, requirement, plan_time,
                                         developers, used_dispatches)
            if parent_index is None:
                return []
        if "amends" in payload:
            if parent_index < 0 or payload["amends"] != reports[parent_index][0]:
                return []
            earliest = max(earliest, plan_time, reports[parent_index][1]["recorded_at"], key=R._timestamp)
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
            if retried and not all(state.get("review_digests", {}).get(path)
                                   for path in [requirement_ref, *(ref for ref, _, _ in chain)]):
                return []
            if guardians is not None:
                guardians.extend(item for item in state.get("role_dispatches", []) if item["id"] in used_dispatches)
            return list(reversed(chain))
        child_plan = plan
        index = parent_index
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
    guardians = []
    solutions = solution_chain(root, state, guardians=guardians)
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
                reviewers = set().union(*(_executor_ids(planner) for planner in guardians))
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
