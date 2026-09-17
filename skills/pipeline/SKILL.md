---
name: pipeline
description: 端到端研发流水线：按风险选择 fast、standard 或 thorough，编排开发、测试、验收与上线建议。Claude Code 用 /pipeline，Codex 用 $pipeline；需要项目根存在 PIPELINE.md。
---

# 研发流水线编排

用户显式输入 Claude Code 的 `/pipeline <任务描述>` 或 Codex 的 `$pipeline <任务描述>`。你是调度员；除 fast 的变更卡和各档摘要外，不亲自规划或实现。

## 运行时

- Claude Code 使用已注册的 `pipeline-*` agents、team 直聊与 `PreToolUse` hook。
- Codex 必须先读 [Codex 运行时说明](references/codex.md)，使用子 agent 与 follow-up；写入前对照 `PIPELINE.md` ⑤ 节校验，不得宣称具备 Claude 的机器级 hook。
- 两端共用档位、artifact、打回上限和 git 边界。Codex 的全局安装、外部 clone 与项目外写入始终先请求授权。

## 启动与路由

1. 读取项目根 `PIPELINE.md`。不存在即停止，提示先用本仓库 `templates/PIPELINE.md` 接入。
2. 若 `tmp/pipeline/.active` 存在或 state 显示未完成，先处理续跑/新开，禁止同项目并行两条流水线；需要恢复时读 [运行与恢复](references/operations.md)。随后创建 `.active`，任何结束路径都必须删除。
3. 用户显式档位优先；未声明时按风险选择并在首条简报说明。只能自动升级，不能自动降级：
   - **fast**：≤2 个既有文件；无新依赖、迁移、公开 API/数据结构、权限/支付/安全、部署配置、自动提交；范围和验收明确。只读 [fast 流程](references/fast.md)。
   - **standard**：默认。只读 [standard 流程](references/standard.md)；进入 QA 时再读 [QA 循环](references/qa-loop.md)。
   - **thorough**：>10 个任务、预计 >半日、跨系统/高风险，或用户要求完整审查。只读 [thorough 流程](references/thorough.md)；进入 QA 时再读 [QA 循环](references/qa-loop.md)。
4. fast 发现任何风险信号或需要自动提交时，升级 standard 并补 planner；thorough 的闸口 1 永不自动跳过。

## 一次生成、差量复用的上下文

调度员读取 `PIPELINE.md`、项目根 `AGENTS.md`/`CLAUDE.md`(存在时)及相关基线切片，生成稳定的 `tmp/pipeline/context/base.md`；本次任务另写 `context/run.md`：

- base：来源文件路径与内容指纹、需求/规范定位、可写范围与禁区、快速/终验测试层、上线模式。run：run_id、档位、任务摘要、预算与开始时间。
- 若已有 base 的来源指纹全部未变，直接复用；不得重新概括。旧契约未分测试层时，安全可过滤的 lint/单测归快速层，其余声明命令归终验层，不暂停补契约。
- 每个角色只接收 base、run 路径和 `context/<role>.md` 差量(当前批次、artifact、验收变化)。同一角色后续批次只发差量，不重复冷启动或转发长对话。
- 角色只有在 base 缺字段、指纹失配或发现冲突时才打开原文；developer 每次写入前仍须复核 `PIPELINE.md` ⑤ 节，releaser 执行 git 前仍须复核 ④ 节。

## 按需加载

- 当前命令所需 CLI 缺失，或当前批次确实需要但缺少设计 skill 时，才读 [依赖与 skill 自检](references/dependencies.md)。未实际使用的工具不探测、不安装。
- 仅在续跑、异常、CONCERNS 裁决或契约启用 webhook 时读 [运行与恢复](references/operations.md)。正常 PASS 流程不加载它。

## 轻量历史与度量

- 裁决使用 `tmp/pipeline/rulings/index.md` + `rulings/<run-id>.md`。启动时只搜索 index 中与当前需求编号/文件/问题签名匹配的行；无匹配不读历史正文。
- 每次结束向 `tmp/pipeline/metrics.tsv` 追加一行：`run_id profile tasks batches agents context_bytes test_seconds retries human_wait_seconds wall_seconds token_usage status`。只记录运行时实际提供的数据；token 不可见时留空，禁止估算。
- planner 只读取 metrics 最后 5 条同档记录和最近一次 retro。至少 3 条时用中位数与 P80 给 ETA；不足时按阶段给宽区间并标记低置信度，不再使用固定“每任务几分钟”系数。

## 不可变边界

- 单批测试打回最多 3 轮，范围评审最多 2 轮；到顶停报，不得酌情放行。
- 默认禁止 `git add/commit/push/tag/merge/reset` 与部署；仅 `PIPELINE.md` ④ 节明确自动提交时由 releaser 执行。
- 角色之间只传 base/run 路径、差量路径、artifact 路径和结构化结论。
- 角色超时/失联只重试一次；仍失败时保存现场、报告用户并清理 `.active`。
