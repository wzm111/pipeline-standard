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


def paths():
    skill = Path(__file__).resolve().parents[1]
    candidates = {"skill": skill / "SKILL.md", "runner": Path(__file__).resolve(),
                  "developer": skill.parent.parent / "agents/pipeline-developer.md"}
    return {"paths": {key: str(p) for key, p in candidates.items() if p.is_file()},
            "unavailable": [key for key, p in candidates.items() if not p.is_file()]}


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


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="action", required=True)
    sub.add_parser("paths", help="resolve installed skill, runner and developer paths without starting a run")
    status = sub.add_parser("status", help="compact read-only status, no credentials")
    status.add_argument("--project", default=".")
    status.add_argument("--run-id")
    status.add_argument("--recovery", action="store_true", help="bounded registered recovery pointers; no retry permission")
    status.add_argument("--host-status", choices=["unknown", "running", "idle"], default="unknown")
    status.add_argument("--host-ended-at", help="explicit native host idle boundary, never inferred from run age")
    task_status = sub.add_parser("task-status", help="read-only execution task records, not acceptance")
    task_status.add_argument("--project", default=".")
    task_status.add_argument("--run-id")
    release_timing = sub.add_parser("release-timing", help="read-only measured release steps, no extra deployment")
    release_timing.add_argument("--project", default=".")
    release_timing.add_argument("--run-id", required=True)
    release_timing.add_argument("--check", help="optional current-run monolithic release check reference")
    start = sub.add_parser("start", help="start after reading contract; retain the returned private receipt path")
    start.add_argument("--project", default=".")
    start.add_argument("--stage", default="execution")
    start.add_argument("--delivery-from", choices=["latest"])
    preflight = sub.add_parser("preflight", help="inspect actual cwd and executable without starting a run or process")
    preflight.add_argument("--project", default=".")
    preflight.add_argument("--cwd")
    preflight.add_argument("argv", nargs=argparse.REMAINDER)
    owned = {}
    for name in ("update", "check", "report", "finish", "plan", "run", "qa", "wait", "continue", "task-start", "task-end"):
        owned[name] = sub.add_parser(name)
        owned[name].add_argument("--project", default=".")
        owned[name].add_argument("--receipt", required=True, help="explicit receipt from this run's start; never use another task's")
    update = owned["update"]
    update.add_argument("--stage")
    update.add_argument("--activity", choices=sorted(R.ACTIVITIES))
    update.add_argument("--next")
    for action in ("task-start", "task-end"):
        owned[action].add_argument("--task-id", required=True)
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
    check.add_argument("--impact-covered", action="store_true")
    check.add_argument("--cwd", help="actual package cwd, relative to project; scope remains project-relative")
    check.add_argument("argv", nargs=argparse.REMAINDER)
    report = owned["report"]
    report.add_argument("--kind", required=True)
    report.add_argument("--name", required=True)
    report.add_argument("--json", required=True, dest="payload")
    owned["plan"].add_argument("--name", required=True)
    owned["plan"].add_argument("--json", required=True, dest="payload", help="agent-maintained checks and required_checks")
    for action in ("run", "qa"):
        owned[action].add_argument("--plan", required=True)
        owned[action].add_argument("--name", required=True)
    owned["run"].add_argument("--id", required=True, dest="check_id")
    owned["run"].add_argument("--impact-covered", action="store_true")
    owned["qa"].add_argument("--summary", default="")
    finish = owned["finish"]
    finish.add_argument("--outcome", choices=sorted(R.FINAL), default="completed")
    finish.add_argument("--stopped", action="store_true", help="only after host confirms all run executors stopped")
    finish.add_argument("--stop-evidence", required=True)
    finish.add_argument("--direct", action="store_true", help="confirm no actual role delegation occurred")
    return cli


def dispatch(args):
    if args.action == "paths":
        return paths(), 0
    root = Path(args.project).resolve(strict=True)
    if args.action == "task-status":
        import run_tasks as T
        return T.status(root, args.run_id), 0
    if args.action == "preflight":
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        result = C.inspect_runner(root, argv, cwd=args.cwd)
        return result, 0 if result["available"] else 1
    if args.action == "status":
        snapshot = R.status(root, args.run_id)
        result = {**_public(snapshot["state"]), "active": snapshot["active"] is not None,
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
        state = R.start(root, initial)
        # NamedTemporaryFile in _atomic gives 0600. No token is printed, passed
        # as argv, or inferred from a different task's active marker.
        receipt = E._local(root, f"tmp/pipeline/runs/{state['run_id']}/receipt.json")
        receipt.parent.mkdir(parents=True, exist_ok=True)
        R._atomic(receipt, {**{k: state[k] for k in ("schema_version", "run_id", "owner_token")}, "project_root": str(root)})
        return {**_public(state), "receipt": str(receipt), **paths()}, 0
    receipt = _receipt(root, args.receipt)
    run_id, owner = receipt["run_id"], receipt["owner_token"]
    if args.action in {"task-start", "task-end"}:
        import run_tasks as T
        if args.action == "task-start":
            item = T.start(root, run_id, owner, args.task_id, args.goal, args.acceptance)
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
    if args.action in {"plan", "run", "qa"}:
        import check_plan as P
        if args.action == "plan":
            payload = json.loads(args.payload)
            if not isinstance(payload, dict) or set(payload) - {"checks", "required_checks"}:
                raise ValueError("plan accepts checks and required_checks only")
            ref = P.save_plan(root, run_id, owner, args.name, payload.get("checks"), payload.get("required_checks"))
            return {"run_id": run_id, "plan": ref}, 0
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
    if args.action == "check":
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        result = C.execute_check(root, run_id, owner, args.name, argv=argv, scope=args.scope,
            coverage=args.coverage, intent=args.intent, timeout=args.timeout, write_paths=args.write_path,
            impact_covered=args.impact_covered, cwd=args.cwd)
        return result, 0 if result["exit_code"] == 0 else 1
    if args.action == "report":
        relative = E.write_report(root, run_id, owner, args.kind, args.name, json.loads(args.payload))
        return {"run_id": run_id, "report": relative}, 0
    R._stopped(args.stopped, args.stop_evidence)
    current = R.status(root, run_id)["state"]
    if args.outcome == "completed" and current.get("check_plans"):
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
    if args.direct:
        D.confirm_direct(root, run_id, owner)
    state = R.finish(root, run_id, owner, args.outcome,
                     executors_stopped=args.stopped, stop_evidence=args.stop_evidence)
    delivery = state.get("delivery_status", {})
    coverage = delivery.get("coverage", {})
    missing = coverage.get("missing")
    issues = {label: sorted({item["reason"] for item in coverage.get("unavailable", [])
                            if label in item.get("coverage", [])}) or ["no_valid_evidence"]
              for label in missing or []}
    return {**_public(state), "delivery_sealed": delivery.get("sealed", False),
            "delivery_reason": delivery.get("reason"), "delivery_detail": delivery.get("detail"),
            "coverage_complete": coverage.get("complete"), "coverage_missing": missing,
            "coverage_issues": issues}, 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        value, code = dispatch(args)
    except (OSError, ValueError, KeyError, TypeError) as error:
        value, code = {"error": type(error).__name__, "message": str(error)}, 2
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
