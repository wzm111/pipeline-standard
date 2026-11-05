"""Local, read-only projection of audit metadata. No transport or file export."""
import math

import run_audit as A
import run_state as R

NOTICE = "Local registered metadata only; not acceptance, host health or reuse evidence."
ERRORS = {"unsafe_path", "invalid_audit", "identity_mismatch", "permission_denied", "storage_error",
          "invalid_data", "state_changed", "active_owner_unrecognized"}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _number(value, *, count=False):
    if (type(value) not in ((int,) if count else (int, float))
            or value < 0 or not math.isfinite(value)):
        return None
    return value


def _counts(value, keys):
    value = _dict(value)
    return {key: _number(value.get(key), count=True) for key in keys}


def _enum(value, choices):
    return value if isinstance(value, str) and value in choices else "unknown"


def preview(project_root, run_id=None, *, live=False):
    """Select explicit/current/latest locally, then expose only typed leaves.

    Uses the existing bounded scan/audit readers. Never scans project content,
    validates lineage, calls a process/network client, writes or clears locks.
    """
    selection, truncated = "explicit", False
    try:
        if run_id is not None:
            result = A.audit(project_root, run_id, live=live)
        else:
            current = R.status(project_root)
            if current["active"] == "unrecognized":
                raise A.AuditError("active_owner_unrecognized")
            if current["active"]:
                marker = current["active"]
                R._owned(current["state"], marker["run_id"], marker["owner_token"])
                selection = "active"
                result = A.audit(project_root, current["state"]["run_id"], live=live)
            else:
                scan = A.scan(project_root, limit=1)
                if scan["status"] == "error":
                    result = scan
                elif not scan["runs"]:
                    selection, result = "none", {"status": "none"}
                else:
                    selection, result = "latest", scan["runs"][0]
                    truncated = scan["truncated"]
                    if live and result.get("run_id"):
                        result = A.audit(project_root, result["run_id"], live=True)
    except Exception as error:
        result = {"status": "error", "error": A._error(error)}
    selected = result.get("live") if live else result.get("audit")
    source = "live" if live and selected is not None else "stored" if selected is not None else "none"
    document = _dict(selected)
    timing, checks = _dict(document.get("timing")), _dict(document.get("checks"))
    adoption, coverage = _dict(document.get("adoption")), _dict(document.get("coverage"))
    tasks, latency = _dict(document.get("tasks")), _dict(document.get("latency"))
    details = document.get("task_details")
    # Stored details pass the closed schema in audit(); live details come from its builder.
    samples = []
    for item in latency.get("samples", [])[:20]:
        item = _dict(item)
        samples.append({"index": _number(item.get("index"), count=True),
            **{key: _number(item.get(key)) for key in
               ("wall_seconds", "check_union_seconds", "operation_union_seconds", "wait_union_seconds", "unattributed_seconds")},
            **_counts(item, ("check_records", "reuse_records", "failure_records", "invalid_timing_records"))})
    snapshot_at = document.get("snapshot_at")
    try:
        snapshot_at = R._timestamp(snapshot_at).isoformat() if snapshot_at is not None else None
    except (ValueError, TypeError, OverflowError):
        snapshot_at = None
    return {"schema_version": 1, "local_only": True, "source": source,
        "snapshot_status": _enum(result.get("status"), {"current", "stale", "missing", "none", "error"}),
        "selection": selection, "selection_truncated": truncated,
        "error": _enum(result.get("error"), ERRORS) if result.get("status") == "error" else None,
        "run_status": _enum(document.get("status"), {"running", *R.FINAL}), "snapshot_at": snapshot_at,
        "completeness": _enum(document.get("completeness"), {"partial", "recorded_only"}),
        "timing": {"coverage": _enum(timing.get("coverage"), {"unknown", "partial", "complete"}),
                   "tracked_seconds": _number(timing.get("tracked_seconds")),
                   "activity_seconds": {key: _number(_dict(timing.get("activity_seconds")).get(key))
                                        for key in sorted(R.ACTIVITIES | {"paused"})}},
        "checks": {**_counts(checks, ("executed", "reused", "inherited", "unavailable", "check_records")),
                   "by_result_class": _counts(checks.get("by_result_class"), sorted(A.RESULTS))},
        "task_count": _number(tasks.get("count"), count=True),
        "adoption": {"mode": _enum(adoption.get("mode"), {"managed", "legacy_untracked", "unknown"}),
                     **{key: adoption.get(key) if type(adoption.get(key)) is bool else None
                        for key in ("current", "caller_acknowledged", "stale")}},
        "evidence_coverage": {"complete": coverage.get("complete") if type(coverage.get("complete")) is bool else None,
                              "missing_count": _number(coverage.get("missing_count"), count=True)},
        "latency": {"coverage": _enum(latency.get("coverage"), {"unknown", "partial", "recorded_only"}),
                    "truncated": latency.get("truncated") if type(latency.get("truncated")) is bool else None,
                    "samples": samples},
        "task_details": details, "actual_model": None, "tokens": None, "notice": NOTICE}


def render(value):
    def show(number):
        return "未知" if number is None else str(round(number, 3)) if type(number) is float else str(number)
    names = {"current": "与原状态一致", "stale": "已过期", "missing": "缺失", "none": "无记录",
             "error": "读取失败", "unknown": "未知", "running": "登记运行中", "completed": "登记完成",
             "paused": "暂停", "aborted": "中止", "partial": "部分采集", "recorded_only": "仅已登记部分"}
    lines = ["本地运行报告（仅预览）", f"来源：{'当前元数据只读计算' if value['source'] == 'live' else '已有本地快照' if value['source'] == 'stored' else '无可用摘要'}；快照：{names[value['snapshot_status']]}"]
    if value["error"]:
        lines.append("读取诊断：" + value["error"])
    lines += [f"运行结果：{names[value['run_status']]}；采集：{names[value['completeness']]}；任务数：{show(value['task_count'])}",
              f"快照时间：{value['snapshot_at'] or '未知'}；登记时长：{show(value['timing']['tracked_seconds'])} 秒",
              "活动秒数：" + "，".join(f"{key}={show(v)}" for key, v in value["timing"]["activity_seconds"].items()),
              "检查登记：" + "，".join(f"{key}={show(value['checks'][key])}" for key in ("check_records", "executed", "reused", "inherited", "unavailable")),
              "结果分类：" + ("，".join(f"{key}={show(v)}" for key, v in value["checks"]["by_result_class"].items() if v) or "未登记"),
              f"规则采用：{value['adoption']['mode']}；当前确认：{show(value['adoption']['caller_acknowledged'])}",
              f"证据覆盖登记：{show(value['evidence_coverage']['complete'])}；缺项数：{show(value['evidence_coverage']['missing_count'])}（不代替业务验收）"]
    for sample in value["latency"]["samples"]:
        lines.append(f"任务段 {show(sample['index'])}：墙钟 {show(sample['wall_seconds'])} 秒；检查并集 {show(sample['check_union_seconds'])} 秒；等待/交接并集 {show(sample['wait_union_seconds'])} 秒；未归因 {show(sample['unattributed_seconds'])} 秒")
    for sample in value.get("task_details") or []:
        roles = sum(sum(bucket.values()) for bucket in sample["dispatches"].values())
        lines.append(f"任务段 {sample['index']} 明细：实跑 {sample['executed']}，复用 {sample['reused']}，继承 {sample['inherited']}，宿主观察 {sample['host_observation_records']}；派发记录 {roles}，已结束角色并集 {show(sample['role_union_seconds'])} 秒，专家尝试 {sample['expert_attempts']}。")
        if sample["qa_gates"]:
            gates = "，".join(f"{gate} {count}" for gate, count in sorted(sample["qa_gates"].items()))
            lines.append(f"  QA 登记：{gates}；机器校验记录 {sample['machine_qa_records']}，早于 tester 返回的 PASS {sample['qa_pass_before_tester_return']}，任务边界不符 {sample['qa_boundary_mismatch']}。")
        if sample["release_checks"] or sample["release_phases"] or sample["release_closeout"] is not None:
            lines.append(f"  发布登记：检查 {sample['release_checks']}，计划前置 {show(sample['release_plan_before_checks'])}，收口引用 {show(sample['release_closeout'])}。")
    if value["selection_truncated"] or value["latency"]["truncated"]:
        lines.append("候选扫描或任务摘要有截断；当前预览不代表全部历史。")
    lines.append("仅本地预览，不写入或上传。未知项不估算；未归因时间不等于浪费。登记不证明业务验收、宿主健康或证据可复用；规则确认不证明全部规范已执行。实际模型和 token 未采集。")
    return "\n".join(lines)
