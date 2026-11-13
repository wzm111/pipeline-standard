"""Read-only cross-project index over bounded local audit metadata."""
import os
from pathlib import Path

import audit_preview as V
import run_audit as A
import run_state as R

MAX_PROJECTS = 20
MAX_WORKSPACE_ENTRIES = 200
MAX_RECENT_ANOMALIES = 3


def _recent_anomalies(tasks):
    findings = []
    for task in reversed(tasks):
        host = task.get("host_diagnostics") or {}
        checks = {key: count for key, count in task["by_result_class"].items()
                  if count and key not in {"success", "search_no_match", "reproduction_nonzero", "unclassified"}}
        qa = {gate: task["qa_gates"][gate] for gate in ("FAIL", "CONCERNS") if task["qa_gates"].get(gate)}
        causes = host.get("failure_causes") or {}
        if checks or qa or causes:
            findings.append({"task_index": task["index"], "check_failures": checks,
                             "qa_findings": qa, "host_failure_causes": causes})
    return findings[:MAX_RECENT_ANOMALIES], len(findings) > MAX_RECENT_ANOMALIES


def _roots(projects, workspace):
    if workspace is None:
        if not projects or len(projects) > MAX_PROJECTS:
            raise ValueError("invalid project count")
        return list(dict.fromkeys(Path(project).resolve() for project in projects)), False
    roots, truncated = [], False
    with os.scandir(Path(workspace).resolve(strict=True)) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_WORKSPACE_ENTRIES:
                truncated = True
                break
            if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False):
                root = Path(entry.path)
                try:
                    if A._local(root, "tmp/pipeline/runs").is_dir():
                        roots.append(root)
                except (OSError, A.AuditError):
                    continue
    roots.sort()
    return roots[:MAX_PROJECTS], truncated or len(roots) > MAX_PROJECTS


def _row(root, record, document):
    audit = V._dict(document)
    timing = V._dict(audit.get("latency"))
    checks = V._dict(audit.get("checks"))
    tasks = audit.get("task_details") or []
    anomalies, anomalies_truncated = _recent_anomalies(tasks)
    detail = V._dict(tasks[-1]) if tasks else {}
    sample = next((item for item in timing.get("samples", [])
                   if item["index"] == detail.get("index")), {})
    run_id = record.get("run_id")
    snapshot_at = A._stamp(audit.get("snapshot_at"))
    handoff = V._dict(detail.get("qa_handoff"))
    return {
        "project": root.name, "run_id": run_id, "snapshot_status": record["status"],
        "snapshot_error": record.get("error"),
        "snapshot_at": snapshot_at, "run_status": V._enum(audit.get("status"), R.FINAL | {"running"}),
        "audit_ref": f"tmp/pipeline/runs/{run_id}/audit.json" if "audit" in record else None,
        "checks": V._counts(checks, ("executed", "reused", "inherited", "host_observation_records")),
        "result_classes": {key: value for key, value in
                           V._counts(checks.get("by_result_class"), sorted(A.RESULTS)).items() if value},
        "compliance": audit.get("compliance"),
        "adoption": V.adoption_summary(audit.get("adoption")),
        "recent_anomalies": anomalies, "recent_anomalies_truncated": anomalies_truncated,
        "latest_task": None if not detail else {
            "index": detail["index"],
            **V._counts(detail, ("executed", "reused", "inherited", "qa_pass_before_tester_return")),
            "reuse_reasons": detail["reuse_reasons"],
            "environment_mismatch_records": V._number(sample.get("environment_mismatch_records"), count=True),
            "wall_seconds": V._number(sample.get("wall_seconds")),
            "check_union_seconds": V._number(sample.get("check_union_seconds")),
            "release_plan_before_checks": detail["release_plan_before_checks"],
            "release_closeout": detail["release_closeout"],
            "release_command_seconds": detail.get("release_command_seconds"),
            "release_reported_stage_seconds": detail.get("release_reported_stage_seconds"),
            "qa_gates": detail["qa_gates"],
            "acceptance_gates": handoff.get("acceptance_gates"),
            "host_diagnostics": detail.get("host_diagnostics"),
        },
    }


def scan(projects=None, *, workspace=None, since=None, limit=5, live=False):
    try:
        cutoff = R._timestamp(since) if since is not None else None
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("invalid scan limit")
        roots, truncated = _roots(projects, workspace)
    except (OSError, RuntimeError, TypeError, ValueError):
        return {"status": "error", "error": "invalid_cross_project_scan"}
    rows, projects_checked = [], []
    for root in roots:
        result = A.scan(root, limit, live=live)
        candidates = []
        for record in result.get("runs", []):
            document = record.get("live") if live else record.get("audit")
            candidates.append(_row(root, record, document))
        dates = [row["snapshot_at"] for row in candidates if row["snapshot_at"] is not None]
        # Keep undated rows visible so a missing snapshot cannot silently vanish.
        visible = [row for row in candidates if cutoff is None or row["snapshot_at"] is None
                   or R._timestamp(row["snapshot_at"]) >= cutoff]
        rows.extend(visible)
        # A filtered stale snapshot can hide a run whose current state is newer.
        filtered_noncurrent = (sum(row["snapshot_status"] != "current" for row in candidates)
                               - sum(row["snapshot_status"] != "current" for row in visible))
        projects_checked.append({
            "project": root.name, "status": result["status"], "error": result.get("error"),
            "truncated": result.get("truncated", False),
            "truncation_reasons": result.get("truncation_reasons", []),
            "candidates_checked": len(candidates), "rows_returned": len(visible),
            "state_files_found": result.get("state_files_found"),
            "state_enumeration_complete": result.get("state_enumeration_complete"),
            "newest_state_mtime": result.get("newest_state_mtime"),
            "undated_candidates": len(candidates) - len(dates),
            "filtered_noncurrent_candidates": filtered_noncurrent,
            "oldest_snapshot_at": min(dates, key=R._timestamp, default=None),
            "newest_snapshot_at": max(dates, key=R._timestamp, default=None),
        })
    rows.sort(key=lambda row: (R._timestamp(row["snapshot_at"]).timestamp() if row["snapshot_at"] else float("-inf"),
                               row["project"], row["run_id"] or ""), reverse=True)
    return {"status": "partial" if any(p["status"] != "ok" or p["truncated"] or p["filtered_noncurrent_candidates"]
                                      for p in projects_checked)
            or truncated or any(row["snapshot_status"] != "current" for row in rows) else "ok",
            "source": "live" if live else "stored", "since": since, "limit_per_project": limit,
            "workspace_truncated": truncated, "projects": projects_checked, "runs": rows}


def render(result):
    if result["status"] == "error":
        return result["error"]

    def seconds(value):
        return "未知" if value is None else f"{value:.1f} 秒"

    lines = [f"跨项目审计 · {result['source']} · {result['status']} · {len(result['runs'])} 条"]
    if result["since"] is not None:
        undated = sum(row["snapshot_at"] is None for row in result["runs"])
        lines.append(f"日期筛选起点 {result['since']} · 命中 {len(result['runs']) - undated} 条 · "
                     f"另保留无日期诊断 {undated} 条")
    for row in result["runs"]:
        lines.append(f"{row['project']}  {row['snapshot_at'] or '日期未知'}  "
                     f"{row['run_id'] or 'run 未知'}  {row['snapshot_status']}")
        if row["snapshot_error"]:
            lines.append(f"  快照读取异常 {row['snapshot_error']}")
        adoption = row["adoption"]
        states = {True: "是", False: "否", None: "未知"}
        lines.append(f"  规则采用 {adoption['mode']} · 当前版本 {states[adoption['current']]} · "
                     f"调用方确认 {states[adoption['caller_acknowledged']]} · "
                     f"规则过期 {states[adoption['stale']]}")
        compliance = row.get("compliance")
        if compliance is None:
            lines.append("  发布阻断及进度审计：未知")
        else:
            lines.append(f"  发布阻断登记 {compliance['release_blockers_recorded']} 项 · "
                         f"待解除 {compliance['release_blockers_pending']} 项 · "
                         f"进度缺证据 {', '.join(compliance['feedback_missing']) or '无'} · "
                         f"格式待修正 {', '.join(compliance['feedback_invalid']) or '无'}")
        for finding in row["recent_anomalies"]:
            groups = []
            for field, label in (("check_failures", "检查"), ("qa_findings", "QA"),
                                 ("host_failure_causes", "宿主失败原因")):
                if finding[field]:
                    counts = "，".join(f"{key}={count}" for key, count in sorted(finding[field].items()))
                    groups.append(f"{label} {counts}")
            lines.append(f"  任务 {finding['task_index']} 异常记录 · " + " · ".join(groups))
        if row["recent_anomalies_truncated"]:
            lines.append("  较早异常已省略")
        task = row["latest_task"]
        if task is None:
            continue
        lines.append(f"  任务 {task['index']} · 检查 {seconds(task['check_union_seconds'])} · "
                     f"实跑 {task['executed']} · 复用 {task['reused']} · 继承 {task['inherited']}")
        if task["environment_mismatch_records"]:
            lines.append(f"  有环境差异记录的检查 {task['environment_mismatch_records']} 项")
        if task["release_plan_before_checks"] is not None or task["release_closeout"] is not None:
            states = {True: "是", False: "否", None: "未知"}
            lines.append(f"  发布计划前置 {states[task['release_plan_before_checks']]} · "
                         f"已登记发布收口 {states[task['release_closeout']]}")
        for key, label in (("reuse_reasons", "复用判定"), ("release_command_seconds", "发布命令耗时（秒）"),
                           ("release_reported_stage_seconds", "脚本报告的发布阶段耗时（秒）"),
                           ("qa_gates", "QA 登记"), ("acceptance_gates", "最终验收登记")):
            if task.get(key):
                values = "，".join(f"{name}={value:g}" for name, value in sorted(task[key].items()))
                lines.append(f"  {label} {values}")
        host = task.get("host_diagnostics") or {}
        if host.get("failure_causes") or host.get("recovery_successes") or host.get("recovery_failures"):
            causes = ", ".join(f"{name}={count}" for name, count in sorted(host["failure_causes"].items()))
            lines.append(f"  宿主失败原因 {causes or '无登记'} · 关联恢复成功 {host['recovery_successes']} · "
                         f"关联恢复失败 {host['recovery_failures']}")
    labels = {"directory_entry_limit": "目录扫描上限", "candidate_limit": "每项目候选上限"}
    for project in result["projects"]:
        span = "日期未知"
        if project["oldest_snapshot_at"]:
            span = f"{project['oldest_snapshot_at']} 至 {project['newest_snapshot_at']}"
        line = (f"{project['project']}  已查 {project['candidates_checked']} 个候选 · "
                f"返回 {project['rows_returned']} 条 · {span} · 无日期 {project['undated_candidates']} 条")
        if project["state_enumeration_complete"] is not None:
            enumeration = "完整" if project["state_enumeration_complete"] else "截断"
            mtime = project["newest_state_mtime"] or "无"
            line += f" · 枚举{enumeration} · 状态文件 {project['state_files_found']} 个 · 观察到的最新修改 {mtime}"
        if project["truncated"]:
            reasons = "、".join(labels[reason] for reason in project["truncation_reasons"])
            line += f" · 候选已截断（{reasons}）"
        if project["filtered_noncurrent_candidates"]:
            line += f" · 日期筛除的非当前快照 {project['filtered_noncurrent_candidates']} 条"
        if project["error"]:
            line += f" · {project['error']}"
        lines.append(line)
    if result["workspace_truncated"]:
        lines.append("工作区扫描已截断")
    return "\n".join(lines)
