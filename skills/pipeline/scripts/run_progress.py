"""Bounded read-only progress, never a health probe or acceptance judgment."""
import hashlib
import json
import re

import run_evidence as E
import run_state as R

LIMIT = 20
REPORT_LIMIT = 200
NOTICE = "Registered results only; freshness, host health and acceptance are not revalidated."


def snapshot(root, run_id=None, *, host_status="unknown", host_ended_at=None):
    if host_status not in {"unknown", "running", "idle"}:
        raise ValueError("host_status must be unknown, running or idle")
    if host_ended_at is not None and host_status != "idle":
        raise ValueError("host ended boundary requires idle feedback")
    before = R.status(root, run_id)
    state = before["state"] or {}
    identity = state.get("run_id", run_id)
    owner = state.get("owner_token", "")

    def label(value):
        return value if (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", value)
                         and not (owner and owner in value)) else "unknown"

    def stamp(value):
        try:
            return R._timestamp(value).isoformat() if isinstance(value, str) else None
        except (ValueError, TypeError, OverflowError):
            return None

    tasks = state.get("tasks", [])
    task = tasks[-1] if tasks else None
    start = stamp(task.get("started_at")) if task else stamp(state.get("created_at"))
    ended = stamp(task.get("ended_at")) if task else None
    task_status = task.get("status") if task and task.get("status") in {
        "running", "execution_ended", "paused", "aborted"} else "unknown"
    idle_boundary = stamp(host_ended_at)
    observed_host = "unknown"
    if host_ended_at is not None and (not idle_boundary or R._timestamp(idle_boundary) > R._timestamp(R._now())):
        raise ValueError("host idle boundary must be a valid non-future timestamp")
    if host_status == "running":
        observed_host = "running"
    elif idle_boundary:
        # An old native stop cannot describe a newer registered task/update.
        updated = stamp(state.get("updated_at"))
        observed_host = "idle" if updated and R._timestamp(idle_boundary) >= R._timestamp(updated) else "stale"
    reports, unavailable = [], 0
    refs = state.get("artifacts", [])
    for ref in refs[-REPORT_LIMIT:]:
        if not isinstance(ref, str) or not re.fullmatch(
                rf"tmp/pipeline/runs/{identity}/artifacts/(check|planned_check|check_plan)/[a-z][a-z0-9_-]{{0,63}}\.json", ref):
            continue
        try:
            report = E.read_report(root, identity, ref)
            at = stamp(report["recorded_at"])
            if not at or not isinstance(report["payload"], dict):
                raise ValueError("invalid progress record")
            if ((start and R._timestamp(at) < R._timestamp(start))
                    or (ended and R._timestamp(at) > R._timestamp(ended))):
                continue
            reports.append((ref, report))
        except (OSError, ValueError, TypeError, KeyError):
            unavailable += 1
    by_ref = dict(reports)
    plans = [(ref, r) for ref, r in reports if r["kind"] == "check_plan" and ref in state.get("check_plans", {})]
    plan_ref, plan = plans[-1] if plans else (None, None)
    bindings = {r["payload"].get("check_id"): r["payload"] for _, r in reports
                if r["kind"] == "planned_check" and r["payload"].get("plan_ref") == plan_ref}

    def check_identity(proof):
        return json.dumps([proof.get("command"), proof.get("scope"), proof.get("cwd", "."),
                           proof.get("intent", "test")], sort_keys=True)

    latest = {}
    last_result_at = None
    for ref, report in reports:
        if report["kind"] != "check":
            continue
        proof = report["payload"]
        latest[check_identity(proof)] = ref
        at = stamp(proof.get("finished_at"))
        if (at and not proof.get("inherited_from")
                and (not start or R._timestamp(at) >= R._timestamp(start))
                and R._timestamp(at) <= R._timestamp(report["recorded_at"])
                and (last_result_at is None or R._timestamp(at) > R._timestamp(last_result_at))):
            last_result_at = at
    completed, remaining = [], []
    checks = plan["payload"].get("checks", {}) if plan else {}

    def append_result(key, proof):
        result = "not_observed"
        if proof is not None:
            result = ("recorded_failure" if proof.get("exit_code") != 0 else
                      "recorded_success" if proof.get("inputs_stable") is True else "unconfirmed_inputs")
        item = {"step": label(key), "result": result,
                "finished_at": stamp(proof.get("finished_at")) if proof else None}
        (completed if result == "recorded_success" else remaining).append(item)

    for key in checks:
        binding = bindings.get(key, {})
        source = binding.get("result", {}).get("report")
        report = by_ref.get(source)
        proof = report["payload"] if report and report["kind"] == "check" else None
        # A later observation wins even if recorded under a different coverage label.
        if proof is not None:
            proof = by_ref[latest[check_identity(proof)]]["payload"]
        append_result(key, proof)
    if plan is None:
        # Direct single-check work needs no plan or task segmentation. Reuse
        # the same bounded, task-filtered observations and latest-result rule;
        # observed results do not establish a complete remaining-work list.
        for ref in latest.values():
            proof = by_ref[ref]["payload"]
            if proof.get("intent", "test") in {"test", "check"}:
                append_result(ref.rsplit("/", 1)[-1][:-5], proof)
    active_checks = [{"step": label(item.get("name")), "started_at": stamp(item.get("started_at"))}
                     for item in state.get("running_checks", [])]
    roles = [{"role": label(item.get("role")), "status": item["status"]}
             for item in state.get("role_dispatches", []) if item.get("status") in {"working", "dispatching"}]
    waiting = state.get("continuity_wait", {}).get("status") == "waiting"
    declared_wait = state.get("activity") == "human_wait"
    blockers = (["user_action_required"] if waiting else [])
    if declared_wait and not waiting:
        blockers.append("human_wait_action_unrecorded")
    if observed_host == "idle" and (roles or active_checks):
        blockers.append("host_idle_with_registered_work")
    if any(item["result"] == "recorded_failure" for item in remaining):
        blockers.append("recorded_check_failure")
    status = state.get("status") if state.get("status") in {"running", *R.FINAL} else "unknown"
    conflicts = any(key in blockers for key in ("human_wait_action_unrecorded", "host_idle_with_registered_work"))
    badge = ("🔴[需你处理]" if waiting else "🔎[待核实]" if conflicts else
             "🟠[修复中]" if blockers else "✅[已完成]" if status == "completed" else
             "🔎[待核实]" if ended or observed_host in {"idle", "stale"} else
             "⏳[等待中]" if roles or active_checks else "🔎[待核实]")
    delivery_status = ("unknown" if not state else "external_action_pending" if waiting or declared_wait else
                       "run_completed_recorded" if status == "completed" else
                       "execution_ended_acceptance_pending" if ended else "in_progress")
    result = {"run_id": identity, "task_id": label(task.get("task_id")) if task else None,
              "status": status, "stage": label(state.get("stage")),
              "activity": state.get("activity") if state.get("activity") in R.ACTIVITIES else "unknown",
              "badge": badge, "current": {"checks": active_checks[:LIMIT], "roles": roles[:LIMIT]},
              "task_execution": {"status": task_status, "ended_at": ended},
              "delivery": {"status": delivery_status, "acceptance": "not_revalidated"},
              "host_feedback": {"status": observed_host, "ended_at": idle_boundary,
                                "basis": "caller_supplied_not_probed"},
              "completed": completed[:LIMIT], "remaining": remaining[:LIMIT], "blockers": blockers,
              "counts": {"recorded_success": len(completed), "remaining": len(remaining),
                         "planned": len(checks) if plan else None},
              "next": "user_action_then_verify" if waiting else
                      "reconcile_host_and_records" if conflicts or observed_host in {"idle", "stale"} and status != "completed" else
                      "review_task_acceptance" if ended and status != "completed" else "inspect_remaining" if remaining else
                      "review_current_qa" if (plan or completed) and status == "running" else
                      "none" if status == "completed" else "inspect_host_and_plan",
              "last_result_at": last_result_at, "state_updated_at": stamp(state.get("updated_at")),
              "unavailable_records": unavailable,
              "truncated": len(refs) > REPORT_LIMIT or any(len(items) > LIMIT for items in (completed, remaining, active_checks, roles)),
              "notice": NOTICE}
    # Do not manufacture a new milestone from a state timestamp-only refresh.
    result["milestone_id"] = hashlib.sha256(json.dumps(
        {k: v for k, v in result.items() if k != "state_updated_at"}, sort_keys=True).encode()).hexdigest()
    if R.status(root, run_id) != before:
        raise ValueError("run changed during progress snapshot; recheck progress")
    return result


def render(value):
    def names(items):
        return ", ".join(item["step"] for item in items) or "未登记"
    current = names(value["current"]["checks"])
    if value["current"]["roles"]:
        current += "；等待角色反馈：" + ", ".join(item["role"] for item in value["current"]["roles"])
    next_step = {"user_action_then_verify": "完成原人工动作后验证", "inspect_remaining": "核对剩余步骤及其证据",
                 "reconcile_host_and_records": "核对宿主反馈与登记状态，不自动重启或清锁",
                 "review_task_acceptance": "核对当前任务验收及待交付项，执行结束不代表验收通过",
                 "review_current_qa": "复核当前QA和收口条件", "none": "本地任务已登记结束",
                 "inspect_host_and_plan": "核对宿主反馈和当前计划"}[value["next"]]
    boundary = {"running": "执行中", "execution_ended": "执行段已结束", "paused": "执行段暂停",
                "aborted": "执行段已中止", "unknown": "未登记"}[value["task_execution"]["status"]]
    delivery = {"external_action_pending": "等待原人工/外部操作", "run_completed_recorded": "run已登记完成",
                "execution_ended_acceptance_pending": "验收/交付待核实", "in_progress": "未收口",
                "unknown": "未知"}[value["delivery"]["status"]]
    return (f"{value['badge']} 当前任务：{value['task_id'] or '未分段'}；阶段：{value['stage']}；当前步骤：{current}。\n"
            f"任务执行：{boundary}；交付：{delivery}；验收有效性未重新核验；宿主反馈：{value['host_feedback']['status']}。\n"
            f"已记录成功：{names(value['completed'])}；剩余/待核实：{names(value['remaining'])}；"
            f"阻塞：{', '.join(value['blockers']) or '未登记'}。最近检查结果：{value['last_result_at'] or '未知'}。下一步：{next_step}。\n"
            "以上为登记记录，未重新核验有效性或宿主健康；无计划时剩余步骤未知，截断时需查看当前计划。")
