"""Small release checkpoints over the existing check runner; no deploy policy.

Callers supply authorized project commands. Live observations are never cached.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import delivery as D
import run_checks as C
import run_evidence as E
import run_state as R

PHASES = {"preflight": "release_preflight", "compatibility": "runner_compatibility",
          "build": "build", "activate": "release_activation", "smoke": "release_smoke"}


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


def cancel_attempt(root, run_id, owner_token, name, *, reason):
    """Record caller cancellation only; this API does not stop local or remote work."""
    if not isinstance(reason, str) or not reason.strip() or owner_token in reason:
        raise ValueError("cancellation reason must be nonempty and exclude owner credentials")
    with R._write_guard(root) as base:
        path, state = _owned(base, run_id, owner_token)
        attempt = next((a for a in state.get("release_plan", {}).get("attempts", []) if a["name"] == name), None)
        if not attempt or attempt["phase"] not in {"build", "activate"}:
            raise ValueError("cancellation requires a registered build or activation attempt")
        if attempt.get("cancel_requested_at") and attempt.get("recovery_hold", {}).get("status") == "held":
            return deepcopy(attempt)
        attempt.update(cancel_requested_at=R._now(), cancel_reason=reason)
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
        plan = {**previous, "required_before": sorted(set(before) | set(previous.get("required_before", []))),
                "required_after": sorted(set(after) | set(previous.get("required_after", []))),
                "attempts": previous.get("attempts", [])}
        if plan != previous:
            state["release_plan"] = plan
            state["updated_at"] = R._now()
            R._atomic(path, state)
        return deepcopy(plan)


def _coverage(root, state, required):
    """Newest failed command blocks older PASS even if its labels differ."""
    sources, seen, blocked = {}, set(), set()
    plan = state.get("release_plan", {})
    latest_phases = {a["phase"]: a for a in plan.get("attempts", [])}
    failed_labels = {PHASES[p] for p, attempt in latest_phases.items() if attempt["status"] != "passed"}
    candidate_id = plan.get("candidate", {}).get("id")
    post_labels = set(plan.get("required_after", [])) | {"release_activation"}
    activations = [a for a in plan.get("attempts", []) if a["phase"] == "activate"
                   and a["candidate_id"] == candidate_id and a["status"] == "passed"]
    activated_at = None
    if activations:
        activation = E.read_report(root, state["run_id"], activations[-1]["report"], kind="check")["payload"]
        activated_at = R._timestamp(activation["finished_at"])
    for path in reversed(state["artifacts"]):
        if "/artifacts/check/" not in path:
            continue
        try:
            payload = E.read_report(root, state["run_id"], path, kind="check")["payload"]
            key = json.dumps([payload.get("command"), payload.get("scope"), payload.get("intent")], sort_keys=True)
            labels = E.coverage_labels(payload.get("coverage"))
            if set(labels) & failed_labels:
                continue
            if any(PHASES[p] in labels and (latest_phases.get(p, {}).get("status") != "passed"
                    or latest_phases[p]["report"] != path) for p in ("preflight", "compatibility")):
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


def _candidate(root, state, coverage=None):
    candidate = state.get("release_plan", {}).get("candidate")
    if not candidate:
        return None
    builds = [a for a in state["release_plan"].get("attempts", []) if a["phase"] == "build"]
    if not builds or builds[-1]["status"] != "passed" or builds[-1]["report"] != candidate["check"]["path"]:
        return None
    try:
        check = candidate["check"]
        coverage = coverage if coverage is not None else _coverage(root, state, ["build"])
        if check not in coverage["sources"].get("build", []):
            return None
        if any(E.fingerprint(root, [a["path"]]) != a["fingerprint"] for a in candidate["assets"]):
            return None
        return candidate
    except (OSError, ValueError, KeyError, TypeError):
        return None


def execute(root, run_id, owner_token, name, *, phase, artifact_paths=None, **options):
    """Precheck first, build once, then at most one automatic activation recovery.

    The project must support its actual candidate/activation commands. This
    helper never invents skip-build flags or weakens isolation/rollback checks.
    """
    if phase not in PHASES:
        raise ValueError("unknown release phase")
    E._label(name)
    if phase == "build" and (not isinstance(artifact_paths, list) or not artifact_paths):
        raise ValueError("build requires explicit artifact or verified manifest paths")
    for path in artifact_paths or []:
        E._local(Path(root).resolve(strict=True), path)
    plan = prepare(root, run_id, owner_token)
    state = R.status(root, run_id)["state"]
    coverage = _coverage(root, state, plan["required_before"])
    if phase in {"build", "activate"}:
        missing = coverage["missing"]
        if missing:
            raise ValueError("release prechecks missing or invalid: " + ",".join(missing))
    candidate = _candidate(root, state, coverage)
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
    with R._write_guard(root) as base:
        path, current = _owned(base, run_id, owner_token)
        attempts = current["release_plan"]["attempts"]
        if phase in {"build", "activate"}:
            if _recovery_blocked(root, current):
                raise ValueError("release recovery hold unresolved; inspect and reconcile before retry")
            if any(a["phase"] in {"build", "activate"} and a["status"] == "running" for a in attempts):
                raise ValueError("build or activation already running")
            current_coverage = _coverage(root, current, current["release_plan"]["required_before"])
            latest_candidate = _candidate(root, current, current_coverage)
            if (current_coverage["missing"] or (latest_candidate or {}).get("id") != (candidate or {}).get("id")):
                raise ValueError("release prechecks or candidate changed while registering")
        identity = candidate["id"] if candidate else None
        related = [a for a in attempts if a["phase"] == "activate" and a["candidate_id"] == identity]
        if phase == "activate" and any(a["status"] == "passed" for a in related):
            raise ValueError("candidate already activated; continue smoke and closeout")
        if phase == "activate" and (any(a["status"] == "running" for a in related) or len(related) >= 2):
            raise ValueError("activation recovery limit reached or activation running; stop and report blocker")
        attempt = {"name": name, "phase": phase, "candidate_id": identity, "status": "running", "report": None,
                   "started_at": R._now()}
        if any(a["name"] == name for a in attempts):
            raise ValueError("release attempt name already exists")
        attempts.append(attempt)
        current["release_plan"].pop("closeout", None)
        R._atomic(path, current)
    result, accepted, completed_normally = None, False, False
    try:
        result = C.execute_check(root, run_id, owner_token, name, coverage=labels, **options)
        passed = result["exit_code"] == 0 and result.get("inputs_stable", result.get("reused") is True) is True
        if phase == "build" and passed:
            assets = [{"path": p, "fingerprint": E.fingerprint(root, [p])} for p in artifact_paths]
            if any(not E._local(Path(root).resolve(strict=True), p).exists() for p in artifact_paths):
                raise ValueError("build did not produce the declared artifacts")
            check = D._reference(Path(root).resolve(strict=True), run_id, result["report"])
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
            if phase in {"build", "activate"}:
                if (not completed_normally or result is None or result.get("exit_code") in {124, 130, 137, 143, 255}
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
                    recorded["result"] = {k: observed[k] for k in ("exit_code", "result_class") if k in observed}
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            if result is not None:
                recorded["result"] = {k: result[k] for k in ("exit_code", "result_class") if k in result}
                result.update(release_accepted=accepted, recovery_blocked=_recovery_blocked(root, current))
            if phase == "build" and recorded["status"] == "passed" and candidate:
                current["release_plan"]["candidate"] = candidate
            current["updated_at"] = R._now()
            R._atomic(path, current)
    return result


def _activated(plan, candidate):
    relevant = [a for a in plan.get("attempts", []) if candidate and a["candidate_id"] == candidate["id"] and a["phase"] == "activate"]
    return bool(relevant and relevant[-1]["status"] == "passed")


def _acceptance(root, state, pending):
    plan = state.get("release_plan", {})
    if _recovery_blocked(root, state):
        raise ValueError("release recovery hold unresolved; cannot accept or finish")
    required = sorted(set(plan.get("required_before", [])) | set(plan.get("required_after", []))
                      | {"build", "release_activation"} | set(state.get("inherited_required_checks") or []))
    coverage = _coverage(root, state, required)
    candidate = _candidate(root, state, coverage)
    if not _activated(plan, candidate) or not coverage["complete"]:
        raise ValueError("release acceptance incomplete; fill only missing or invalid checks")
    return {"gate": "PASS", "required_checks": required, "coverage": coverage,
              "candidate_id": candidate["id"], "external_pending": pending,
              "scope": "engineering_release; pending external outcomes are not passed"}


def validate_closeout(root, state):
    """Called by finish only for an explicitly adopted release plan."""
    reference = state.get("release_plan", {}).get("closeout")
    if not reference:
        raise ValueError("release requires one validated closeout before finishing")
    packet = D._read_reference(Path(root).resolve(strict=True), reference, "qa")
    if packet != _acceptance(root, state, packet["external_pending"]):
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
    return {"report": relative, "reused": reused, "external_pending": pending}
