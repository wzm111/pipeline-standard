# 在真实宿主边界记录角色

仅在确有收益、已经决定委派时使用；角色模型仍由models.md解析。这个适配器不创建、停止或切换模型，也不代替Claude/Codex的原生工具。不要为满足事件要求额外创建agent。

1. 准备实际spawn或新一轮follow-up前，`ticket = role_dispatch.begin(root, run_id, owner_token, role, executor_id=..., requested_model=..., requested_effort=...)`。新创建时省略executor_id；续用时用本run稳定短别名，调度上下文保存其与真实宿主ID的映射。requested值来自实际准备传入的参数；宿主仅续用既有角色且未重新传参时记inherit。begin只记派发尝试，不计成功创建。
2. 立刻调用原生创建/续用工具，提示只带规则、缓存base引用和本批差量。Codex的collaboration工具必须直接调用，不能放进Python或functions.exec模拟。宿主接受后`accepted(root, run_id, owner_token, ticket['id'], executor_id, actual_model=..., actual_effort=...)`，自动记实际created/reused及working边界。actual仅填写宿主明确返回的实际值，未反馈省略保持unknown；不能复制requested充当actual。
3. 角色实际等待或开始可观测准备时，仍可用role_metrics.record_role_event记waiting/preparing/working；没有可观察的准备时间保持unknown，不用请求到接受的差值猜模型启动耗时。角色仍在工作时只发差量消息不另开工作episode、不重复记created/reused。
4. 宿主确认本轮角色已完成后，`complete(root, run_id, owner_token, ticket['id'], executors_stopped=True, stop_evidence=...)`，自动闭合stopped。拒绝/创建失败须确认没有遗留执行者，再以outcome='failed'关闭尝试；超时/失联不能当停止证据。complete只记录状态，不会取消或销毁角色。

同一角色后续新批次先begin续用，不冷启动替代。幂等重复accepted/complete不增加事件；不允许把closed结果改成另一结论。恢复前后未接受的旧派发先按真实停止情况关闭，再发新请求，不把中断空档算作工作。

role_dispatch.summary只读输出请求/实际反馈、尝试状态与role_metrics的区间并集。未知实际模型和token保持unknown，不根据API单价或请求推算。finish只对接入本适配器且尚未闭合的派发拒绝收口，不能据此声称所有宿主工具都已被强制拦截。

当前会话全程直做、没有真实委派时，结束前`confirm_direct(root, run_id, owner_token)`；摘要将角色统计标not_applicable，不报漏事件。有角色事件/派发历史时不能改写为直做。旧上下文只用底层role_metrics的历史继续保留，缺的派发边界不事后补造。
