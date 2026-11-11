"""Compact direct-workflow entrypoint. Receipts are private ownership credentials."""
import argparse
import json
from pathlib import Path
import sys

# Importing a workflow command must not mutate its own declared source scope.
# In particular, finish may import delivery for the first time after QA.
if __name__ == "__main__":
    sys.dont_write_bytecode = True

import run_checks as C
import run_evidence as E
import run_state as R
import role_dispatch as D
import resource_paths


def paths(project=None):
    return resource_paths.discover(Path(__file__), project=project)


def _receipt(root, value):
    supplied = Path(value)
    relative = supplied.relative_to(root).as_posix() if supplied.is_absolute() else supplied.as_posix()
    path = E._local(root, relative)
    if path.stat().st_mode & 0o077:
        raise ValueError("receipt must be private to its owner")
    receipt = R._read(path)
    if receipt.get("project_root") != str(root):
        raise ValueError("receipt belongs to another project")
    _, base = R._paths(root)
    R._load_owned(base, receipt["run_id"], receipt["owner_token"])
    return receipt


def _public(state):
    if state is None:
        return {"run_id": None, "status": None}
    return {key: state.get(key) for key in ("run_id", "status", "stage", "activity", "pending", "next")}


def _plan_payload(value):
    payload = json.loads(value)
    if not isinstance(payload, dict) or set(payload) - {"checks", "required_checks"}:
        raise ValueError("plan accepts checks and required_checks only")
    return payload


def _optional_audit(root, run_id):
    # The lifecycle already succeeded. Optional diagnostics cannot undo it.
    try:
        import run_audit as A
        return A.audit(root, run_id)
    except Exception:
        return {"status": "error", "error": "collector_unavailable"}


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="action", required=True)
    resources = sub.add_parser("paths", allow_abbrev=False, help="resolve real installed resources without starting a run")
    resources.add_argument("--project", help="validate project root; resource paths remain anchored to the runner")
    audit = sub.add_parser("audit", help="read a safe existing audit; --live computes metadata in memory only")
    audit.add_argument("--project", default=".")
    audit.add_argument("--run-id", required=True)
    audit.add_argument("--live", action="store_true")
    scan = sub.add_parser("scan", help="bounded read-only existing audit discovery, no repair")
    scan.add_argument("--project", default=".")
    scan.add_argument("--limit", type=int, default=20)
    multi_scan = sub.add_parser("scan-projects", help="read-only local index across project audit directories")
    source = multi_scan.add_mutually_exclusive_group(required=True)
    source.add_argument("--project", action="append", help="project root, repeat to include more projects")
    source.add_argument("--workspace", help="scan immediate project directories only")
    multi_scan.add_argument("--since", help="inclusive snapshot timestamp with timezone")
    multi_scan.add_argument("--limit", type=int, default=5, help="latest run candidates per project, 1 to 20")
    multi_scan.add_argument("--live", action="store_true", help="read registered metadata without saving snapshots")
    multi_scan.add_argument("--format", choices=["json", "text"], default="json")
    preview = sub.add_parser("preview", help="local read-only audit preview; never upload, export or start a run")
    preview.add_argument("--project", default=".")
    preview.add_argument("--run-id", help="explicit run; otherwise select active or latest local record")
    preview.add_argument("--live", action="store_true", help="compute existing metadata in memory without saving")
    preview.add_argument("--format", choices=["text", "json"], default="text")
    status = sub.add_parser("status", help="compact read-only status, no credentials")
    status.add_argument("--project", default=".")
    status.add_argument("--run-id")
    status.add_argument("--recovery", action="store_true", help="bounded registered recovery pointers; no retry permission")
    status.add_argument("--host-status", choices=["unknown", "running", "idle"], default="unknown")
    status.add_argument("--host-ended-at", help="explicit native host idle boundary, never inferred from run age")
    progress = sub.add_parser("progress", help="bounded read-only steps; no health or acceptance claim")
    progress.add_argument("--project", default=".")
    progress.add_argument("--run-id")
    progress.add_argument("--format", choices=["json", "text"], default="json")
    progress.add_argument("--host-status", choices=["unknown", "running", "idle"], default="unknown")
    progress.add_argument("--host-ended-at", help="actual native idle time; never inferred from registration")
    progress.add_argument("--after-milestone", help="last returned milestone_id; unchanged returns a compact delta")
    task_status = sub.add_parser("task-status", help="read-only execution task records, not acceptance")
    task_status.add_argument("--project", default=".")
    task_status.add_argument("--run-id")
    release_timing = sub.add_parser("release-timing", help="read-only measured release steps, no extra deployment")
    release_timing.add_argument("--project", default=".")
    release_timing.add_argument("--run-id", required=True)
    release_timing.add_argument("--check", help="optional current-run monolithic release check reference")
    start = sub.add_parser("start", help="start after reading contract; retain the returned private receipt path")
    start.add_argument("--project", default=".")
    start.add_argument("--review-risk", required=True, choices=["low", "medium", "high", "unknown"])
    start.add_argument("--review-reason", required=True)
    start.add_argument("--stage", default="execution")
    start.add_argument("--delivery-from", choices=["latest"])
    start.add_argument("--compact", action="store_true", help="short success response; full artifacts and error diagnostics remain unchanged")
    policy = sub.add_parser("policy", help="inspect current rule revision and caller acknowledgement without writes")
    policy.add_argument("--project", default=".")
    policy.add_argument("--run-id")
    allocation = sub.add_parser("allocation-status", help="read-only coordinator decision and registered dispatch boundaries")
    allocation.add_argument("--project", default=".")
    allocation.add_argument("--run-id")
    expert = sub.add_parser("expert-status", help="read-only current-task advisory budget; no model call")
    expert.add_argument("--project", default=".")
    expert.add_argument("--run-id")
    preflight = sub.add_parser("preflight", help="inspect actual cwd and executable without starting a run or process")
    preflight.add_argument("--project", default=".")
    preflight.add_argument("--cwd")
    preflight.add_argument("--write-path", action="append")
    preflight.add_argument("--protected-path", action="append")
    preflight.add_argument("argv", nargs=argparse.REMAINDER)
    reuse = sub.add_parser("reuse-preflight", help="read-only strict evidence reuse diagnosis; no execution or writes")
    reuse.add_argument("--project", default=".")
    reuse.add_argument("--run-id", required=True)
    reuse.add_argument("--scope", nargs="+", required=True)
    reuse.add_argument("--coverage", nargs="+")
    reuse.add_argument("--cwd")
    reuse.add_argument("--intent", choices=sorted(C.INTENTS), default="test")
    reuse.add_argument("--impact-covered", action="store_true")
    reuse.add_argument("argv", nargs=argparse.REMAINDER)
    visual_status = sub.add_parser("visual-inspect", help="read-only screenshot/view binding status; never aesthetic PASS")
    visual_status.add_argument("--project", default=".")
    visual_status.add_argument("--run-id", required=True)
    visual_status.add_argument("--requirement", required=True)
    owned = {}
    for name in ("update", "check", "report", "finish", "plan", "plan-preflight", "plan-bind", "run", "qa", "verify", "wait", "continue", "task-start", "task-end", "allocation-decide", "allocation-review", "expert-prepare", "observe-begin", "observe-finish", "policy-ack", "visual-declare", "visual-begin", "visual-finish", "visual-view", "visual-review"):
        owned[name] = sub.add_parser(name)
        owned[name].add_argument("--project", default=".")
        owned[name].add_argument("--receipt", required=True, help="explicit receipt from this run's start; never use another task's")
    owned["policy-ack"].add_argument("--revision", required=True)
    owned["allocation-review"].add_argument("--change-id", required=True, help="stable id of a meaningful authorized scope change")
    owned["allocation-decide"].add_argument("--json", required=True, dest="allocation_payload",
                                           help="mode, technical reason and developer assignments; records a plan, never spawns")
    owned["expert-prepare"].add_argument("--json", required=True, dest="expert_payload",
                                       help="case_id, trigger, evidence refs, brief and optional local_fix_attempted; no model call")
    for action in ("visual-declare", "visual-begin", "visual-finish", "visual-view", "visual-review"):
        owned[action].add_argument("--name", required=True)
    owned["visual-declare"].add_argument("--json", required=True, dest="payload")
    for action in ("visual-begin", "visual-finish"):
        owned[action].add_argument("--requirement", required=True)
    owned["visual-begin"].add_argument("--format", choices=["jpeg", "png"], default="jpeg", dest="image_format")
    owned["visual-finish"].add_argument("--json", required=True, dest="payload", help="actually observed target/viewport/state/version")
    owned["visual-finish"].add_argument("--host-stopped", action="store_true")
    owned["visual-finish"].add_argument("--stop-evidence", required=True)
    owned["visual-view"].add_argument("--capture", required=True)
    owned["visual-view"].add_argument("--viewed", action="store_true", help="caller confirms actual image viewing; not native proof")
    for action in ("visual-view", "visual-review"):
        owned[action].add_argument("--reviewer-dispatch", help="actual accepted current-task tester dispatch for independent review")
    owned["visual-review"].add_argument("--capture", required=True)
    owned["visual-review"].add_argument("--view", required=True)
    owned["visual-review"].add_argument("--json", required=True, dest="payload", help="actual gate/findings and optional resolved IDs")
    for action in ("observe-begin", "observe-finish"):
        owned[action].add_argument("--id", required=True, dest="observation_id")
    owned["observe-begin"].add_argument("--json", required=True, dest="declaration")
    observe_finish = owned["observe-finish"]
    observe_finish.add_argument("--exit-code", required=True)
    observe_finish.add_argument("--log-path", required=True)
    observe_finish.add_argument("--host-stopped", action="store_true")
    observe_finish.add_argument("--stop-evidence", required=True)
    observe_finish.add_argument("--error")
    observe_finish.add_argument("--operation")
    observe_finish.add_argument("--failure-cause")
    observe_finish.add_argument("--recovery-of")
    update = owned["update"]
    update.add_argument("--stage")
    update.add_argument("--activity", choices=sorted(R.ACTIVITIES))
    update.add_argument("--next")
    for action in ("task-start", "task-end"):
        owned[action].add_argument("--task-id", required=True)
    owned["task-start"].add_argument("--review-risk", required=True, choices=["low", "medium", "high", "unknown"])
    owned["task-start"].add_argument("--review-reason", required=True)
    owned["task-start"].add_argument("--continuation-of", help="explicit prior task in this run")
    owned["task-start"].add_argument("--retrospective", action="store_true")
    owned["task-start"].add_argument("--independent-goal", action="store_true")
    owned["task-start"].add_argument("--goal", required=True)
    owned["task-start"].add_argument("--accept", action="append", required=True, dest="acceptance")
    owned["task-end"].add_argument("--outcome", choices=["execution_ended", "paused", "aborted"], default="execution_ended")
    owned["task-end"].add_argument("--report", action="append", dest="report_refs")
    for option in ("reason", "action", "verify"):
        owned["wait"].add_argument("--" + option, required=True, dest="wait_" + option)
    owned["continue"].add_argument("--evidence", required=True)
    check = owned["check"]
    check.add_argument("--name", required=True, help="unique attempt name, separate from stable coverage")
    check.add_argument("--scope", nargs="+", required=True)
    check.add_argument("--coverage", nargs="+", help="stable semantic categories across attempts; omit only when unknown")
    check.add_argument("--intent", choices=sorted(C.INTENTS), default="test")
    check.add_argument("--timeout", type=float)
    check.add_argument("--write-path", action="append")
    check.add_argument("--protected-path", action="append")
    check.add_argument("--impact-covered", action="store_true")
    check.add_argument("--cwd", help="actual package cwd, relative to project; scope remains project-relative")
    check.add_argument("argv", nargs=argparse.REMAINDER)
    report = owned["report"]
    report.add_argument("--kind", required=True)
    report.add_argument("--name", required=True)
    report.add_argument("--json", required=True, dest="payload")
    owned["plan"].add_argument("--name", required=True)
    owned["plan"].add_argument("--json", required=True, dest="payload", help="agent-maintained checks and required_checks")
    for action in ("run", "qa", "verify", "plan-preflight", "plan-bind"):
        if action != "verify":
            owned[action].add_argument("--plan", required=True)
        owned[action].add_argument("--name", required=True)
    owned["plan-bind"].add_argument("--id", required=True, dest="check_id")
    owned["plan-bind"].add_argument("--check", required=True, dest="check_ref")
    owned["run"].add_argument("--id", required=True, dest="check_id")
    owned["run"].add_argument("--impact-covered", action="store_true")
    owned["qa"].add_argument("--summary", default="")
    verify = owned["verify"]
    source = verify.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan", help="actual registered plan reference returned by plan/verify")
    source.add_argument("--json", dest="payload", help="inline checks and required_checks; saved as this attempt's plan")
    verify.add_argument("--summary", default="")
    verify.add_argument("--finish", action="store_true", help="explicitly complete after PASS using original completion guards")
    verify.add_argument("--stopped", action="store_true", help="only after host confirms all run executors stopped")
    verify.add_argument("--stop-evidence")
    verify.add_argument("--direct", action="store_true", help="with --finish only; confirm no actual role delegation occurred")
    finish = owned["finish"]
    finish.add_argument("--outcome", choices=sorted(R.FINAL), default="completed")
    finish.add_argument("--stopped", action="store_true", help="only after host confirms all run executors stopped")
    finish.add_argument("--stop-evidence", required=True)
    finish.add_argument("--direct", action="store_true", help="confirm no actual role delegation occurred")
    for command in (verify, finish):
        command.add_argument("--compact", action="store_true", help="short success response; full artifacts and error diagnostics remain unchanged")
    return cli


def dispatch(args):
    if args.action == "paths":
        return paths(args.project), 0
    if args.action in {"audit", "scan"}:
        import run_audit as A
        result = A.audit(args.project, args.run_id, live=args.live) if args.action == "audit" else A.scan(args.project, args.limit)
        return result, int(result["status"] == "error" and "live" not in result)
    if args.action == "preview":
        import audit_preview as V
        result = V.preview(args.project, args.run_id, live=args.live)
        failed = result["snapshot_status"] == "error" and result["source"] != "live"
        return V.render(result) if args.format == "text" else result, int(failed)
    if args.action == "scan-projects":
        import audit_projects as M
        result = M.scan(args.project, workspace=args.workspace, since=args.since,
                        limit=args.limit, live=args.live)
        return M.render(result) if args.format == "text" else result, int(result["status"] == "error")
    root = Path(args.project).resolve(strict=True)
    if args.action == "visual-inspect":
        import visual_evidence as V
        return V.inspect(root, args.run_id, args.requirement), 0
    if args.action == "progress":
        import run_progress as G
        value = G.snapshot(root, args.run_id, host_status=args.host_status, host_ended_at=args.host_ended_at)
        if args.after_milestone is not None:
            value = G.delta(value, args.after_milestone)
        return G.render(value) if args.format == "text" else value, 0
    if args.action == "policy":
        import run_policy
        return run_policy.inspect(root, R.status(root, args.run_id)["state"]), 0
    if args.action == "allocation-status":
        import work_allocation as W
        return W.summary(R.status(root, args.run_id)["state"] or {}), 0
    if args.action == "expert-status":
        import expert_advice as X
        return X.summary(R.status(root, args.run_id)["state"] or {}), 0
    if args.action == "reuse-preflight":
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        result = C.inspect_reuse(root, args.run_id, argv=argv, scope=args.scope,
                                 coverage=args.coverage, cwd=args.cwd, intent=args.intent,
                                 impact_covered=args.impact_covered)
        return result, 0 if result["reusable"] else 1
    if args.action == "task-status":
        import run_tasks as T
        return T.status(root, args.run_id), 0
    if args.action == "preflight":
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        result = C.inspect_runner(root, argv, cwd=args.cwd, write_paths=args.write_path,
                                  protected_paths=args.protected_path)
        return result, 0 if result["available"] and result["resources"]["safe_to_execute"] else 1
    if args.action == "status":
        import run_policy
        snapshot = R.status(root, args.run_id)
        result = {**_public(snapshot["state"]), "active": snapshot["active"] is not None,
                  "policy": run_policy.inspect(root, snapshot["state"]),
                  "unrecognized_active": snapshot["active"] == "unrecognized"}
        if args.recovery:
            import run_continuity as T
            result["recovery"] = T.recovery(root, args.run_id, _snapshot=snapshot)
            # Recovery has a bounded, credential-filtered progress projection.
            result.update({key: result["recovery"]["progress"].get(key)
                           for key in ("run_id", "status", "stage", "activity", "pending", "next")})
            result["pending"] = result["recovery"]["progress"]["pending"]["items"]
        if args.host_status != "unknown" or args.host_ended_at is not None:
            import run_continuity as T
            identity = args.run_id or result["run_id"]
            if identity is not None:
                result["observation"] = T.inspect(root, identity, host_status=args.host_status,
                                                  host_ended_at=args.host_ended_at)
        if args.recovery and R.status(root, args.run_id) != snapshot:
            raise ValueError("run changed during recovery snapshot; recheck status --recovery")
        return result, 0
    if args.action == "release-timing":
        import release_timing as T
        return T.summary(root, args.run_id, check_ref=args.check), 0
    if args.action == "start":
        initial = {"stage": args.stage}
        if args.delivery_from:
            initial["delivery_from"] = args.delivery_from
        state = R.start(root, initial, managed=True, review_risk=args.review_risk, review_reason=args.review_reason)
        # NamedTemporaryFile in _atomic gives 0600. No token is printed, passed
        # as argv, or inferred from a different task's active marker.
        receipt = E._local(root, f"tmp/pipeline/runs/{state['run_id']}/receipt.json")
        receipt.parent.mkdir(parents=True, exist_ok=True)
        R._atomic(receipt, {**{k: state[k] for k in ("schema_version", "run_id", "owner_token")}, "project_root": str(root)})
        import run_policy
        return {**_public(state), "receipt": str(receipt), "policy": run_policy.inspect(root, state),
                "next_action": "policy-ack --receipt <receipt> --revision <policy.revision>; caller acknowledgement only",
                "audit": _optional_audit(root, state["run_id"]), **paths()}, 0
    receipt = _receipt(root, args.receipt)
    run_id, owner = receipt["run_id"], receipt["owner_token"]
    if args.action == "expert-prepare":
        import expert_advice as X
        payload = json.loads(args.expert_payload)
        required = {"case_id", "trigger", "evidence", "brief"}
        if not isinstance(payload, dict) or not required <= payload.keys() or payload.keys() - (required | {"local_fix_attempted"}):
            raise ValueError("expert preparation requires case_id, trigger, evidence and brief")
        value = X.prepare(root, run_id, owner, **payload)
        return value, 0 if value["eligible"] else 1
    if args.action in {"allocation-review", "allocation-decide"}:
        import work_allocation as W
        if args.action == "allocation-review":
            return W.scope_change(root, run_id, owner, args.change_id), 0
        payload = json.loads(args.allocation_payload)
        if not isinstance(payload, dict) or set(payload) != {"mode", "reason", "assignments"}:
            raise ValueError("allocation decision requires mode, reason and assignments")
        return W.decide(root, run_id, owner, **payload), 0
    if args.action.startswith("visual-"):
        import visual_evidence as V
        if args.action == "visual-declare":
            payload = json.loads(args.payload)
            if (not isinstance(payload, dict) or set(payload) - {"scope", "target", "viewport", "state", "version", "review", "supersedes"}
                    or not {"scope", "target", "viewport", "state", "version"} <= payload.keys()):
                raise ValueError("visual-declare requires scope/target/viewport/state/version and optional review/supersedes")
            ref = V.declare(root, run_id, owner, args.name, **payload)
        elif args.action == "visual-begin":
            return V.begin(root, run_id, owner, args.requirement, args.name, image_format=args.image_format), 0
        elif args.action == "visual-finish":
            ref = V.finish(root, run_id, owner, args.requirement, args.name, observed=json.loads(args.payload),
                           host_stopped=args.host_stopped, stop_evidence=args.stop_evidence)
        elif args.action == "visual-view":
            ref = V.view(root, run_id, owner, args.capture, args.name, viewed=args.viewed, reviewer_dispatch=args.reviewer_dispatch)
        else:
            payload = json.loads(args.payload)
            if (not isinstance(payload, dict) or set(payload) - {"gate", "findings", "resolved"}
                    or not {"gate", "findings"} <= payload.keys()):
                raise ValueError("visual-review requires gate/findings and optional resolved IDs")
            ref = V.review(root, run_id, owner, args.capture, args.view, args.name,
                           reviewer_dispatch=args.reviewer_dispatch, **payload)
        return {"report": ref, "notice": "Visual evidence only; no aesthetic or business PASS."}, 0
    if args.action == "policy-ack":
        import run_policy
        return run_policy.acknowledge(root, run_id, owner, args.revision), 0
    if args.action in {"task-start", "task-end"}:
        import run_tasks as T
        if args.action == "task-start":
            item = T.start(root, run_id, owner, args.task_id, args.goal, args.acceptance, require_managed=True,
                           review_risk=args.review_risk, review_reason=args.review_reason,
                           continuation_of=args.continuation_of, retrospective=args.retrospective,
                           independent_goal=args.independent_goal)
        else:
            item = T.end(root, run_id, owner, args.task_id, outcome=args.outcome, report_refs=args.report_refs)
        return {"run_id": run_id, "task": item, "notice": T.NOTICE}, 0
    if args.action in {"wait", "continue"}:
        import run_continuity as T
        if args.action == "wait":
            state = T.wait_for_user(root, run_id, owner, reason=args.wait_reason,
                                    action=args.wait_action, verify=args.wait_verify)
        else:
            state = T.resume_work(root, run_id, owner, evidence=args.evidence)
        return {**_public(state), "handoff": {key: state["continuity_wait"].get(key)
                for key in ("status", "reason", "action", "verify")}}, 0
    if args.action in {"plan", "plan-preflight", "plan-bind", "run", "qa", "verify"}:
        import check_plan as P
        if args.action == "plan-preflight":
            result = P.preflight_plan(root, run_id, owner, args.plan, args.name)
            return result, 0 if result["ready"] else 1
        if args.action == "verify":
            if args.finish:
                current = R.preflight_finish(root, run_id, owner, executors_stopped=args.stopped,
                                             stop_evidence=args.stop_evidence)
                if args.direct:
                    D.validate_direct(current)
            elif args.direct or args.stopped or args.stop_evidence is not None:
                raise ValueError("verify stopping/direct options require explicit --finish")
            plan_ref = args.plan
            if args.payload is not None:
                payload = _plan_payload(args.payload)
                plan_ref = P.save_plan(root, run_id, owner, args.name, payload.get("checks"),
                                       payload.get("required_checks"))
            result = P.verify_plan(root, run_id, owner, plan_ref, args.name, summary=args.summary)
            if result["gate"] != "PASS":
                return result, 1
            if args.finish:
                completion, code = _finish(root, run_id, owner, stopped=args.stopped,
                                            stop_evidence=args.stop_evidence, direct=args.direct)
                return {**result, "completion": completion}, code
            return result, 0
        if args.action == "plan":
            payload = _plan_payload(args.payload)
            ref = P.save_plan(root, run_id, owner, args.name, payload.get("checks"), payload.get("required_checks"))
            result = {"run_id": run_id, "plan": ref}
            delta = P.review_delta(root, run_id, owner, ref)
            if delta is not None:
                result["review_delta"] = delta
            return result, 0
        if args.action == "plan-bind":
            return P.bind_check(root, run_id, owner, args.plan, args.check_id, args.name, args.check_ref), 0
        if args.action == "run":
            result = P.run_planned(root, run_id, owner, args.plan, args.check_id, args.name,
                                   impact_covered=args.impact_covered)
            return result, 0 if result["exit_code"] == 0 else 1
        R.update(root, run_id, owner, {"stage": "acceptance", "activity": "testing"})
        ref = P.qa_from_plan(root, run_id, owner, args.plan, args.name, summary=args.summary)
        payload = E.read_report(root, run_id, ref, kind="qa")["payload"]
        return {"run_id": run_id, "report": ref, **{k: payload[k] for k in
                ("gate", "required_checks", "missing_checks", "check_failures", "environment_diagnostics")}}, 0 if payload["gate"] == "PASS" else 1
    if args.action == "update":
        changes = {key: value for key in ("stage", "activity", "next") if (value := getattr(args, key)) is not None}
        if not changes:
            raise ValueError("update needs a changed stage, activity or next action")
        return _public(R.update(root, run_id, owner, changes)), 0
    if args.action in {"observe-begin", "observe-finish"}:
        import observed_checks as H
        if args.action == "observe-begin":
            declaration = json.loads(args.declaration)
            allowed = {"command", "scope", "environment", "coverage", "write_paths", "intent"}
            if (not isinstance(declaration, dict) or set(declaration) - allowed
                    or not {"command", "scope", "environment"} <= declaration.keys()):
                raise ValueError("observe-begin requires command/scope/environment and supported declaration fields only")
            return H.begin(root, run_id, owner, args.observation_id, **declaration, event_view=True), 0
        try:
            exit_code = int(args.exit_code)
        except ValueError:
            raise ValueError("observe-finish requires an integer exit code") from None
        if args.error not in {None, "command_unavailable", "timeout", "interrupted"}:
            raise ValueError("observe-finish requires a supported execution error")
        report = H.finish(root, run_id, owner, args.observation_id, exit_code=exit_code,
                          log_path=args.log_path, host_stopped=args.host_stopped,
                          stop_evidence=args.stop_evidence, error=args.error, operation=args.operation,
                          failure_cause=args.failure_cause, recovery_of=args.recovery_of)
        return {"observation_id": args.observation_id, "status": "completed", "report": report}, 0
    if args.action == "check":
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        result = C.execute_check(root, run_id, owner, args.name, argv=argv, scope=args.scope,
            coverage=args.coverage, intent=args.intent, timeout=args.timeout, write_paths=args.write_path,
            impact_covered=args.impact_covered, cwd=args.cwd, protected_paths=args.protected_path)
        return result, 0 if result["exit_code"] == 0 else 1
    if args.action == "report":
        submitted = json.loads(args.payload)
        relative = E.write_report(root, run_id, owner, args.kind, args.name, submitted)
        if args.kind == "qa" and "run_policy" in R.status(root, run_id)["state"]:
            judgment = E.read_report(root, run_id, relative, kind="qa")["payload"]
            result = {"run_id": run_id, "report": relative,
                      **{key: judgment[key] for key in ("gate", "required_checks", "missing_checks", "check_failures") if key in judgment}}
            requested_acceptance = submitted.get("gate") == "PASS" or (
                submitted.get("gate") not in {"CONCERNS", "FAIL"} and submitted.get("plan_ref") is not None)
            return result, int(requested_acceptance and judgment.get("gate") != "PASS")
        return {"run_id": run_id, "report": relative}, 0
    return _finish(root, run_id, owner, outcome=args.outcome, stopped=args.stopped,
                   stop_evidence=args.stop_evidence, direct=args.direct)


def _finish(root, run_id, owner, *, outcome="completed", stopped=False, stop_evidence=None, direct=False):
    """Shared original closeout path, including same-process verify completion."""
    R._stopped(stopped, stop_evidence)
    current = R.status(root, run_id)["state"]
    if outcome == "completed" and current.get("check_plans") and "run_policy" not in current:
        latest_qa = next((ref for ref in reversed(current.get("artifacts", []))
                          if "/artifacts/qa/" in ref), None)
        payload = E.read_report(root, run_id, latest_qa, kind="qa")["payload"] if latest_qa else None
        current_plan = next(ref for ref in reversed(current["artifacts"]) if ref in current["check_plans"])
        if (not isinstance(payload, dict) or payload.get("gate") != "PASS"
                or current["artifacts"].index(latest_qa) < current["artifacts"].index(current_plan)
                or payload.get("plan_ref", current_plan) != current_plan):
            return {**_public(current), "error": "qa_required_before_finish",
                    "message": "Record the current QA judgment before finishing this planned run; command success alone is not QA acceptance.",
                    "next_action": "qa --receipt <current receipt> --plan <current plan> --name <new QA attempt>",
                    "latest_qa": latest_qa, "active_preserved": True}, 1
        import check_plan as P
        validation = P.inspect_plan(root, run_id, owner, current_plan)
        if validation["gate"] != "PASS":
            return {**_public(current), "error": "qa_evidence_invalid_before_finish",
                    "message": "The accepted plan evidence is no longer current; revalidate before finishing.",
                    "latest_qa": latest_qa, "active_preserved": True,
                    "next_action": "Revalidate failed or missing checks and record the current QA judgment.",
                    **{key: validation[key] for key in ("missing_checks", "check_failures", "environment_diagnostics")}}, 1
    if direct:
        D.confirm_direct(root, run_id, owner)
    state = R.finish(root, run_id, owner, outcome,
                     executors_stopped=stopped, stop_evidence=stop_evidence)
    delivery = state.get("delivery_status", {})
    coverage = delivery.get("coverage", {})
    missing = coverage.get("missing")
    issues = {label: sorted({item["reason"] for item in coverage.get("unavailable", [])
                            if label in item.get("coverage", [])}) or ["no_valid_evidence"]
              for label in missing or []}
    result = {**_public(state), "delivery_sealed": delivery.get("sealed", False),
            "audit": _optional_audit(root, run_id),
            "delivery_reason": delivery.get("reason"), "delivery_detail": delivery.get("detail"),
            "coverage_complete": coverage.get("complete"), "coverage_missing": missing,
            "coverage_issues": issues}
    if delivery.get("visual"):
        result["visual"] = delivery["visual"]
    return result, 0


def _compact_response(action, value, code):
    """Project successful output only, after all original execution and writes."""
    if code != 0 or not isinstance(value, dict) or "error" in value:
        return value
    if "gate" in value and value["gate"] != "PASS":
        return value
    if action == "start":
        return {key: value[key] for key in
                ("run_id", "status", "stage", "activity", "receipt", "policy", "next_action")}
    if action == "finish":
        return {key: item for key, item in value.items() if key != "audit"}
    if action == "verify":
        result = {key: item for key, item in value.items() if key not in {"results", "skipped_checks", "completion"}}
        checks = value["results"]
        result["counts"] = {
            "executed": sum(not check["reused"] for check in checks),
            "reused": sum(bool(check["reused"]) for check in checks),
            "failed": sum(check["exit_code"] != 0 for check in checks),
            "skipped": len(value["skipped_checks"]),
        }
        if "completion" in value:
            result["completion"] = _compact_response("finish", value["completion"], code)
        return result
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        value, code = dispatch(args)
    except (OSError, ValueError, KeyError, TypeError) as error:
        value, code = {"error": type(error).__name__, "message": str(error)}, 2
    if getattr(args, "compact", False):
        value = _compact_response(args.action, value, code)
    print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
