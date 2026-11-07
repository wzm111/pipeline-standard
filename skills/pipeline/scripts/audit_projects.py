"""Read-only cross-project index over bounded local audit metadata."""
import os
from pathlib import Path

import audit_preview as V
import run_audit as A
import run_state as R

MAX_PROJECTS = 20
MAX_WORKSPACE_ENTRIES = 200


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
    detail = V._dict(tasks[-1]) if tasks else {}
    sample = next((item for item in timing.get("samples", [])
                   if item["index"] == detail.get("index")), {})
    run_id = record.get("run_id")
    snapshot_at = A._stamp(audit.get("snapshot_at"))
    handoff = V._dict(detail.get("qa_handoff"))
    return {
        "project": root.name, "run_id": run_id, "snapshot_status": record["status"],
        "snapshot_at": snapshot_at, "run_status": V._enum(audit.get("status"), R.FINAL | {"running"}),
        "audit_ref": f"tmp/pipeline/runs/{run_id}/audit.json" if "audit" in record else None,
        "checks": V._counts(checks, ("executed", "reused", "inherited", "host_observation_records")),
        "result_classes": {key: value for key, value in
                           V._counts(checks.get("by_result_class"), sorted(A.RESULTS)).items() if value},
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
        projects_checked.append({"project": root.name, "status": result["status"],
                                 "truncated": result.get("truncated", False),
                                 "error": result.get("error")})
        for record in result.get("runs", []):
            document = record.get("live") if live else record.get("audit")
            row = _row(root, record, document)
            # An undated row stays visible so a missing snapshot cannot silently vanish.
            if cutoff is None or row["snapshot_at"] is None or R._timestamp(row["snapshot_at"]) >= cutoff:
                rows.append(row)
    rows.sort(key=lambda row: (R._timestamp(row["snapshot_at"]).timestamp() if row["snapshot_at"] else float("-inf"),
                               row["project"], row["run_id"] or ""), reverse=True)
    return {"status": "partial" if any(p["status"] != "ok" or p["truncated"] for p in projects_checked)
            or truncated or any(row["snapshot_status"] != "current" for row in rows) else "ok",
            "source": "live" if live else "stored", "since": since, "limit_per_project": limit,
            "workspace_truncated": truncated, "projects": projects_checked, "runs": rows}


def render(result):
    if result["status"] == "error":
        return result["error"]

    def seconds(value):
        return "未知" if value is None else f"{value:.1f} 秒"

    lines = [f"跨项目审计 · {result['source']} · {result['status']} · {len(result['runs'])} 条"]
    for row in result["runs"]:
        lines.append(f"{row['project']}  {row['snapshot_at'] or '日期未知'}  "
                     f"{row['run_id'] or 'run 未知'}  {row['snapshot_status']}")
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
                           ("qa_gates", "QA 登记"), ("acceptance_gates", "最终验收登记")):
            if task.get(key):
                values = "，".join(f"{name}={value:g}" for name, value in sorted(task[key].items()))
                lines.append(f"  {label} {values}")
        host = task.get("host_diagnostics") or {}
        if host.get("failure_causes") or host.get("recovery_successes") or host.get("recovery_failures"):
            causes = ", ".join(f"{name}={count}" for name, count in sorted(host["failure_causes"].items()))
            lines.append(f"  宿主失败原因 {causes or '无登记'} · 关联恢复成功 {host['recovery_successes']} · "
                         f"关联恢复失败 {host['recovery_failures']}")
    for project in result["projects"]:
        if project["error"] or project["truncated"]:
            lines.append(f"{project['project']}  {project['error'] or '候选已截断'}")
    if result["workspace_truncated"]:
        lines.append("工作区扫描已截断")
    return "\n".join(lines)
