"""Small release checkpoints over the existing check runner; no deploy policy.

Callers supply authorized project commands. Live observations are never cached.
"""
from copy import deepcopy
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True

import delivery as D
import project_release_gate as G
import run_checks as C
import run_evidence as E
import run_state as R

PHASES = {"preflight": "release_preflight", "compatibility": "runner_compatibility",
          "build": "build", "activate": "release_activation", "smoke": "release_smoke",
          "readiness": "release_readiness"}

READINESS_REASONS = {20: "production_driver_unbound", 21: "production_driver_unverified",
                     22: "production_permission_denied", 23: "production_permission_unverified"}


def early_readiness(root, *, environment):
    """Check the declared read-only probe before any release plan exists."""
    root = Path(root).resolve(strict=True)
    environment = E._environment(environment)
    if not isinstance(environment.get("release_target"), str) or not environment["release_target"].strip():
        raise ValueError("early readiness requires a release_target")
    gate = G.snapshot(root)
    if not gate or not gate.get("readiness_probe"):
        return {"ready": False, "reason_code": "readiness_probe_not_declared"}
    request = G.readiness_request(root, gate, {"environment": environment})
    try:
        process = subprocess.run(request["argv"], cwd=root, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return {"ready": False, "reason_code": "readiness_probe_timeout"}
    if G.snapshot(root) != gate:
        return {"ready": False, "reason_code": "readiness_probe_inputs_changed"}
    if process.returncode:
        return {"ready": False, "reason_code": READINESS_REASONS.get(process.returncode, "readiness_probe_failed")}
    return {"ready": True}


class PreflightIncomplete(ValueError):
    def __init__(self, missing):
        super().__init__("release prechecks missing or invalid: " + ",".join(missing))
        self.missing = missing


class CandidateUnavailable(ValueError):
    pass


def _owned(base, run_id, owner):
    path, state = R._load_owned(base, run_id, owner)
    if state["status"] != "running":
        raise ValueError("release requires an active owned run")
    return path, state


def _held(plan):
    return any(a.get("recovery_hold", {}).get("status") == "held" for a in plan.get("attempts", []))


def _recovery_blocked(root, state):
    for attempt in state.get("release_plan", {}).get("attempts", []):
        hold = attempt.get("recovery_hold")
        if not hold:
            continue
        if hold.get("status") != "resolved":
            return True
        try:
            _recovery_evidence(root, state, attempt, hold["evidence"])
        except (OSError, ValueError, KeyError, TypeError):
            return True
    return False


def _recovery_evidence(root, state, attempt, evidence):
    run_id, owner = state["run_id"], state["owner_token"]
    if (not isinstance(evidence, dict) or evidence.get("run_id") != run_id
            or evidence.get("path") not in state["artifacts"]):
        raise ValueError("recovery evidence must be registered in this run")
    payload = D._read_reference(Path(root).resolve(strict=True), evidence, "release_recovery")
    report = E.read_report(root, run_id, evidence["path"], kind="release_recovery")
    recorded = R._timestamp(report["recorded_at"])
    stop = payload.get("stop_evidence")
    boundary = R._timestamp(attempt["recovery_hold"]["held_at"])
    if attempt.get("cancel_requested_at"):
        boundary = max(boundary, R._timestamp(attempt["cancel_requested_at"]))
    if (not boundary < recorded <= R._timestamp(R._now())
            or payload.get("attempt") != attempt["name"] or payload.get("executors_stopped") is not True
            or payload.get("remote_state") not in {"not_activated", "safely_rolled_back"}
            or not isinstance(stop, str) or not stop.strip() or owner in stop):
        raise ValueError("fresh attempt-bound stopped and safe remote recovery evidence required")


def _hold(attempt, reason):
    if attempt.get("recovery_hold", {}).get("status") != "held":
        attempt["recovery_hold"] = {"status": "held", "held_at": R._now(), "reason": reason}


def close_stopped_attempts(state, now):
    """Called under the ownership lock after explicit executor stop confirmation."""
    plan = state.get("release_plan", {})
    for attempt in plan.get("attempts", []):
        if attempt["status"] != "running":
            continue
        if attempt["phase"] in {"build", "activate"} and attempt.get("launch_possible", True):
            _hold(attempt, "execution_interrupted")
        attempt.update(status="failed", finished_at=now, interrupted_at=now)
        plan.pop("closeout", None)


def _mark_launch_possible(root, run_id, owner, name):
    with R._write_guard(root) as base:
        path, state = _owned(base, run_id, owner)
        attempt = next(a for a in state["release_plan"]["attempts"] if a["name"] == name)
        if attempt["status"] != "running":
            raise ValueError("release attempt is no longer running")
        if attempt.get("cancel_requested_at"):
            raise ValueError("release attempt was cancelled before launch")
        if attempt.get("readiness_binding"):
            validate_readiness_binding(root, state, attempt["readiness_binding"], verify_files=False)
        attempt["launch_possible"] = True
        R._atomic(path, state)


def cancel_attempt(root, run_id, owner_token, name, *, reason):
    """Record caller cancellation only; this API does not stop local or remote work."""
    if not isinstance(reason, str) or not reason.strip() or owner_token in reason:
        raise ValueError("cancellation reason must be nonempty and exclude owner credentials")
    with R._write_guard(root) as base:
        path, state = _owned(base, run_id, owner_token)
        attempt = next((a for a in state.get("release_plan", {}).get("attempts", []) if a["name"] == name), None)
        if not attempt or attempt["phase"] not in {"build", "activate"}:
            raise ValueError("cancellation requires a registered build or activation attempt")
        launch_possible = attempt.get("launch_possible", True)
        if attempt["status"] == "failed" and not launch_possible:
            raise ValueError("release attempt already ended before launch")
        if attempt.get("cancel_requested_at") and (
                not launch_possible or attempt.get("recovery_hold", {}).get("status") == "held"):
            return deepcopy(attempt)
        attempt.update(cancel_requested_at=R._now(), cancel_reason=reason)
        if launch_possible:
            _hold(attempt, "cancellation_requested")
        state["release_plan"].pop("closeout", None)
        state["updated_at"] = R._now()
        R._atomic(path, state)
        return deepcopy(attempt)


def reconcile(root, run_id, owner_token, name, *, evidence):
    """Release a hold using current immutable caller-verified recovery evidence.

    The caller must inspect the real executor and remote target. This adapter
    validates the observation's binding; it cannot itself establish remote state.
    """
    with R._write_guard(root) as base:
        path, state = _owned(base, run_id, owner_token)
        attempt = next((a for a in state.get("release_plan", {}).get("attempts", []) if a["name"] == name), None)
        if not attempt or attempt.get("recovery_hold", {}).get("status") != "held":
            raise ValueError("attempt has no unresolved recovery hold")
        if attempt["status"] == "running" or any(c["name"] == name for c in state.get("running_checks", [])):
            raise ValueError("original executor is still registered running")
        _recovery_evidence(root, state, attempt, evidence)
        hold = attempt["recovery_hold"]
        hold.update(status="resolved", resolved_at=R._now(), evidence=deepcopy(evidence))
        state["updated_at"] = R._now()
        R._atomic(path, state)
        return {"attempt": name, "recovery_blocked": _recovery_blocked(root, state),
                "release_accepted": False}


def prepare(root, run_id, owner_token, *, required_before=None, required_after=None):
    """Agent-maintained extra requirements can strengthen, never erase defaults."""
    before = sorted({"release_preflight", "runner_compatibility"} | set(E.coverage_labels(required_before)))
    after = sorted({"release_identity", "release_smoke"} | set(E.coverage_labels(required_after)))
    with R._write_guard(root) as base:
        path, state = _owned(base, run_id, owner_token)
        previous = state.get("release_plan", {})
        gate = G.snapshot(Path(root).resolve(strict=True))
        if previous.get("project_activation_gate") is not None and previous["project_activation_gate"] != gate:
            raise ValueError("project release gate changed after release preparation")
        plan = {**previous, "required_before": sorted(set(before) | set(previous.get("required_before", []))),
                "required_after": sorted(set(after) | set(previous.get("required_after", []))),
                "attempts": previous.get("attempts", [])}
        if gate is not None:
            plan["project_activation_gate"] = gate
            if gate.get("readiness_probe"):
                plan["required_before"] = sorted(set(plan["required_before"]) | {"release_readiness"})
        if not previous:
            plan["prepared_at"] = R._now()
        if plan != previous:
            state["release_plan"] = plan
            state["updated_at"] = R._now()
            R._atomic(path, state)
        return deepcopy(plan)


def _effective_attempts(plan):
    return [attempt for attempt in plan.get("attempts", [])
            if attempt["status"] != "failed" or attempt.get("launch_possible", True)]


def _coverage_identity(payload):
    return json.dumps([payload.get("command"), payload.get("scope"), payload.get("intent")], sort_keys=True)


def _coverage(root, state, required, *, identities=None, excluded=()):
    """Newest failed command blocks older PASS even if its labels differ."""
    sources, seen, blocked = {}, set(), set()
    plan = state.get("release_plan", {})
    latest_phases = {a["phase"]: a for a in _effective_attempts(plan)}
    failed_labels = {PHASES[p] for p, attempt in latest_phases.items() if attempt["status"] != "passed"}
    if (plan.get("project_activation_gate") or {}).get("readiness_probe"):
        probe = latest_phases.get("readiness", {})
        preflight = latest_phases.get("preflight", {})
        if probe.get("status") != "passed" or preflight.get("readiness_report") != probe.get("report"):
            failed_labels.add("release_preflight")
    candidate_id = plan.get("candidate", {}).get("id")
    post_labels = set(plan.get("required_after", [])) | {"release_activation"}
    activations = [a for a in plan.get("attempts", []) if a["phase"] == "activate"
                   and a["candidate_id"] == candidate_id and a["status"] == "passed"]
    activated_at = None
    if activations:
        activation = E.read_report(root, state["run_id"], activations[-1]["report"], kind="check")["payload"]
        activated_at = R._timestamp(activation["finished_at"])
    for path in reversed(state["artifacts"]):
        if "/artifacts/check/" not in path or path in excluded:
            continue
        try:
            payload = E.read_report(root, state["run_id"], path, kind="check")["payload"]
            key = _coverage_identity(payload)
            if identities is not None and key not in identities:
                continue
            labels = E.coverage_labels(payload.get("coverage"))
            if set(labels) & failed_labels:
                continue
            if any(PHASES[p] in labels and (latest_phases.get(p, {}).get("status") != "passed"
                    or latest_phases[p]["report"] != path) for p in ("preflight", "compatibility", "readiness")):
                continue  # An inherited old observation cannot inspect today's host.
            if set(labels) & post_labels:
                if not candidate_id or payload.get("environment", {}).get("context:pipeline_release_candidate") != candidate_id:
                    continue
                if set(labels) & set(plan.get("required_after", [])):
                    if activated_at is None or R._timestamp(payload.get("started_at")) < activated_at:
                        continue
                    if latest_phases.get("smoke", {}).get("report") != path:
                        continue
            labeled = key, tuple(labels)
            if key in blocked or labeled in seen:
                continue
            seen.add(labeled)
            if E.reusable_check(root, state["run_id"], path, command=payload["command"],
                    environment=payload["environment"], impact_covered=True, coverage=labels)["reusable"]:
                reference = D._reference(Path(root).resolve(strict=True), state["run_id"], path)
                for label in labels:
                    sources.setdefault(label, []).append(reference)
            else:
                blocked.add(key)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    missing = sorted(set(required) - set(sources))
    return {"required": sorted(set(required)), "covered": sorted(sources), "missing": missing,
            "complete": not missing, "sources": sources}


def _candidate(root, state, coverage=None, *, verify_assets=True):
    candidate = state.get("release_plan", {}).get("candidate")
    if not candidate:
        return None
    builds = [a for a in _effective_attempts(state["release_plan"]) if a["phase"] == "build"]
    if not builds or builds[-1]["status"] != "passed" or builds[-1]["report"] != candidate["check"]["path"]:
        return None
    try:
        check = candidate["check"]
        coverage = coverage if coverage is not None else _coverage(root, state, ["build"])
        if check not in coverage["sources"].get("build", []):
            return None
        if verify_assets and any(E.fingerprint(root, [a["path"]]) != a["fingerprint"]
                                 for a in candidate["assets"]):
            return None
        return candidate
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _readiness_binding(root, state, coverage, environment):
    if not (state["release_plan"].get("project_activation_gate") or {}).get("readiness_probe"):
        return None
    target = (environment or {}).get("release_target")
    binding = {}
    for label in ("release_readiness", "release_preflight"):
        refs = coverage["sources"].get(label, [])
        match = next((ref for ref in refs if E.read_report(root, state["run_id"], ref["path"], kind="check")
                      ["payload"]["environment"].get("context:release_target") == target), None)
        if match is None:
            raise PreflightIncomplete(["release_readiness_target"])
        binding[label] = match
    binding["runner_compatibility"] = coverage["sources"]["runner_compatibility"][0]
    return binding


def validate_readiness_binding(root, state, binding, *, verify_files=True):
    """Recheck the exact official pair under the launch claim's write lock."""
    latest = {a["phase"]: a for a in _effective_attempts(state["release_plan"])}
    probe, preflight = latest.get("readiness", {}), latest.get("preflight", {})
    if (not binding or probe.get("status") != "passed" or preflight.get("status") != "passed"
            or preflight.get("readiness_report") != probe.get("report")
            or binding["release_readiness"]["path"] != probe.get("report")
            or binding["release_preflight"]["path"] != preflight.get("report")):
        raise PreflightIncomplete(["release_readiness", "release_preflight"])
    compatibility = latest.get("compatibility", {})
    if (compatibility.get("status") != "passed"
            or binding.get("runner_compatibility", {}).get("path") != compatibility.get("report")):
        raise PreflightIncomplete(["runner_compatibility"])
    if verify_files:
        identities = {_coverage_identity(D._read_reference(Path(root).resolve(strict=True), ref, "check"))
                      for ref in binding.values()}
        current = _coverage(root, state, binding, identities=identities)
        missing = [label for label, ref in binding.items() if ref not in current["sources"].get(label, [])]
        if missing:
            raise PreflightIncomplete(missing)


def execute(root, run_id, owner_token, name, *, phase, artifact_paths=None, expected_candidate_id=None, **options):
    """Precheck first, build once, then at most one automatic activation recovery.

    The project must support its actual candidate/activation commands. This
    helper never invents skip-build flags or weakens isolation/rollback checks.
    """
    if phase not in PHASES or phase == "readiness":
        raise ValueError("unknown release phase")
    if expected_candidate_id is not None and (phase not in {"activate", "smoke"}
            or not isinstance(expected_candidate_id, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_candidate_id)):
        raise ValueError("expected_candidate_id requires a candidate ID and an activate or smoke phase")
    E._label(name)
    if phase == "build" and (not isinstance(artifact_paths, list) or not artifact_paths):
        raise ValueError("build requires explicit artifact or verified manifest paths")
    for path in artifact_paths or []:
        E._local(Path(root).resolve(strict=True), path)
    plan = prepare(root, run_id, owner_token)
    gate = plan.get("project_activation_gate", {})
    readiness_report = None
    if phase == "preflight" and gate.get("readiness_probe"):
        project_root = Path(root).resolve(strict=True)
        C.validate_request(project_root, owner_token, **{
            key: options[key] for key in ("argv", "scope", "environment", "write_paths", "timeout", "cwd", "protected_paths")
            if key in options})
        probe_name = "ready-" + hashlib.sha256(name.encode()).hexdigest()[:24]
        readiness = _execute_prepared(root, run_id, owner_token, probe_name, "readiness", plan, None,
                                      G.readiness_request(project_root, gate, options))
        if not readiness["release_accepted"]:
            return readiness
        readiness_report = readiness["report"]
    return _execute_prepared(root, run_id, owner_token, name, phase, plan, artifact_paths, options,
                             readiness_report=readiness_report, expected_candidate_id=expected_candidate_id)


def _execute_prepared(root, run_id, owner_token, name, phase, plan, artifact_paths, options, *, readiness_report=None,
                      expected_candidate_id=None):
    state = R.status(root, run_id)["state"]
    if phase == "activate":
        import review_control
        review_control.validate_pre_activation(root, state)
    candidate = None
    if phase in {"build", "activate", "smoke"}:
        coverage = _coverage(root, state, plan["required_before"])
        if phase in {"build", "activate"} and coverage["missing"]:
            raise PreflightIncomplete(coverage["missing"])
        candidate = _candidate(root, state, coverage)
    if expected_candidate_id is not None and (candidate or {}).get("id") != expected_candidate_id:
        raise CandidateUnavailable("accepted release candidate missing or changed")
    if phase == "activate" and not candidate:
        raise ValueError("validated candidate missing or changed; do not activate")
    if phase == "smoke" and not _activated(plan, candidate) and not (_held(plan) and candidate):
        raise ValueError("candidate has not been successfully activated")
    labels = sorted(set(E.coverage_labels(options.pop("coverage", None))) | {PHASES[phase]})
    # Let the common runner verify requested command, scope and CURRENT process
    # environment too. Valid old output alone cannot certify a new build spec.
    options.pop("reuse_from", None)
    matching_assets = candidate and sorted(artifact_paths or []) == sorted(a["path"] for a in candidate["assets"])
    options["impact_covered"] = bool(phase == "build" and matching_assets)
    if options["impact_covered"]:
        options["reuse_from"] = candidate["check"]["path"]
    options.setdefault("intent", "check")
    if phase == "activate":
        options["write_paths"] = None
    if phase in {"activate", "smoke"}:
        environment = E._environment(options.get("environment") or {"scope": "local"})
        if "pipeline_release_candidate" in environment:
            raise ValueError("release candidate context is maintained by the adapter")
        options["environment"] = {**environment, "pipeline_release_candidate": candidate["id"]}
    if phase in {"build", "activate"}:
        keys = ("argv", "scope", "intent", "environment", "write_paths", "timeout", "cwd", "protected_paths")
        C.validate_request(Path(root).resolve(strict=True), owner_token,
                           **{key: options[key] for key in keys if key in options})
    with R._write_guard(root) as base:
        path, current = _owned(base, run_id, owner_token)
        R.require_not_waiting(current)
        if phase == "activate":
            import review_control
            review_control.validate_pre_activation(root, current)
        attempts = current["release_plan"]["attempts"]
        if phase == "compatibility":
            previous = next((a for a in reversed(attempts) if a["phase"] == phase), None)
            if previous and previous["status"] == "running":
                raise ValueError("runner compatibility already running")
            if previous and previous["status"] == "passed" and previous.get("report"):
                options.update(impact_covered=True, reuse_from=previous["report"])
        readiness_binding = None
        if phase in {"build", "activate"}:
            if _recovery_blocked(root, current):
                raise ValueError("release recovery hold unresolved; inspect and reconcile before retry")
            if any(a["phase"] == "smoke" and a.get("pinned_candidate") and a["status"] == "running"
                   for a in attempts):
                raise ValueError("candidate readback already running")
            if any(a["phase"] in {"build", "activate"} and a["status"] == "running" for a in attempts):
                raise ValueError("build or activation already running")
            current_coverage = _coverage(root, current, current["release_plan"]["required_before"])
            # A configured guard rechecks all asset bytes at the final launch
            # claim. Here we still bind the current build and precheck evidence.
            guarded_activation = phase == "activate" and bool(current["release_plan"].get("project_activation_gate"))
            latest_candidate = _candidate(root, current, current_coverage, verify_assets=not guarded_activation)
            if expected_candidate_id is not None and (latest_candidate or {}).get("id") != expected_candidate_id:
                raise CandidateUnavailable("accepted release candidate missing or changed during activation registration")
            if (current_coverage["missing"] or (latest_candidate or {}).get("id") != (candidate or {}).get("id")):
                if current_coverage["missing"]:
                    raise PreflightIncomplete(current_coverage["missing"])
                raise ValueError("release prechecks or candidate changed while registering")
            if phase == "activate":
                readiness_binding = _readiness_binding(root, current, current_coverage, options.get("environment"))
        if phase == "smoke" and expected_candidate_id is not None:
            current_plan = current["release_plan"]
            if current_plan.get("candidate", {}).get("id") != expected_candidate_id or any(
                    a["phase"] in {"build", "activate"} and a["status"] == "running" for a in attempts):
                raise CandidateUnavailable("accepted release candidate changed or a rollout is in progress")
        identity = candidate["id"] if candidate else None
        related = [a for a in _effective_attempts(current["release_plan"])
                   if a["phase"] == "activate" and a["candidate_id"] == identity]
        if phase == "activate" and any(a["status"] == "passed" for a in related):
            raise ValueError("candidate already activated; continue smoke and closeout")
        if phase == "activate" and (any(a["status"] == "running" for a in related) or len(related) >= 2):
            raise ValueError("activation recovery limit reached or activation running; stop and report blocker")
        attempt = {"name": name, "phase": phase, "candidate_id": identity, "status": "running", "report": None,
                   "started_at": R._now()}
        if phase == "smoke" and expected_candidate_id is not None:
            attempt["pinned_candidate"] = True
        if readiness_report:
            probe = next((a for a in reversed(attempts) if a["phase"] == "readiness"), None)
            if not probe or probe["status"] != "passed" or probe["report"] != readiness_report:
                raise PreflightIncomplete(["release_readiness"])
            attempt["readiness_report"] = readiness_report
        if readiness_binding:
            attempt["readiness_binding"] = readiness_binding
        if phase in {"build", "activate"}:
            attempt["launch_possible"] = False
        if phase == "activate" and current["release_plan"].get("project_activation_gate") is not None:
            attempt["guarded_command"] = G.command_identity({
                "argv": options["argv"], "cwd": options.get("cwd") or ".", "environment": options["environment"],
            })
        if any(a["name"] == name for a in attempts):
            raise ValueError("release attempt name already exists")
        attempts.append(attempt)
        current["release_plan"].pop("closeout", None)
        current["updated_at"] = attempt["started_at"]
        R._atomic(path, current)
    result, accepted, completed_normally = None, False, False
    try:
        if phase in {"build", "activate"}:
            options["_before_launch"] = lambda: _mark_launch_possible(root, run_id, owner_token, name)
        result = C.execute_check(root, run_id, owner_token, name, coverage=labels, **options)
        passed = result["exit_code"] == 0 and result.get("inputs_stable", result.get("reused") is True) is True
        if phase == "build" and passed:
            base = Path(root).resolve(strict=True)
            if any(not E._local(base, p).exists() for p in artifact_paths):
                raise ValueError("build did not produce the declared artifacts")
            assets = [G.candidate_asset(base, p) if plan.get("project_activation_gate")
                      else {"path": p, "fingerprint": E.fingerprint(base, [p])} for p in artifact_paths]
            check = D._reference(base, run_id, result["report"])
            candidate = {"check": check, "assets": assets}
            payload = E.read_report(root, run_id, result["report"], kind="check")["payload"]
            identity = {"assets": assets, **{k: payload[k] for k in ("command", "fingerprint", "environment")}}
            candidate["id"] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        accepted = passed
        completed_normally = True
    finally:
        with R._write_guard(root) as base:
            path, current = _owned(base, run_id, owner_token)
            recorded = next(a for a in current["release_plan"]["attempts"] if a["name"] == name)
            if recorded["status"] != "running":
                raise ValueError("release attempt already closed during ownership recovery")
            if phase in {"build", "activate"}:
                if recorded.get("launch_possible", True) and (
                        not completed_normally or result is None or result.get("exit_code") in {124, 130, 137, 143, 255}
                        or result.get("exit_code", 0) < 0
                        or result.get("result_class") in {"timeout", "interrupted"}):
                    _hold(recorded, "execution_outcome_unknown")
                if recorded.get("cancel_requested_at") or recorded.get("recovery_hold", {}).get("status") == "held":
                    accepted = False
            report = result["report"] if result else None
            if report is None:
                observed = f"tmp/pipeline/runs/{run_id}/artifacts/check/{name}.json"
                if observed in current["artifacts"]:
                    report = observed
            recorded.update(status="passed" if accepted else "failed",
                            report=report, finished_at=R._now())
            if result is None and report is not None:
                # The runner can register raw interruption evidence before it
                # re-raises. Preserve that observation without declaring PASS.
                try:
                    observed = E.read_report(root, run_id, report, kind="check")["payload"]
                    recorded["result"] = {k: observed[k] for k in ("exit_code", "process_exit_code", "result_class")
                                          if k in observed}
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            if result is not None:
                recorded["result"] = {k: result[k] for k in ("exit_code", "result_class") if k in result}
                if phase == "readiness" and not accepted:
                    reason = ("readiness_probe_timeout" if result.get("result_class") == "timeout" else
                              READINESS_REASONS.get(result["exit_code"], "readiness_probe_failed"))
                    recorded["reason_code"] = result["reason_code"] = reason
                result.update(release_accepted=accepted, recovery_blocked=_recovery_blocked(root, current))
            if phase == "build" and recorded["status"] == "passed" and candidate:
                current["release_plan"]["candidate"] = candidate
            current["updated_at"] = R._now()
            R._atomic(path, current)
    return result


def _activated(plan, candidate):
    relevant = [a for a in _effective_attempts(plan)
                if candidate and a["candidate_id"] == candidate["id"] and a["phase"] == "activate"]
    return bool(relevant and relevant[-1]["status"] == "passed")


def _acceptance(root, state, pending):
    import release_blockers
    release_blockers.require_clear(root, state)
    plan = state.get("release_plan", {})
    if _recovery_blocked(root, state):
        raise ValueError("release recovery hold unresolved; cannot accept or finish")
    required = sorted(set(plan.get("required_before", [])) | set(plan.get("required_after", []))
                      | {"build", "release_activation"} | set(state.get("inherited_required_checks") or []))
    replaced = {}
    if "run_policy" in state:
        import managed_qa
        source_required, replaced = managed_qa.release_source_scope(root, state)
        required = sorted(set(required) | source_required)
    coverage = _coverage(root, state, required, excluded=replaced)
    candidate = _candidate(root, state, coverage)
    if not _activated(plan, candidate) or not coverage["complete"]:
        raise ValueError("release acceptance incomplete; fill only missing or invalid checks")
    if (plan.get("project_activation_gate") or {}).get("readiness_probe"):
        activation = next(a for a in reversed(_effective_attempts(plan))
                          if a["phase"] == "activate" and a["candidate_id"] == candidate["id"])
        binding = activation.get("readiness_binding", {})
        if not {"release_readiness", "release_preflight", "runner_compatibility"} <= binding.keys():
            raise PreflightIncomplete(["release_readiness", "release_preflight"])
        historical = {label: D._read_reference(Path(root).resolve(strict=True), ref, "check")
                      for label, ref in binding.items()}
        target = historical["release_readiness"]["environment"].get("context:release_target")
        _readiness_binding(root, state, coverage, {"release_target": target})
    packet = {"gate": "PASS", "required_checks": required, "coverage": coverage,
              "candidate_id": candidate["id"], "external_pending": pending,
              "scope": "engineering_release; pending external outcomes are not passed"}
    if "run_policy" in state:
        managed_qa.validate_release_sources(root, state, coverage)
        packet["machine_validation"] = {"schema": 1, "mode": "release", "task_started_at": managed_qa.boundary(state)}
    import visual_evidence
    return visual_evidence.apply(root, state, packet)


def validate_closeout(root, state):
    """Called by finish only for an explicitly adopted release plan."""
    reference = state.get("release_plan", {}).get("closeout")
    if not reference:
        raise ValueError("release requires one validated closeout before finishing")
    packet = D._read_reference(Path(root).resolve(strict=True), reference, "qa")
    if packet.get("gate") != "PASS" or packet != _acceptance(root, state, packet["external_pending"]):
        raise ValueError("release changed after closeout; revalidate affected evidence")


def closeout(root, run_id, owner_token, *, external_pending=None):
    """Write one compact engineering acceptance; external results stay pending."""
    pending = E.coverage_labels(external_pending)
    with R._write_guard(root) as base:
        _, state = _owned(base, run_id, owner_token)
    packet = _acceptance(root, state, pending)
    digest = hashlib.sha256(json.dumps(packet, sort_keys=True).encode()).hexdigest()[:20]
    relative = f"tmp/pipeline/runs/{run_id}/artifacts/qa/release-{digest}.json"
    if relative in state["artifacts"]:
        if E.read_report(root, run_id, relative, kind="qa")["payload"] != packet:
            raise ValueError("release closeout changed")
        reused = True
    else:
        E.write_report(root, run_id, owner_token, "qa", f"release-{digest}", packet)
        reused = False
    reference = D._reference(Path(root).resolve(strict=True), run_id, relative)
    with R._write_guard(root) as base:
        path, latest = _owned(base, run_id, owner_token)
        if packet != _acceptance(root, latest, pending):
            raise ValueError("release changed while closing")
        if latest["release_plan"].get("closeout") != reference:
            latest["release_plan"]["closeout"] = reference
            latest["updated_at"] = R._now()
            R._atomic(path, latest)
    result = {"report": relative, "reused": reused, "external_pending": pending}
    if packet["gate"] != "PASS":
        result["gate"] = packet["gate"]
    return result


def public_status(root, run_id, owner_token):
    """Read current gate validity without exposing commands or ownership secrets."""
    _, base = R._paths(root)
    _, state = _owned(base, run_id, owner_token)
    plan = state.get("release_plan", {})
    coverage = _coverage(root, state, plan.get("required_before", []))
    candidate = _candidate(root, state, coverage)
    activated = _activated(plan, candidate)
    recovery = _recovery_blocked(root, state)
    after = set(plan.get("required_after", []))
    missing_after = sorted(after - set(coverage["covered"])) if activated else []
    attempts = plan.get("attempts", [])
    related = [a for a in _effective_attempts(plan)
               if candidate and a["phase"] == "activate" and a["candidate_id"] == candidate["id"]]
    from release_blockers import pending
    if state.get("continuity_wait", {}).get("status") == "waiting":
        action = "resolve_human_wait"
    elif recovery:
        action = "reconcile_release"
    elif any(a["status"] == "running" for a in attempts):
        action = "wait_for_running_phase"
    elif not plan:
        action = "prepare"
    elif coverage["missing"]:
        action = "fill_missing_prechecks"
    elif not activated and any(a["phase"] == "activate" and a["status"] == "passed" for a in attempts):
        action = "inspect_activated_release"
    elif not candidate:
        action = "build"
    elif pending(root, state):
        action = "resolve_release_blocker"
    elif not activated and len(related) >= 2:
        action = "inspect_activation_failure"
    elif not activated:
        action = "activate"
    elif missing_after:
        action = "smoke"
    elif plan.get("closeout"):
        try:
            closeout = D._read_reference(Path(root).resolve(strict=True), plan["closeout"], "qa")
            action = ("finish" if closeout.get("gate") == "PASS" and closeout.get("candidate_id") == candidate["id"]
                      else "inspect_closeout")
        except (OSError, ValueError, KeyError, TypeError):
            action = "inspect_closeout"
    else:
        action = "closeout"
    return {"run_id": run_id, "prepared": bool(plan),
            "candidate_id": candidate["id"] if candidate else None,
            "activated": activated,
            "missing_before": coverage["missing"],
            "missing_after": missing_after,
            "next_action": action,
            "recovery_blocked": recovery,
            "attempts": [{k: a[k] for k in ("name", "phase", "status", "reason_code") if k in a}
                         for a in attempts]}


def _operation_check(root, state, reference, target, *, phase, current=False):
    """Historical identity is not a claim about today's execution environment."""
    import managed_qa
    if (not isinstance(reference, dict) or reference.get("run_id") != state["run_id"]
            or reference.get("path") not in state["artifacts"]):
        raise ValueError("operation evidence must be registered in this run")
    payload = D._read_reference(root, reference, "check")
    report = E.read_report(root, state["run_id"], reference["path"], kind="check")
    if not managed_qa._current(report, state) or payload.get("inherited_from") or payload.get("reused"):
        raise ValueError("operation requires actual current-task execution")
    attempts = [a for a in state["release_plan"]["attempts"] if a["phase"] == phase]
    if (not attempts or attempts[-1]["status"] != "passed"
            or attempts[-1]["report"] != reference["path"]
            or attempts[-1]["candidate_id"] != state["release_plan"]["candidate"]["id"]):
        raise ValueError("latest registered operation phase is not this successful check")
    if any(payload.get("environment", {}).get("context:" + key) != value for key, value in target.items()):
        raise ValueError("explicit target environment does not match observed operation")
    labels = set(E.coverage_labels(payload.get("coverage")))
    required = {PHASES[phase]} | ({"release_identity"} if phase == "smoke" else set())
    if not required <= labels:
        raise ValueError("operation or live identity coverage missing")
    # A later matching command or relevant phase observation must never be hidden
    # by the saved successful reference, even when it used different labels.
    identity = lambda p: [p.get(k) for k in ("command", "scope", "intent", "cwd")]
    newer = state["artifacts"][state["artifacts"].index(reference["path"]) + 1:]
    for path in newer:
        if "/artifacts/check/" in path:
            later = E.read_report(root, state["run_id"], path, kind="check")["payload"]
            if (identity(later) == identity(payload) or PHASES[phase] in later.get("coverage", [])):
                raise ValueError("later operation observation requires an updated proof")
    verdict = E.reusable_check(root, state["run_id"], reference["path"], command=payload["command"],
        environment=payload["environment"], impact_covered=True, coverage=payload["coverage"])
    if not verdict["reusable"]:
        raise ValueError("historical operation identity invalid")
    if current:
        if not payload.get("argv"):
            raise ValueError("current readback requires actual runner environment identity")
        managed_qa._validate_ref(root, state, reference["path"])
    return payload


def _operation_binding(root, state, target, activation, readback, *, current_readback=False):
    import managed_qa
    if _recovery_blocked(root, state):
        raise ValueError("release recovery unresolved")
    candidate = _candidate(root, state)
    if not candidate:
        raise ValueError("current exact candidate is unavailable")
    mutation = _operation_check(root, state, activation, target, phase="activate")
    live = _operation_check(root, state, readback, target, phase="smoke", current=current_readback)
    if (mutation["environment"].get("context:pipeline_release_candidate") != candidate["id"]
            or live["environment"].get("context:pipeline_release_candidate") != candidate["id"]
            or R._timestamp(live["started_at"]) < R._timestamp(mutation["finished_at"])):
        raise ValueError("live readback must follow activation of this exact candidate")
    return {"schema": 1, "candidate": deepcopy(candidate), "task_started_at": managed_qa.boundary(state),
            "target_sha256": hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest(),
            "activation": deepcopy(activation), "readback": deepcopy(readback)}


def capture_operation_proof(root, run_id, owner_token, name, *, target_environment):
    """Persist explicit historical release identity, without granting QA acceptance.

    Target fields must already be actual context on activation and smoke checks.
    No deployment command runs here; capture requires a currently valid readback.
    """
    root = Path(root).resolve(strict=True)
    target = E._environment(target_environment)
    with R._write_guard(root) as base:
        _, state = _owned(base, run_id, owner_token)
        references = {}
        for phase in ("activate", "smoke"):
            attempts = [a for a in state.get("release_plan", {}).get("attempts", []) if a["phase"] == phase]
            if not attempts or not attempts[-1].get("report"):
                raise ValueError("successful activation and live readback required")
            references[phase] = D._reference(root, run_id, attempts[-1]["report"])
        binding = _operation_binding(root, state, target, references["activate"], references["smoke"],
                                     current_readback=True)
    relative = E.write_report(root, run_id, owner_token, "release_operation", name, binding)
    return {"proof": D._reference(root, run_id, relative), "acceptance_status": "CONCERNS",
            "next_action": "validate_original_strict_qa_contract"}


def inspect_operation_proof(root, run_id, *, proof, target_environment, readback=None):
    """Read-only diagnostics; never replay historical environment or mutations.

    A fresh explicit readback can replace the saved observation. Generic QA and
    its full current-environment requirements remain separate and unchanged.
    """
    root = Path(root).resolve(strict=True)
    before = R.status(root, run_id)
    state = before["state"]
    result = {"valid_operation": False, "historical_mutation_valid": False,
              "current_readback_valid": False, "acceptance_status": "CONCERNS",
              "next_action": "repair_operation_binding_without_repeating_mutation"}
    try:
        target = E._environment(target_environment)
        if (not state or not isinstance(proof, dict) or proof.get("run_id") != run_id
                or proof.get("path") not in state["artifacts"]):
            raise ValueError("registered current-run operation proof required")
        saved = D._read_reference(root, proof, "release_operation")
        if not isinstance(saved, dict):
            raise ValueError("structured operation proof required")
        # Saved observation remains authenticated, even when a newer live
        # observation is supplied. Only its phase freshness may be superseded.
        D._read_reference(root, saved["readback"], "check")
        selected = readback or saved["readback"]
        historical = _operation_binding(root, state, target, saved["activation"], selected)
        if any(saved.get(k) != historical[k] for k in ("schema", "candidate", "task_started_at", "target_sha256", "activation")):
            raise ValueError("operation candidate, task or target changed")
        result["historical_mutation_valid"] = True
        result["next_action"] = "run_current_readonly_live_identity_check"
        _operation_binding(root, state, target, saved["activation"], selected, current_readback=True)
        result.update(valid_operation=True, current_readback_valid=True,
                      next_action="validate_original_strict_qa_contract")
    except (OSError, ValueError, KeyError, TypeError):
        pass  # Public diagnostics never include argv, context or private values.
    if R.status(root, run_id) != before:
        raise ValueError("run changed during operation inspection; recheck")
    return result


def main(argv=None):
    # Reuse the original receipt ownership/path/permission checks; no second
    # credential format or lifecycle entrypoint.
    from pipeline_cli import _receipt
    parser = argparse.ArgumentParser(description="Invoke existing authorized project release commands.")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("early-readiness", "prepare", "execute", "status", "closeout", "operation-capture",
                   "operation-inspect"):
        command = sub.add_parser(action, allow_abbrev=False)
        command.add_argument("--project", default=".")
        if action == "operation-inspect":
            command.add_argument("--run-id", required=True)
        elif action != "early-readiness":
            command.add_argument("--receipt", required=True)
        if action != "status":
            command.add_argument("--json", required=True, dest="declaration")
    args = parser.parse_args(argv)
    try:
        declaration = {} if args.action == "status" else json.loads(args.declaration)
        allowed = {"early-readiness": {"environment"},
                   "prepare": {"required_before", "required_after"},
                   "execute": {"name", "phase", "argv", "scope", "artifact_paths", "timeout", "cwd",
                               "environment", "coverage", "write_paths", "protected_paths", "expected_candidate_id"},
                   "closeout": {"external_pending"}, "status": set(),
                   "operation-capture": {"name", "target_environment"},
                   "operation-inspect": {"proof", "target_environment", "readback"}}[args.action]
        if not isinstance(declaration, dict) or set(declaration) - allowed:
            raise ValueError("unknown declaration fields")
        if args.action == "execute" and not {"name", "phase", "argv", "scope"} <= set(declaration):
            raise ValueError("execute requires name, phase, argv and scope")
        required = {"early-readiness": {"environment"}, "operation-capture": {"name", "target_environment"},
                    "operation-inspect": {"proof", "target_environment"}}.get(args.action, set())
        if not required <= set(declaration):
            raise ValueError("operation declaration missing required fields")
        root = Path(args.project).resolve(strict=True)
        if args.action not in {"early-readiness", "operation-inspect"}:
            receipt = _receipt(root, args.receipt)
            owned = root, receipt["run_id"], receipt["owner_token"]
        code = 0
        if args.action == "early-readiness":
            result = early_readiness(root, **declaration)
            code = 0 if result["ready"] else 1
        elif args.action == "operation-inspect":
            E._environment(declaration["target_environment"])
            if (not isinstance(declaration["proof"], dict)
                    or set(declaration["proof"]) != {"run_id", "path", "sha256"}
                    or any(not isinstance(v, str) or not v for v in declaration["proof"].values())):
                raise ValueError("structured proof reference required")
            if "readback" in declaration and (not isinstance(declaration["readback"], dict)
                    or set(declaration["readback"]) != {"run_id", "path", "sha256"}
                    or any(not isinstance(v, str) or not v for v in declaration["readback"].values())):
                raise ValueError("structured readback reference required")
            result = inspect_operation_proof(root, args.run_id, **declaration)
            code = 0 if result["valid_operation"] else 1
        elif args.action == "operation-capture":
            result = capture_operation_proof(*owned, **declaration)
        elif args.action == "prepare":
            prepare(*owned, **declaration)
            result = public_status(*owned)
        elif args.action == "execute":
            executed = execute(*owned, **declaration)
            result = {k: executed[k] for k in ("report", "exit_code", "reused", "release_accepted", "recovery_blocked")}
            if "reason_code" in executed:
                result["reason_code"] = executed["reason_code"]
            code = 0 if executed["release_accepted"] else 1
        elif args.action == "closeout":
            result = closeout(*owned, **declaration)
            code = 0 if result.get("gate", "PASS") == "PASS" else 1
        else:
            result = public_status(*owned)
    except PreflightIncomplete as error:
        result, code = {"error": "preflight_incomplete", "reason_code": "release_preflight_evidence_missing",
                        "missing": error.missing}, 2
    except CandidateUnavailable:
        result, code = {"error": "candidate_unavailable", "reason_code": "release_candidate_missing_or_changed"}, 2
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Exception text can contain submitted argv, paths or environment values.
        result, code = {"error": type(error).__name__, "message": "release request rejected; inspect the declaration and current gate evidence"}, 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
