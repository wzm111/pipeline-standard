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
    if phase == "smoke" and not _activated(plan, candidate):
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
        identity = candidate["id"] if candidate else None
        related = [a for a in attempts if a["phase"] == "activate" and a["candidate_id"] == identity]
        if phase == "activate" and any(a["status"] == "passed" for a in related):
            raise ValueError("candidate already activated; continue smoke and closeout")
        if phase == "activate" and (any(a["status"] == "running" for a in related) or len(related) >= 2):
            raise ValueError("activation recovery limit reached or activation running; stop and report blocker")
        attempt = {"name": name, "phase": phase, "candidate_id": identity, "status": "running", "report": None}
        if any(a["name"] == name for a in attempts):
            raise ValueError("release attempt name already exists")
        attempts.append(attempt)
        current["release_plan"].pop("closeout", None)
        R._atomic(path, current)
    result, accepted = None, False
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
        return result
    finally:
        with R._write_guard(root) as base:
            path, current = _owned(base, run_id, owner_token)
            recorded = next(a for a in current["release_plan"]["attempts"] if a["name"] == name)
            recorded.update(status="passed" if accepted else "failed",
                            report=result["report"] if result else None)
            if phase == "build" and recorded["status"] == "passed" and candidate:
                current["release_plan"]["candidate"] = candidate
            current["updated_at"] = R._now()
            R._atomic(path, current)


def _activated(plan, candidate):
    relevant = [a for a in plan.get("attempts", []) if candidate and a["candidate_id"] == candidate["id"] and a["phase"] == "activate"]
    return bool(relevant and relevant[-1]["status"] == "passed")


def _acceptance(root, state, pending):
    plan = state.get("release_plan", {})
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
