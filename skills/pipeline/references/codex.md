# Codex 运行时说明

本文件只定义 Codex 的执行映射；流程、artifact 格式、闸口和轮次上限以父级 `SKILL.md` 为准。

## 触发和角色

fast/standard 默认当前 agent 直接执行，不创建角色或交接文件。下表及角色生命周期规则仅适用于实际委派；首次委派才建立 base/run/角色差量。thorough 保留完整角色链。

使用 `$pipeline <任务描述>` 触发。不要创建用户侧栏 task；流水线角色是当前任务的子 agent。派发前，先以当前 `SKILL.md` 的真实路径（跟随软链接）解析角色目录：`<skill-dir>/../../agents`。把解析出的绝对路径传给子 agent，不能使用相对项目根的 `agents/...` 路径。

| 流程角色 | Codex 实现 | 提示内容 |
| --- | --- | --- |
| planner | 子 agent `planner` | 角色规则、base/run、`context/planner.md` |
| scope guardian | 子 agent `scope_guardian` | 角色规则、base、待审 artifact 差量 |
| developer | 子 agent `dev` | 角色规则、base、当前批次差量 |
| tester | 子 agent `qa` | 角色规则、base、当前批次/测试差量 |
| releaser | 子 agent `releaser` | 角色规则、base、验收/上线差量 |

把角色文件作为任务规则传给子 agent，不要假设 Codex 会识别 Claude frontmatter 里的 `tools` 或 `model` 字段。调度员复用稳定的 `context/base.md`，每次只新建轻量 `context/run.md`；角色接收 base/run 路径和自己的差量路径，后续批次只用 follow-up 发送差量。AGENTS.md/CLAUDE.md 已被 base 摘要，只有指纹失配、缺字段或冲突时才让角色读原文。

只有确定需要委派时才创建对应子 agent。打回、复测、后续批次使用 follow-up 保留上下文；standard 按收益选择角色，thorough 创建完整角色集。不要为了摘要补建角色，不转发完整对话。调度员负责轮次、闸口、CONCERNS 和用户进度。

## 安全与运行边界

以下启动/清理规则仅用于研发 run；设置、帮助与状态查询按 interaction.md，不创建或移除已有锁。

1. 开始前读取项目根 `PIPELINE.md` 并创建 `.active` 标记；只有 standard/thorough 初始化完整 state.md，fast 正常运行不创建。base 来源指纹未变时复用，不重复总结。
2. 每次写入前对照已读的 `PIPELINE.md` ⑤ 节校验范围；契约变化或上下文缺失时重读，不必每次从磁盘重读。不在允许范围内时停下交由用户裁决。
3. Codex 没有本仓库 Claude hook 的 `PreToolUse` 等价配置。这个检查是强制流程步骤，不是不可绕过的工具级阻断；在最终报告中如实说明该差异。
4. `agents/*.md` 中的 Claude 模型字段仅供 Claude Code 使用。Codex 默认继承当前模型；存在角色偏好时按 [模型配置](models.md) 解析并通过实际 spawn 参数应用，不通过 prompt 模拟切换。
5. 不要自动执行全局包安装、外部仓库 clone 或写入 Codex/Claude 目录。先检测，说明影响，并在当前运行时要求授权；批准前把对应检查标为本轮跳过并写入报告。
6. 终止、异常或用户中止时，更新 `tmp/pipeline/state.md` 并移除 `tmp/pipeline/.active`。这项清理不依赖 hook。

## 协作失败时的回退

如果某个子 agent 超时、失联或无法接收 follow-up，只重试一次。仍失败时，调度员把结构化失败清单和 artifact 路径转给下一角色，向用户报告当前阶段和现场。不要静默继续，也不要绕过打回上限。
