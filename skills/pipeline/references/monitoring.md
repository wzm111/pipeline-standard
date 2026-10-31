# 只读监控的频率与增量

仅用户要求持续观察时使用。固定本批目标与任务边界，不把同run后来无关需求纳入；完成的样本冻结。监控不确认闸口、不发消息给执行会话、不改外部项目或全局设置，不启动实验。

优先用宿主紧凑快照和cursor；只有阶段/角色/实质失败恢复/验收变化才读取必要turn及本任务报告，先筛技术证据，不扫描长历史。规则留在本文件，自动检查提示只引用本批ignored状态文件、固定目标及边界，不反复复制历史报告或整套规则。报告只追加变化；cursor及退避计数只更新小状态，不为无变化轮询重写长摘要。

智能体将当前任务的安全标识和已观察事实交给`monitor_policy.decide(previous, observation)`，把返回state保存到本批ignored记录。字段为task_id、cursor、stage、activity、host_status（active/idle/unknown）、live_actors（真实可确认数量或unknown）、needs_user/action_id、failure_id及实际宿主反馈的model_feedback_id；标识不含提示、业务原文或凭据。outcome默认open，只有当前任务实际验收/明确停止才给accepted/stopped；executors_stopped必须来自真实宿主，不拿登记事件代替证明。

策略只建议：活动进展默认10分钟；无变化逐步退避，最多60分钟；人工等待至少30分钟，新进展恢复默认。首次明确用户动作/新故障/实际型号变化才通知，相同动作不催促。record_change=false只更新小状态，不新增报告正文；notify=false保持安静。需要结束时先取得验收/明确停止、host idle和执行者停止，再补一次无变化的紧凑快照确认；next_check_seconds=0只表示立即确认一次，不创建零间隔计划。

调度员在用户原有监控授权范围内，用宿主automation更新工具应用next_check_seconds；stop_monitor=true时用删除工具结束检查并说明已停止。模块本身不创建/修改/删除计划、不读写文件、不判断业务验收。宿主不能动态调频时，保留原计划并说明限制，不宣称已经降频；不为轮询增加付费角色。人工等待/未知角色/旧混合run未结束均不能自行判PASS；等待失去可行动价值时按本批停止边界说明未验证项后结束观察，不虚报任务完成。
