"""Evidence fingerprints and paired workflow statistics; no model calls."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics

try:
    from input_fingerprint import fingerprint as _input_fingerprint
except ModuleNotFoundError as error:
    # Existing consumers load this file by absolute importlib spec, without
    # putting the sibling scripts directory on sys.path.
    if error.name != "input_fingerprint":
        raise
    import importlib.util
    _spec = importlib.util.spec_from_file_location("pipeline_input_fingerprint", Path(__file__).with_name("input_fingerprint.py"))
    _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    _input_fingerprint = _module.fingerprint


def fingerprint(root, paths, environment):
    """Hash explicit inputs (directories include names/additions/deletions).

    environment must describe stable tool/config/dependency/service inputs.
    A caller cannot cache mutable external state without a stable snapshot.
    """
    if not environment:
        raise ValueError("environment identity is required")
    return _input_fingerprint(root, paths, environment)


def reusable(evidence, current_fingerprint, command, environment_stable):
    valid = bool(environment_stable and evidence.get("exit_code") == 0
                and type(evidence.get("exit_code")) is int
                and evidence.get("fingerprint") == current_fingerprint
                and evidence.get("command") == command
                and evidence.get("finished_at") and evidence.get("output_path")
                and Path(evidence["output_path"]).is_file())
    if not valid:
        return False
    try:
        return hashlib.sha256(Path(evidence["output_path"]).read_bytes()).hexdigest() == evidence.get("output_sha256")
    except OSError:
        return False


def interval_seconds(intervals):
    """Union overlapping intervals so parallel work is never double counted."""
    merged = []
    for start, end in sorted(intervals):
        if not all(type(x) in (int, float) and math.isfinite(x) for x in (start, end)) or end < start:
            raise ValueError("invalid interval")
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return sum(end - start for start, end in merged)


def _number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"invalid {label}")
    return value


def paired_summary(records):
    """Use matched A/B repeats; expose exclusions and never infer missing tokens."""
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        raise ValueError("records must be an array of objects")
    slots, excluded, separate = {}, [], []
    identity = ("base_commit", "model", "effort", "runtime", "environment_id", "acceptance_id")
    for record in records:
        if record.get("arm") not in ("A", "B", "C") or not record.get("task_id"):
            raise ValueError("invalid task/arm")
        repeat = record.get("repeat")
        if type(repeat) is not int or repeat < 1:
            raise ValueError("repeat must be a positive integer")
        if record["arm"] == "C":
            separate.append({"task_id": record["task_id"], "repeat": repeat, "reason": "mixed-model C is separate"})
            continue
        key = (record["task_id"], repeat)
        group = slots.setdefault(key, {})
        if record["arm"] in group:
            raise ValueError("duplicate task/arm/repeat")
        group[record["arm"]] = record
    by_task, tokens, matched = {}, [], []
    for (task, repeat), pair in sorted(slots.items()):
        reason = None
        if set(pair) != {"A", "B"}:
            reason = "unpaired"
        elif any(r.get("status") != "passed" or r.get("acceptance_passed") is not True for r in pair.values()):
            reason = "failure, blocked or acceptance not passed"
        elif any(not isinstance(pair["A"].get(k), str) or pair["A"][k].strip().lower() in ("", "unknown", "unverified")
                 or pair["A"].get(k) != pair["B"].get(k) for k in identity):
            reason = "identity mismatch or missing"
        if reason:
            excluded.append({"task_id": task, "repeat": repeat, "reason": reason})
            continue
        elapsed = {}
        for arm, record in pair.items():
            wall = _number(record.get("wall_seconds"), "wall_seconds")
            wait = _number(record.get("human_wait_seconds"), "human_wait_seconds")
            if wait >= wall:
                raise ValueError("execution time must be positive")
            elapsed[arm] = wall - wait
        ratio = elapsed["B"] / elapsed["A"]
        by_task.setdefault(task, []).append(ratio)
        matched.append({"task_id": task, "repeat": repeat, "ratio": ratio})
        ta, tb = pair["A"].get("token_usage"), pair["B"].get("token_usage")
        if ta is not None and tb is not None:
            _number(ta, "token_usage")
            _number(tb, "token_usage")
            if ta > 0:
                tokens.append(tb / ta)
    values = sorted(statistics.median(v) for v in by_task.values())
    median = statistics.median(values) if values else None
    p80 = values[math.ceil(len(values) * .8) - 1] if values else None
    sufficient = len(by_task) >= 12 and all(len(v) >= 2 for v in by_task.values())
    return {"task_count": len(by_task), "matched_pairs": matched, "excluded": excluded, "separate": separate,
            "median_ratio": median, "p80_ratio": p80, "sample_sufficient": sufficient,
            "performance_gate": (median <= 1.3 and p80 <= 1.5 and not excluded) if sufficient else None,
            "token_pairs": len(tokens), "token_coverage_complete": bool(matched) and len(tokens) == len(matched),
            "token_median_ratio": statistics.median(tokens) if tokens else None,
            "label": "publishable_sample" if sufficient and not excluded else "exploratory"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", type=Path, help="JSON array of real benchmark records")
    args = parser.parse_args()
    try:
        result = paired_summary(json.loads(args.records.read_text()))
    except (ValueError, OSError, TypeError) as error:
        parser.exit(1, f"统计失败：{error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
