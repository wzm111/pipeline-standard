# 运行、裁决与恢复

只在续跑、异常、CONCERNS、预算预警或契约启用通知时读取。

## 续跑

- 用户已说“继续”就读取 state 和相关产物续跑，不再确认同一意图；恢复前仍须核对 run 归属及执行者停止证据。没有明确意图且存在未完成任务时才问续跑或新开。state 保留 status/stage/batches/pending/next 与必要的归属、产物及恢复证据。
- “进度”只读 state/最近报告并用已完成、剩余、阻塞回答，不启动任务、不清锁。“停止”先结束本 run 的活动执行者，保存状态再清理自己的锁。“修复中断”先确认没有活跃执行者，保留代码和产物，只清理可确认过期的锁；不要求用户手删文件。无法判断锁归属时不删除。
- 角色失联或超时只重试一次；仍失败时记录现有 artifact 和下一步，报告用户。只有确认所有执行者停止且锁归属一致才将 state 置 aborted 并移除自己的 `.active`；失联本身不证明已经停止。

## 本地运行状态接口

`scripts/run_state.py --project <项目根> [--run-id <已知ID>]` 仅查询，不创建目录、启动任务、恢复或清锁。无 active 且未指定 ID 时返回空状态；查询历史 run 必须指定 ID。旧纯文本或损坏 `.active` 返回 `active: unrecognized`，保留原字节，不猜归属或根据时间删除。

标准库模块提供可测试函数，调用方先遵守项目契约与当前授权：

- `start(project_root, initial=None)` 生成唯一 UUID run_id 和随机 owner_token，排他创建 `.active`；存在任何旧标记均拒绝。返回 state 供当前调度员保存归属。initial 可包含 stage/batches/pending/next/artifacts，不可替换身份或状态字段。
- `update(project_root, run_id, owner_token, changes)` 仅修改上述进度字段，校验 state 和 active 的归属一致；不代表启动、停止或更换执行者。
- `finish(project_root, run_id, owner_token, outcome, executors_stopped=True, stop_evidence=...)` 支持 paused/aborted/completed。调用方必须先用宿主实际工具确认本 run 的全部执行者停止，再提供非空证据。先原子保存最终 state，再复核归属并清自己的标记；证据不充分则保留锁和现场。
- `resume(project_root, run_id, owner_token, executors_stopped=True, stop_evidence=...)` 明确恢复本人 paused/aborted 或中断的 running run；拒绝 completed、其他 run 的 active、坏锁、归属不符及缺停止证据。身份保持不变，便于状态/标记之间中断后按同一归属重试；恢复不是自动重新委派，下一步由调度员根据 pending/next 和已有产物决定。

状态放在 `tmp/pipeline/runs/<run_id>.json`，各 run 独立保留；不覆盖旧 `state.md` 或代码/产物。`.active` 仅保存 schema_version/run_id/owner_token。owner token 是本地归属凭证，不要写入公开报告。模块不操作 agent/进程，也不能验证调用方提供的停止证据是否真实；不能把传入 true 当作工具已替人终止 agent。旧纯文本锁无法经此接口自动迁移，调度员需先独立确认其归属与执行者，保留证据后再做受授权的迁移，不能通过猜测 token 或删除锁绕过。

所有写函数与模型保存共用项目根 `.pipeline.models.lock`，排他失败立即返回；先取得该协调锁再处理 active，避免合作调用之间的设置保存/启动竞态。现有未知协调锁不自动回收。项目根可解析 macOS 系统目录别名，但项目内部 tmp、pipeline、runs、控制文件和状态文件拒绝符号链接。状态经同目录临时文件/fsync/原子替换保存，失败清理本次临时文件并保留可确认的现场。状态和 active 不是跨文件事务：写后中断可能需要带同一 run 归属和新停止证据重试；不承诺抵御非协作进程抢写或断电后目录元数据持久化。写失败时查询现文件，不盲目清锁或覆盖。

## 裁决分片

- `rulings/index.md` 每条只写：`signature | requirement-or-files | decision | run_id | detail-path`。
- 详细理由写 `rulings/<run-id>.md`。signature 由稳定的问题类型 + 需求编号/文件范围组成，不含日期或轮次。
- 启动/续跑只搜索 index；仅命中当前范围时读取对应 detail。CONCERNS 裁决后同时更新本 run detail 和 index，不读其他历史。

## 通知和预算

- 契约声明 webhook 且环境变量存在时，只在等待闸口、CONCERNS、异常/完成时发送项目名、阶段和待办；未设置静默跳过。
- 预算约 80% 时更新 state，报告已完成、剩余和收口方案，等待继续/收口裁决。

完成、失败或中止路径均应收口状态；只有确认归属和执行者已停止后才清理自己的 `.active`。无法确认时保留标记并说明所缺证据。
