# 在真实宿主边界记录角色

仅在确有收益、已经决定委派时使用；角色模型仍由models.md解析。这个适配器不创建、停止或切换模型，也不代替Claude/Codex的原生工具。不要为满足事件要求额外创建agent。

1. 准备实际spawn或新一轮follow-up前，`ticket = role_dispatch.begin(root, run_id, owner_token, role, executor_id=..., requested_model=..., requested_effort=...)`。新创建时省略executor_id；续用时用本run稳定短别名，调度上下文保存其与真实宿主ID的映射。requested值来自实际准备传入的参数；宿主仅续用既有角色且未重新传参时记inherit。begin只记派发尝试，不计成功创建。
2. 立刻调用原生创建/续用工具，提示只带规则、缓存base引用和本批差量。Codex的collaboration工具必须直接调用，不能放进Python或functions.exec模拟。宿主接受后`accepted(root, run_id, owner_token, ticket['id'], executor_id, actual_model=..., actual_effort=...)`，自动记实际created/reused及working边界。actual仅填写宿主明确返回的实际值，未反馈省略保持unknown；不能复制requested充当actual。
3. 角色实际等待或开始可观测准备时，仍可用role_metrics.record_role_event记waiting/preparing/working；没有可观察的准备时间保持unknown，不用请求到接受的差值猜模型启动耗时。角色仍在工作时只发差量消息不另开工作episode、不重复记created/reused。
4. 宿主确认本轮角色已完成后，`complete(root, run_id, owner_token, ticket['id'], executors_stopped=True, stop_evidence=...)`，自动闭合stopped。拒绝/创建失败须确认没有遗留执行者，再以outcome='failed'关闭尝试；超时/失联不能当停止证据。complete只记录状态，不会取消或销毁角色。

同一run最多保留3个developer派发名额：尚未接受的dispatching、working和已有未停止developer事件都占位；第4个begin在写入前拒绝，只有真实停止并complete后才释放，不自动取消或增加名额。该门禁只覆盖已接入派发记录，不证明宿主实际容量或拦截所有原生调用；主调度员另按宿主可用名额和写入冲突决定派发顺序。

同一角色后续新批次先begin续用，不冷启动替代。幂等重复accepted/complete不增加事件；不允许把closed结果改成另一结论。恢复前后未接受的旧派发先按真实停止情况关闭，再发新请求，不把中断空档算作工作。

实际派发代表主流程进入实现或独立验收时，begin显式传`workflow_phase="implementation"`或`"verification"`；accepted确认宿主接受后，分别切换主run到implementation/execution或verification/testing，并用当下时间记录原history transition。begin本身不切换；创建失败不算进入QA。默认省略用于后台/并行角色，不根据tester等角色名抢写主阶段。请求期间若发生主状态显式更新（包括同值更新）、任务边界、人工等待或仍有前台检查/宿主观察，旧请求不覆盖现场，返回workflow_applied=false；需要切换时由调度员按实际状态显式update。

主会话确实开始等待已接受的角色时调用`wait_for_dispatches(root, run_id, owner_token, [ticket_a['id'], ticket_b['id']])`，只登记本次真正等待的集合。存在人工等待或未闭合前台检查/宿主观察时拒绝；不从角色数量推断等待。所选派发全部经complete确认停止后才把同一主状态的role_wait切到handoff；任一个仍在工作、后台角色单独返回或期间另有主状态/任务变化，都不提前切换。failed/interrupted也只是已返回待处理，handoff不表示QA通过或任务完成。重复登记同一等待和重复accepted/complete不新增transition；只记录真实调用时间，不补历史。原stop、QA与finish门禁仍适用。

需要核对模型是否真正传参时，begin可加`native_parameters={"model": "实际传值", "reasoning_effort": "实际传值", "fork_turns": "none"}`；Claude使用其原生effort字段，省略宿主未传字段。只记录模型/强度/上下文继承方式，不存prompt或凭据。显式请求与提供的原生参数必须相符；旧调用不传该字段仍兼容，但不能据此声称已验证传参。accepted可加`host_executor_id="宿主实际返回的ID"`关联本run短别名，续用保持映射不变。登记是调度员记录，仍需实际原生调用与返回证据，不能将记录本身当宿主证明。

role_dispatch.summary只读输出请求/实际反馈、尝试状态与role_metrics的区间并集。未知实际模型和token保持unknown，不根据API单价或请求推算。finish只对接入本适配器且尚未闭合的派发拒绝收口，不能据此声称所有宿主工具都已被强制拦截。

当前会话全程直做、没有真实委派时，结束前`confirm_direct(root, run_id, owner_token)`；摘要将角色统计标not_applicable，不报漏事件。有角色事件/派发历史时不能改写为直做。旧上下文只用底层role_metrics的历史继续保留，缺的派发边界不事后补造。
