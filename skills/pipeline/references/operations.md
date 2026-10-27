# 运行、裁决与恢复

只在续跑、异常、CONCERNS、预算预警或契约启用通知时读取。

## 续跑

- 用户已说“继续”就读取 state 和相关产物续跑，不再确认同一意图；恢复前仍须核对 run 归属及执行者停止证据。没有明确意图且存在未完成任务时才问续跑或新开。state 保留 status/stage/batches/pending/next 与必要的归属、产物及恢复证据。
- “进度”只读 state/最近报告并用已完成、剩余、阻塞回答，不启动任务、不清锁。“停止”先结束本 run 的活动执行者，保存状态再清理自己的锁。“修复中断”先确认没有活跃执行者，保留代码和产物，只清理可确认过期的锁；不要求用户手删文件。无法判断锁归属时不删除。
- 角色失联或超时只重试一次；仍失败时记录现有 artifact 和下一步，报告用户。只有确认所有执行者停止且锁归属一致才将 state 置 aborted 并移除自己的 `.active`；失联本身不证明已经停止。

## 本地运行状态接口

`scripts/run_state.py --project <项目根> [--run-id <已知ID>]` 仅查询，不创建目录、启动任务、恢复或清锁。无 active 且未指定 ID 时返回空状态；查询历史 run 必须指定 ID。旧纯文本或损坏 `.active` 返回 `active: unrecognized`，保留原字节，不猜归属或根据时间删除。

用户说“继续上次”但无active时，由智能体读取runs目录中的最近状态并按本次需求定位，不要求用户手填run_id或owner_token。多个未完成候选且无法判断时才问续跑哪个任务；不得仅凭最近修改时间删除其他记录。

标准库模块提供可测试函数，调用方先遵守项目契约与当前授权：

- `start(project_root, initial=None)` 生成唯一 UUID run_id 和随机 owner_token，排他创建 `.active`；存在任何旧标记均拒绝。返回 state 供当前调度员保存归属。initial可包含stage/activity/batches/pending/next/artifacts，不可替换身份或状态字段；新run自动保存created_at与start事件。
- `update(project_root, run_id, owner_token, changes)` 仅修改上述进度字段，校验state和active归属一致；stage/activity真实改变时自动记录transition。只改待办或重复同值不增加事件；不代表启动、停止或更换执行者。
- `finish(project_root, run_id, owner_token, outcome, executors_stopped=True, stop_evidence=...)` 支持 paused/aborted/completed。调用方必须先用宿主实际工具确认本 run 的全部执行者停止，再提供非空证据。先原子保存最终 state，再复核归属并清自己的标记；证据不充分则保留锁和现场。
- `resume(project_root, run_id, owner_token, executors_stopped=True, stop_evidence=...)` 明确恢复本人 paused/aborted 或中断的 running run；拒绝 completed、其他 run 的 active、坏锁、归属不符及缺停止证据。身份保持不变，便于状态/标记之间中断后按同一归属重试；恢复不是自动重新委派，下一步由调度员根据 pending/next 和已有产物决定。

状态放在 `tmp/pipeline/runs/<run_id>.json`，各 run 独立保留；不覆盖旧 `state.md` 或代码/产物。`.active` 仅保存 schema_version/run_id/owner_token。owner token 是本地归属凭证，不要写入公开报告。模块不操作 agent/进程，也不能验证调用方提供的停止证据是否真实；不能把传入 true 当作工具已替人终止 agent。旧纯文本锁无法经此接口自动迁移，调度员需先独立确认其归属与执行者，保留证据后再做受授权的迁移，不能通过猜测 token 或删除锁绕过。

### 轻量计时

activity仅为execution/testing/human_wait/role_wait/handoff之一，默认execution。调度员在真实阶段或等待状态改变时随update一起更新，不按命令、轮询或每条消息写事件；角色并行时仍是一条run墙钟时间线，不累加角色用时。

`timing(project_root, run_id=None)`或CLI `--timing`只读输出按activity/阶段划分的非重叠区间、tracked_seconds、paused_seconds及coverage，不输出owner_token。活动run只计算到读取时刻，结束后截止finish；暂停至恢复单列，恢复默认execution。中断的running缺少可靠停点时不把空档记成执行。计时依赖调度员准确发出状态变更，不能自动辨认它未上报的等待。

coverage=complete表示新run有完整事件边界，partial表示只能覆盖部分已观测区间，unknown表示无可用历史。旧run不从updated_at倒推开始或阶段耗时；缺数据保持未知，不把0秒当成真实没有开销。已生成报告与检查证据的归属/复用规则见[evidence.md](evidence.md)。

前台检查由run_checks.execute_check自动记录命令实际耗时并恢复调度阶段；role_metrics.summary提供角色级阶段并集和创建/续用计数，具体事件按evidence.md对接宿主。两类统计都不从测试项数或模型请求值推断耗时。resume确认旧执行者停止后，同时清除旧running_checks与临时阶段恢复记录；不得仅为绕过资源冲突调用恢复。

所有写函数与模型保存共用项目根 `.pipeline.models.lock`，排他失败立即返回；先取得该协调锁再处理 active，避免合作调用之间的设置保存/启动竞态。现有未知协调锁不自动回收。项目根可解析 macOS 系统目录别名，但项目内部 tmp、pipeline、runs、控制文件和状态文件拒绝符号链接。状态经同目录临时文件/fsync/原子替换保存，失败清理本次临时文件并保留可确认的现场。状态和 active 不是跨文件事务：写后中断可能需要带同一 run 归属和新停止证据重试；不承诺抵御非协作进程抢写或断电后目录元数据持久化。写失败时查询现文件，不盲目清锁或覆盖。

## 裁决分片

- `rulings/index.md` 每条只写：`signature | requirement-or-files | decision | run_id | detail-path`。
- 详细理由写 `rulings/<run-id>.md`。signature 由稳定的问题类型 + 需求编号/文件范围组成，不含日期或轮次。
- 启动/续跑只搜索 index；仅命中当前范围时读取对应 detail。CONCERNS 裁决后同时更新本 run detail 和 index，不读其他历史。

## 通知和预算

- 契约声明 webhook 且环境变量存在时，只在等待闸口、CONCERNS、异常/完成时发送项目名、阶段和待办；未设置静默跳过。
- 预算约 80% 时更新 state，报告已完成、剩余和收口方案，等待继续/收口裁决。

完成、失败或中止路径均应收口状态；只有确认归属和执行者已停止后才清理自己的 `.active`。无法确认时保留标记并说明所缺证据。
