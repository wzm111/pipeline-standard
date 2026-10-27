"""Execute authorized foreground checks with timing, classification and evidence.

No shell expansion, model calls, git policy or QA gate decisions are implicit.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import time

import run_evidence as E
import run_state as R

INTENTS = {"test", "search", "locate", "reproduce", "check"}
RESULTS = {"success", "search_no_match", "path_lookup_failure", "reproduction_nonzero",
           "test_failure", "command_failure", "command_unavailable", "timeout", "interrupted"}
REVIEW_RESULTS = {"environment_failure", "fixture_failure", "regression_failure"}


def classify_result(exit_code, intent, error=None):
    if intent not in INTENTS or type(exit_code) is not int:
        raise ValueError("invalid command intent or exit code")
    if error in {"command_unavailable", "timeout", "interrupted"}:
        return error
    if error is not None:
        raise ValueError("unknown execution error")
    if exit_code == 0:
        return "success"
    if exit_code < 0:
        return "interrupted"
    if intent == "search" and exit_code == 1:
        return "search_no_match"
    return {"test": "test_failure", "locate": "path_lookup_failure",
            "reproduce": "reproduction_nonzero"}.get(intent, "command_failure")


def _environment(root, argv, child_env, context):
    # PATH entries (including the empty entry) are relative to the child's cwd.
    search_path = os.pathsep.join(str(root / part) if not Path(part).is_absolute() else part
                                  for part in child_env.get("PATH", os.defpath).split(os.pathsep))
    candidate = str(root / argv[0]) if os.sep in argv[0] and not Path(argv[0]).is_absolute() else argv[0]
    executable = shutil.which(candidate, path=search_path)
    try:
        identity = hashlib.sha256(Path(executable).read_bytes()).hexdigest() if executable else "unavailable"
    except OSError:
        identity = "unavailable"
    return {"process_env_sha256": hashlib.sha256(json.dumps(child_env, sort_keys=True).encode()).hexdigest(),
            "executable": str(Path(executable).resolve()) if executable else argv[0],
            "executable_sha256": identity,
            **{f"context:{k}": v for k, v in E._environment(context or {"scope": "local"}).items()}}


def _claim(root, run_id, owner, name, write_paths, *, testing=True):
    with R._write_guard(root) as base:
        path, state = R._load_owned(base, run_id, owner)
        if state["status"] != "running":
            raise ValueError("check requires an active owned run")
        claims = state.setdefault("running_checks", [])
        if any(c["name"] == name or E.write_paths_conflict(root, write_paths, c["write_paths"]) for c in claims):
            raise ValueError("check resources conflict; wait or declare verified isolated write paths")
        report = E._local(root, f"tmp/pipeline/runs/{run_id}/artifacts/check/{name}.json")
        if report.exists():
            raise FileExistsError("check report exists; choose a new round/name")
        if not claims:
            state["check_previous_progress"] = {k: state.get(k, "execution") for k in ("stage", "activity")}
            if testing and (state["stage"], state.get("activity")) != ("checking", "testing"):
                state.update(stage="checking", activity="testing")
                state["history"].append(R._event(state, "transition", R._now()))
        claims.append({"name": name, "write_paths": write_paths})
        state["updated_at"] = R._now()
        R._atomic(path, state)


def _release(root, run_id, owner, name):
    with R._write_guard(root) as base:
        path, state = R._load_owned(base, run_id, owner)
        state["running_checks"] = [c for c in state.get("running_checks", []) if c["name"] != name]
        if not state["running_checks"]:
            previous = state.pop("check_previous_progress", None)
            if previous and (state["stage"], state.get("activity")) == ("checking", "testing"):
                changed = any(state.get(k) != v for k, v in previous.items())
                state.update(previous)
                if changed:
                    state["history"].append(R._event(state, "transition", R._now()))
        state["updated_at"] = R._now()
        R._atomic(path, state)


def _stop(process):
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass
    process.wait()


def execute_check(project_root, run_id, owner_token, name, *, argv, scope, intent="test",
                  environment=None, write_paths=None, timeout=None, reuse_from=None, impact_covered=False):
    """One call handles a check; callers still authorize command and impact scope.

    Unknown writes serialize cooperating checks. Timeout ends our POSIX process
    group. Commands must stay in the foreground; this is not a service runner.
    """
    root = Path(project_root).resolve(strict=True)
    E._label(name)
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a or "\0" in a for a in argv):
        raise ValueError("argv must be a nonempty list of arguments")
    classify_result(0, intent)
    if timeout is not None and (type(timeout) not in (int, float) or not 0 < timeout < float("inf")):
        raise ValueError("timeout must be a finite positive duration")
    # Check declared paths before any process starts, even without competing checks.
    E.write_paths_conflict(root, write_paths, [])
    before = E.fingerprint(root, scope)
    child_env = {k: v for k, v in os.environ.items() if k != "PIPELINE_OWNER_TOKEN" and owner_token not in v}
    env = _environment(root, argv, child_env, environment)
    command = shlex.join(argv)
    if owner_token in command:
        raise ValueError("owner token cannot be passed to a command")
    if impact_covered is True and reuse_from is None:
        owned = R.status(root, run_id)["state"]
        R._owned(owned, run_id, owner_token)
        # Search only this run's registered evidence, including explicit imports.
        for candidate in reversed(owned["artifacts"]):
            if "/artifacts/check/" not in candidate:
                continue
            try:
                proof = E.read_report(root, run_id, candidate, kind="check")["payload"]
                if (proof.get("command") == command and proof.get("scope") == scope
                        and proof.get("intent") == intent and proof.get("environment") == env):
                    reuse_from = candidate
                    break
            except (OSError, ValueError, KeyError, TypeError):
                continue
    if reuse_from and impact_covered is True:
        try:
            old = E.read_report(root, run_id, reuse_from, kind="check")["payload"]
            reusable = (env["executable_sha256"] != "unavailable" and
                        old.get("scope") == scope and old.get("intent") == intent and
                        E.reusable_check(root, run_id, reuse_from, command=command, environment=env,
                                         impact_covered=True)["reusable"])
        except (OSError, ValueError, KeyError, TypeError):
            reusable = False
        if reusable:
            path = E.write_report(root, run_id, owner_token, "check_reuse", name, {"source": reuse_from})
            return {"report": reuse_from, "reuse_report": path, "reused": True,
                    "exit_code": 0, "result_class": "success", "seconds": 0.0}
    _claim(root, run_id, owner_token, name, write_paths, testing=intent not in {"search", "locate"})
    process, interrupted = None, None
    try:
        relative = f"tmp/pipeline/runs/{run_id}/artifacts/logs/{name}.log"
        log = E._local(root, relative)
        log.parent.mkdir(parents=True, exist_ok=True)
        started_at, start = R._now(), time.monotonic()
        error = None
        with log.open("x", encoding="utf-8") as stream:
            try:
                process = subprocess.Popen(argv, cwd=root, env=child_env, stdin=subprocess.DEVNULL,
                                           stdout=stream, stderr=subprocess.STDOUT, start_new_session=os.name == "posix")
                try:
                    exit_code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    _stop(process)
                    exit_code, error = process.returncode, "timeout"
                finally:
                    # A foreground check must not leave descendants holding its resources.
                    if process.poll() is not None:
                        _stop(process)
            except OSError as exc:
                stream.write(f"Launch failed: {type(exc).__name__}\n")
                exit_code, error = 127, "command_unavailable"
            except BaseException as exc:
                if process is not None:
                    _stop(process)
                exit_code, error, interrupted = -1, "interrupted", exc
        seconds, finished_at = time.monotonic() - start, R._now()
        try:
            stable = (env["executable_sha256"] != "unavailable" and E.fingerprint(root, scope) == before
                      and _environment(root, argv, child_env, environment) == env)
        except (OSError, ValueError):
            stable = False
        result_class = classify_result(exit_code, intent, error)
        path = E.write_report(root, run_id, owner_token, "check", name,
                              {"command": command, "argv": argv, "intent": intent, "result_class": result_class,
                               "scope": scope, "fingerprint": before, "environment": env,
                               "inputs_stable": stable, "exit_code": exit_code, "seconds": seconds,
                               "started_at": started_at, "finished_at": finished_at, "log_path": relative,
                               "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest()})
        if interrupted is not None:
            raise interrupted
        return {"report": path, "reused": False, "exit_code": exit_code, "result_class": result_class,
                "seconds": seconds, "inputs_stable": stable}
    finally:
        if process is not None and process.poll() is None:
            _stop(process)
        _release(root, run_id, owner_token, name)


def review_check(project_root, run_id, owner_token, name, source, result_class, reason):
    """Append a reasoned diagnosis without rewriting the observed exit or QA gate."""
    original = E.read_report(project_root, run_id, source, kind="check")["payload"]
    if (original.get("exit_code") == 0 or result_class not in REVIEW_RESULTS
            or not isinstance(reason, str) or not reason.strip()):
        raise ValueError("failure review requires a nonzero check, supported diagnosis and reason")
    return E.write_report(project_root, run_id, owner_token, "check_review", name,
                          {"source": source, "result_class": result_class, "reason": reason})


def check_summary(project_root, run_id=None):
    state = R.status(project_root, run_id)["state"]
    result = {"run_id": state["run_id"] if state else run_id, "executed": 0, "reused": 0,
              "by_result_class": {}, "by_raw_result_class": {}, "reviews": 0, "inherited": 0, "unconfirmed": 0}
    checks, reviews = {}, {}
    for path in state.get("artifacts", []) if state else []:
        if not any(f"/artifacts/{kind}/" in path for kind in ("check", "check_reuse", "check_review")):
            continue
        try:
            report = E.read_report(project_root, state["run_id"], path)
            if report["kind"] == "check_reuse":
                E.read_report(project_root, state["run_id"], report["payload"]["source"], kind="check")
                result["reused"] += 1
            elif report["kind"] == "check_review":
                payload = report["payload"]
                original = E.read_report(project_root, state["run_id"], payload["source"], kind="check")["payload"]
                if original.get("exit_code") == 0 or payload["result_class"] not in REVIEW_RESULTS or not payload.get("reason"):
                    raise ValueError("unconfirmed failure review")
                reviews[payload["source"]] = payload["result_class"]
                result["reviews"] += 1
            else:
                if report["payload"].get("inherited_from"):
                    E.validate_lineage(project_root, report["payload"])
                    result["inherited"] += 1
                    continue
                category = report["payload"].get("result_class", "unclassified")
                if category not in RESULTS:
                    category = "unclassified"
                checks[path] = category
        except (OSError, ValueError, KeyError, TypeError):
            result["unconfirmed"] += 1
    result["executed"] = len(checks)
    for path, raw in checks.items():
        category = reviews.get(path, raw)
        result["by_result_class"][category] = result["by_result_class"].get(category, 0) + 1
        result["by_raw_result_class"][raw] = result["by_raw_result_class"].get(raw, 0) + 1
    return result
