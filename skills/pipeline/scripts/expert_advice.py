"""Bounded advisory eligibility; never calls a model or grants a gate waiver.

The caller asserts a common unresolved cause / concrete review disagreement.
Report provenance and timing are checked here; semantic sameness is not inferred.
"""
import hashlib
import json
from pathlib import Path

import run_evidence as E
import run_state as R

MAX_BRIEF_CHARS = 6000
TASK_LIMIT = 2
PREPARED_LIMIT = 8
ROLES = {"planner", "scope_guardian"}
TRIGGERS = {"repeated_failure", "review_conflict"}


class _Ineligible(ValueError):
    pass


def _scope(state):
    tasks = state.get("tasks", [])
    return {"kind": "task", "id": tasks[-1]["task_id"]} if tasks else {"kind": "run", "id": state["run_id"]}


def _active(state):
    if state.get("status") != "running":
        raise _Ineligible("run_not_running")
    if state.get("tasks") and state["tasks"][-1].get("status") != "running":
        raise _Ineligible("task_not_running")
    if state.get("activity") == "human_wait" or state.get("continuity_wait", {}).get("status") == "waiting":
        raise _Ineligible("human_wait")


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cases(state):
    scope = _scope(state)
    return [case for case in state.get("expert_advice_cases", []) if case["scope"] == scope]


def _attempts(state):
    return [item for item in state.get("role_dispatches", [])
            if item.get("advice_scope") == _scope(state) and item.get("advice_case_id") is not None]


def _budget(state, case_id, cause):
    attempts = _attempts(state)
    if any(item["advice_case_id"] == case_id for item in attempts):
        raise _Ineligible("case_attempt_used")
    consumed = {item["advice_case_id"] for item in attempts}
    if any(case["case_id"] in consumed and case.get("cause_sha256") == cause for case in _cases(state)):
        raise _Ineligible("cause_attempt_used")
    if len(attempts) >= TASK_LIMIT:
        raise _Ineligible("task_budget_exhausted")


def _brief(text, owner):
    if not isinstance(text, str) or not 0 < len(text) <= MAX_BRIEF_CHARS:
        raise _Ineligible("brief_size")
    if owner and owner in text:
        raise _Ineligible("owner_in_brief")


def _evidence(root, state, trigger, refs):
    if (not isinstance(refs, list) or len(refs) != 2 or any(not isinstance(ref, str) for ref in refs)
            or len(set(refs)) != 2):
        raise _Ineligible("two_distinct_reports_required")
    root = Path(root).resolve(strict=True)
    reports, bindings = [], []
    start = R._timestamp(state["tasks"][-1]["started_at"]) if state.get("tasks") else None
    for ref in refs:
        if ref not in state.get("artifacts", []):
            raise _Ineligible("unregistered_evidence")
        report = E.read_report(root, state["run_id"], ref)
        payload = report["payload"]
        if not isinstance(payload, dict) or payload.get("inherited_from") is not None:
            raise _Ineligible("inherited_or_invalid_evidence")
        if start is not None and (R._timestamp(report["recorded_at"]) < start
                or (payload.get("started_at") and R._timestamp(payload["started_at"]) < start)):
            raise _Ineligible("old_task_evidence")
        reports.append(report)
        bindings.append({"ref": ref, "sha256": _hash(E._local(root, ref))})
    if trigger == "review_conflict":
        if any(report["kind"] not in {"qa", "review"} for report in reports):
            raise _Ineligible("review_reports_required")
    else:
        if any(report["kind"] != "check" for report in reports):
            raise _Ineligible("check_reports_required")
        payloads = [report["payload"] for report in reports]
        for report, payload in zip(reports, payloads):
            if (not isinstance(payload.get("command"), str) or not payload["command"].strip()
                    or not isinstance(payload.get("cwd"), str) or not payload["cwd"]
                    or not isinstance(payload.get("coverage"), list)):
                raise _Ineligible("check_identity_required")
            E.coverage_labels(payload["coverage"])
            if (type(payload.get("exit_code")) is not int or payload["exit_code"] <= 0
                    or payload["exit_code"] in {124, 130, 137, 143}
                    or payload.get("intent") not in {"test", "check"}
                    or payload.get("result_class") not in {"test_failure", "command_failure"}
                    or payload.get("inputs_stable") is not True):
                raise _Ineligible("qualifying_failure_required")
            if not payload.get("started_at") or not payload.get("finished_at"):
                raise _Ineligible("check_timing_required")
            if (R._timestamp(payload["finished_at"]) < R._timestamp(payload["started_at"])
                    or R._timestamp(payload["finished_at"]) > R._timestamp(report["recorded_at"])):
                raise _Ineligible("invalid_check_timing")
            if _hash(E._local(root, payload["log_path"])) != payload["log_sha256"]:
                raise _Ineligible("log_changed")
        payloads.sort(key=lambda p: R._timestamp(p["started_at"]))
        first, last = payloads
        if (R._timestamp(first["started_at"]) == R._timestamp(last["started_at"])
                and R._timestamp(first["finished_at"]) == R._timestamp(last["finished_at"])):
            raise _Ineligible("duplicate_execution")
        if R._timestamp(first["finished_at"]) > R._timestamp(last["started_at"]):
            raise _Ineligible("parallel_failures")
        identity = ("command", "cwd", "intent", "coverage")
        if any(key not in first or key not in last or first[key] != last[key] for key in identity):
            raise _Ineligible("check_identity_changed")
        # A later success supersedes a blocker. Input/environment drift is itself
        # useful diagnostic context, so this does not claim evidence reuse.
        latest_end = R._timestamp(last["finished_at"])
        for ref in reversed(state.get("artifacts", [])):
            if "/artifacts/check/" not in ref:
                continue
            try:
                newer = E.read_report(root, state["run_id"], ref, kind="check")
                # Registration order bounds the scan to the recent execution window.
                if R._timestamp(newer["recorded_at"]) < latest_end:
                    break
                observed = newer["payload"]
                if (not isinstance(observed, dict) or type(observed.get("exit_code")) is not int
                        or observed["exit_code"] != 0 or observed.get("inherited_from") is not None
                        or observed.get("inputs_stable") is not True or observed.get("result_class") != "success"
                        or not all(observed.get(key) == last[key] for key in identity)):
                    continue
                began, ended, recorded = (R._timestamp(observed["started_at"]),
                                          R._timestamp(observed["finished_at"]), R._timestamp(newer["recorded_at"]))
                if (began > ended or ended > recorded or ended < latest_end
                        or (start is not None and (began < start or recorded < start))):
                    continue
                if _hash(E._local(root, observed["log_path"])) != observed["log_sha256"]:
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                continue  # Incomplete/old/malformed success cannot veto a blocker.
            raise _Ineligible("newer_success")
    bindings.sort(key=lambda item: item["ref"])
    # Conservative check identity is a cause slot, never a semantic diagnosis.
    logical = ({key: last[key] for key in identity} if trigger == "repeated_failure"
               else {"reviews": [item["ref"] for item in bindings]})
    if trigger == "repeated_failure":
        logical["coverage"] = E.coverage_labels(logical["coverage"])
    cause = hashlib.sha256(json.dumps(logical, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return bindings, cause



def prepare(root, rid, owner, case_id, trigger, evidence, brief, local_fix_attempted=False):
    """Prepare immutable private metadata under the existing owned write lock.

    A review_conflict call explicitly asserts concrete disagreement; a
    repeated_failure call asserts the same unresolved cause after a local fix.
    """
    E._label(case_id)
    if trigger not in TRIGGERS:
        raise ValueError("unsupported advisory trigger")
    if owner and owner in case_id:
        raise ValueError("owner cannot appear in case identity")
    with R._write_guard(root) as base:
        path, state = R._load_owned(base, rid, owner)
        import run_policy
        run_policy.require(root, state)
        try:
            _active(state)
            _brief(brief, owner)
            if isinstance(evidence, list) and owner and any(owner in ref for ref in evidence if isinstance(ref, str)):
                raise _Ineligible("owner_in_evidence_reference")
            if trigger == "repeated_failure" and local_fix_attempted is not True:
                raise _Ineligible("local_fix_required")
            bindings, cause = _evidence(root, state, trigger, evidence)
            cases = _cases(state)
            pair = next((case for case in cases if case["evidence"] == bindings), None)
            if pair is not None and pair["case_id"] != case_id:
                raise _Ineligible("evidence_already_bound")
            for case in cases:
                if case["case_id"] == case_id:
                    continue
                if set(item["ref"] for item in case["evidence"]) & set(item["ref"] for item in bindings):
                    raise _Ineligible("evidence_already_bound")
                if case.get("cause_sha256") == cause:
                    raise _Ineligible("cause_already_bound")
            existing = next((case for case in cases if case["case_id"] == case_id), None)
            item = {"case_id": case_id, "trigger": trigger, "scope": _scope(state), "evidence": bindings,
                    "brief_sha256": hashlib.sha256(brief.encode("utf-8")).hexdigest(), "brief_chars": len(brief), "cause_sha256": cause}
            if existing is not None and existing != item:
                raise _Ineligible("case_identity_conflict")
            _budget(state, case_id, cause)
            if existing is None:
                if len(cases) >= PREPARED_LIMIT:
                    raise _Ineligible("prepared_limit_reached")
                state.setdefault("expert_advice_cases", []).append(item)
                state["updated_at"] = R._now()
                R._atomic(path, state)
            return {"eligible": True, "reason": "prepared", "case_id": case_id}
        except _Ineligible as error:
            return {"eligible": False, "reason": str(error), "case_id": case_id}
        except (OSError, ValueError, KeyError, TypeError):
            return {"eligible": False, "reason": "unconfirmed_evidence", "case_id": case_id}


def require_dispatch(root, state, case_id, role, native_parameters):
    """Validate while role_dispatch holds its lock; its tagged write reserves budget."""
    _active(state)
    if role not in ROLES:
        raise ValueError("advisory dispatch requires planner or scope_guardian")
    if isinstance(native_parameters, dict) and native_parameters.get("fork_turns") == "all":
        raise ValueError("advisory dispatch forbids declared native fork_turns=all")
    case = next((case for case in _cases(state) if case["case_id"] == case_id), None)
    if case is None:
        raise ValueError("advisory case not prepared in current task")
    try:
        bindings, cause = _evidence(root, state, case["trigger"], [item["ref"] for item in case["evidence"]])
        if bindings != case["evidence"] or cause != case.get("cause_sha256"):
            raise _Ineligible("evidence_changed")
        _budget(state, case_id, cause)
    except (OSError, KeyError, TypeError) as error:
        raise ValueError("advisory evidence cannot be revalidated") from error
    return {"advice_case_id": case_id, "advice_scope": _scope(state), "advice_trigger": case["trigger"]}


def summary(state):
    """Safe current-scope counters, including a latest ended task for read-only status."""
    if not state:
        return {"scope": None, "prepared_cases": 0, "attempts": 0, "remaining": TASK_LIMIT, "cases": []}
    attempts = _attempts(state)
    cases = [{"case_id": case["case_id"], "trigger": case["trigger"],
              "attempts": sum(item["advice_case_id"] == case["case_id"] for item in attempts)}
             for case in _cases(state)[:PREPARED_LIMIT]]
    return {"scope": _scope(state), "prepared_cases": len(_cases(state)), "attempts": len(attempts),
            "remaining": max(0, TASK_LIMIT - len(attempts)), "cases": cases}
