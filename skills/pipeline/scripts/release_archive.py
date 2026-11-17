"""Authenticate retained production results without invoking their commands."""
import hashlib
import json
from pathlib import Path
import re
import shlex

import check_plan as P
import delivery as D
import managed_qa as Q
import planned_release as L
import run_evidence as E
import run_state as R


KIND = "retained_release"


def _test_counts(log, groups):
    if not isinstance(groups, dict) or not groups:
        raise ValueError("declare the test file/count requirements for each category")
    log = re.sub(r"\x1b\[[0-9;]*m", "", log)
    lines = log.splitlines()
    starts = [index for index, line in enumerate(lines) if line == "[build-stage] source-gates started"]
    ends = [index for index, line in enumerate(lines)
            if re.fullmatch(r"\[build-stage\] source-gates [\d.]+s exit=\d+", line)]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0] or not lines[ends[0]].endswith(" exit=0"):
        raise ValueError("one successful AWS source gate is required")
    stage = "\n".join(lines[starts[0] + 1:ends[0]])
    observed = {}
    for name, number in re.findall(r"^ *✓[^\n]*? (tests/[^\s]+) +\((\d+) tests?\)", stage, re.M):
        observed.setdefault(name, []).append(int(number))
    files = {}
    for label, requirements in groups.items():
        E._label(label)
        if not isinstance(requirements, dict) or not requirements:
            raise ValueError("each test category requires a nonempty file/count mapping")
        for name, count in requirements.items():
            if (not re.fullmatch(r"tests/(?:[\w.-]+/)*[\w.-]+\.test\.[cm]?[jt]sx?", name)
                    or ".." in Path(name).parts or type(count) is not int or count <= 0
                    or observed.get(name) != [count]):
                raise ValueError("original successful test file/count missing or ambiguous")
            files[name] = count
    return files


def _retrospective_audit(state, report):
    when = R._timestamp(report["recorded_at"])
    return next((task["task_id"] for task in state.get("tasks", [])
                 if task.get("ended_at")
                 and state.get("review_decisions", {}).get(task["task_id"], {}).get("retrospective")
                 and R._timestamp(task["started_at"]) <= when <= R._timestamp(task["ended_at"])), None)


def _revalidate_runner(root, state, ref, proof):
    identity = proof["environment"]
    check = {"argv": proof["argv"], "cwd": proof["cwd"],
             "environment": {key[8:]: value for key, value in identity.items() if key.startswith("context:")}}
    env = P._environment(root, state["owner_token"], check)
    env["process_env_sha256"] = identity["process_env_sha256"]
    verdict = E.reusable_check(root, state["run_id"], ref, command=shlex.join(check["argv"]),
                               environment=env, coverage=proof["coverage"], impact_covered=True)
    if not verdict["reusable"]:
        raise ValueError("original runner evidence is invalid: " + verdict["reason"])


def _corrected_audit(root, state, snapshot, ref, original, label, markers):
    before = original["payload"]
    if before.get("intent") != "check" or before.get("result_class") != "success":
        return False
    task = _retrospective_audit(state, original)
    replacement = snapshot["observations"].get(P._identity(before))
    if not task or not replacement or snapshot["positions"][replacement] <= snapshot["positions"][ref]:
        return False
    corrected = Q._read(root, state, replacement, "check")
    after = corrected["payload"]
    if (_retrospective_audit(state, corrected) != task or after.get("result_class") != "success"
            or "argv" not in after or before.get("argv") != after["argv"]
            or after.get("log_sha256") != before.get("log_sha256")
            or set(after["coverage"]) != {"historical_" + item for item in before["coverage"]}
            or "historical_" + label not in after["coverage"]):
        return False
    L.executed(state, ref, original)
    L.executed(state, replacement, corrected)
    _revalidate_runner(root, state, replacement, after)
    L.read_log(root, before)
    prefixes = tuple(markers[key][0] for key in ("version_marker", "target_marker"))
    return not any(line.startswith(prefixes) for line in L.read_log(root, after).splitlines())


def latest_source(root, state, snapshot, label, markers):
    """A relabeled, identical audit must not outrank an actual release event."""
    for ref in reversed(state["artifacts"]):
        if "/artifacts/check/" not in ref:
            continue
        record = Q._read(root, state, ref, "check")
        proof = record["payload"]
        if (label not in proof.get("coverage", []) or proof.get("inherited_from")
                or proof.get("observation_kind") == KIND):
            continue
        if not _corrected_audit(root, state, snapshot, ref, record, label, markers):
            return ref
    return None


def _paired(root, state, snapshot, declaration, version, ended):
    if (not isinstance(declaration, dict)
            or set(declaration) != {"report", "version_marker", "paired_marker", "paired_version"}):
        raise ValueError("paired readback requires an original report and exact version markers")
    ref = declaration["report"]["path"]
    record = Q._read(root, state, ref, "check")
    proof = D._read_reference(root, declaration["report"], "check")
    if (declaration["report"]["run_id"] != state["run_id"] or proof != record["payload"]
            or proof.get("observation_kind") or proof.get("inherited_from")
            or proof.get("result_class") != "success" or proof.get("inputs_stable") is not True
            or "argv" not in proof or snapshot["observations"].get(P._identity(proof)) != ref
            or L.executed(state, ref, record)[0] <= ended):
        raise ValueError("paired readback requires a latest original post-activation execution")
    markers = {"version_marker": declaration["version_marker"], "target_marker": declaration["paired_marker"]}
    if any(latest_source(root, state, snapshot, label, markers) != ref for label in proof["coverage"]):
        raise ValueError("newer paired readback evidence requires a new binding")
    _revalidate_runner(root, state, ref, proof)
    log = L.read_log(root, proof)
    vline, value = L._identity_marker(log, declaration["version_marker"], target=False)
    pline, paired = L._identity_marker(log, declaration["paired_marker"], target=False)
    if (vline == pline or value != version or paired != declaration["paired_version"]
            or any(declaration[key][1] for key in ("version_marker", "paired_marker"))):
        raise ValueError("paired readback version does not match retained release")
    return proof


def proof(root, state, declaration):
    if (not isinstance(declaration, dict)
            or not {"binding", "test_groups"} <= declaration.keys()
            or declaration.keys() - {"binding", "test_groups", "paired_readback"}):
        raise ValueError("retained release requires an original release binding and test groups")
    run_id = state["run_id"]
    snapshot = P._snapshot(root, run_id, state)
    ref = declaration["binding"]
    payload = Q._read(root, state, ref["path"], "planned_release")["payload"]
    if (ref["run_id"] != run_id or D._read_reference(root, ref, "planned_release") != payload
            or snapshot["release_bindings"].get((payload["plan"]["path"], payload["activation"]["id"]))
            != {"path": ref["path"], "sha256": ref["sha256"]}):
        raise ValueError("retained release must use the latest registered release binding")
    (version, target), (activation, activation_log, ended), live = L.validated_pair(
        root, state, payload, snapshot, retained=True)
    tests = _test_counts(activation_log, declaration["test_groups"])
    labels = set(activation["coverage"]) | set(live["coverage"])
    sources = [activation, live]
    if "paired_readback" in declaration:
        extra = _paired(root, state, snapshot, declaration["paired_readback"], version, ended)
        labels.update(extra["coverage"])
        sources.append(extra)
    groups = declaration["test_groups"]
    if labels & groups.keys():
        raise ValueError("test results cannot supply release or readback coverage")
    scope = sorted({path for item in sources for path in (*item["scope"], item["log_path"])}
                   | {ref["path"]})
    env = {"release_archive_sha256": hashlib.sha256(
        json.dumps(declaration, sort_keys=True).encode()).hexdigest()}
    return {"observation_kind": KIND, "declaration": declaration,
            "command": f"authenticate retained release {target} {version}",
            "intent": "check", "result_class": "success", "exit_code": 0,
            "scope": scope, "coverage": sorted(labels | groups.keys()),
            "environment": env, "fingerprint": E.fingerprint(root, scope),
            "test_files": tests, "version": version, "target": target,
            "inputs_stable": True}


def record(root, run_id, owner, name, spec):
    import review_control

    state = P._owned(root, run_id, owner)
    if not review_control.decision(state).get("retrospective") or not review_control.required(state):
        raise ValueError("retained release requires a retrospective task with independent review")
    if not isinstance(spec, dict) or "binding" not in spec or isinstance(spec["binding"], dict):
        raise ValueError("binding must name an original registered release binding")
    declaration = {**spec, "binding": D._reference(root, run_id, spec["binding"])}
    if "paired_readback" in spec:
        paired = spec["paired_readback"]
        declaration["paired_readback"] = {**paired, "report": D._reference(root, run_id, paired["report"])}
    result = proof(root, state, declaration)
    ref = E.write_report(root, run_id, owner, "check", name, result)
    return {"report": ref, "version": result["version"], "target": result["target"],
            "test_files": result["test_files"], "coverage": result["coverage"],
            "check": {"observed_ref": ref,
                      **{key: result[key] for key in ("scope", "coverage", "environment", "intent")}}}


def validate(root, state, report):
    if proof(root, state, report["declaration"]) != report:
        raise ValueError("retained release evidence changed")
