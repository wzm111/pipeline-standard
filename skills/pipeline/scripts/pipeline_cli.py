"""Compact direct-workflow entrypoint. Receipts are private ownership credentials."""
import argparse
import json
from pathlib import Path
import sys

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
    start = sub.add_parser("start", help="start after reading contract; retain the returned private receipt path")
    start.add_argument("--project", default=".")
    start.add_argument("--stage", default="execution")
    start.add_argument("--delivery-from", choices=["latest"])
    owned = {}
    for name in ("update", "check", "report", "finish"):
        owned[name] = sub.add_parser(name)
        owned[name].add_argument("--project", default=".")
        owned[name].add_argument("--receipt", required=True, help="explicit receipt from this run's start; never use another task's")
    update = owned["update"]
    update.add_argument("--stage")
    update.add_argument("--activity", choices=sorted(R.ACTIVITIES))
    update.add_argument("--next")
    check = owned["check"]
    check.add_argument("--name", required=True, help="unique attempt name, separate from stable coverage")
    check.add_argument("--scope", nargs="+", required=True)
    check.add_argument("--coverage", nargs="+", help="stable semantic categories across attempts; omit only when unknown")
    check.add_argument("--intent", choices=sorted(C.INTENTS), default="test")
    check.add_argument("--timeout", type=float)
    check.add_argument("--write-path", action="append")
    check.add_argument("--impact-covered", action="store_true")
    check.add_argument("argv", nargs=argparse.REMAINDER)
    report = owned["report"]
    report.add_argument("--kind", required=True)
    report.add_argument("--name", required=True)
    report.add_argument("--json", required=True, dest="payload")
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
    if args.action == "status":
        snapshot = R.status(root, args.run_id)
        return {**_public(snapshot["state"]), "active": snapshot["active"] is not None,
                "unrecognized_active": snapshot["active"] == "unrecognized"}, 0
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
    if args.action == "update":
        changes = {key: value for key in ("stage", "activity", "next") if (value := getattr(args, key)) is not None}
        if not changes:
            raise ValueError("update needs a changed stage, activity or next action")
        return _public(R.update(root, run_id, owner, changes)), 0
    if args.action == "check":
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        result = C.execute_check(root, run_id, owner, args.name, argv=argv, scope=args.scope,
            coverage=args.coverage, intent=args.intent, timeout=args.timeout, write_paths=args.write_path,
            impact_covered=args.impact_covered)
        return result, 0 if result["exit_code"] == 0 else 1
    if args.action == "report":
        relative = E.write_report(root, run_id, owner, args.kind, args.name, json.loads(args.payload))
        return {"run_id": run_id, "report": relative}, 0
    R._stopped(args.stopped, args.stop_evidence)
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
