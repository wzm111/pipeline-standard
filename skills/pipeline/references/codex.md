# Codex 运行时说明

本文件只定义 Codex 的执行映射；流程、artifact 格式、闸口和轮次上限以父级 `SKILL.md` 为准。

## 触发和角色

使用 `$pipeline <任务描述>` 触发。不要创建用户侧栏 task；流水线角色是当前任务的子 agent。派发前，先以当前 `SKILL.md` 的真实路径（跟随软链接）解析角色目录：`<skill-dir>/../../agents`。把解析出的绝对路径传给子 agent，不能使用相对项目根的 `agents/...` 路径。

| 流程角色 | Codex 实现 | 提示内容 |
| --- | --- | --- |
| planner | 子 agent `planner` | `<角色目录>/pipeline-planner.md` 的角色规则、任务描述、`PIPELINE.md` 路径 |
| scope guardian | 子 agent `scope_guardian` | `<角色目录>/pipeline-scope-guardian.md` 的规则、待审 artifact 路径 |
| developer | 子 agent `dev` | `<角色目录>/pipeline-developer.md` 的规则、当前批次计划路径 |
| tester | 子 agent `qa` | `<角色目录>/pipeline-tester.md` 的规则、当前批次计划路径 |
| releaser | 子 agent `releaser` | `<角色目录>/pipeline-releaser.md` 的规则、验收结论和 artifact 路径 |

把角色文件作为任务规则传给子 agent，不要假设 Codex 会识别 Claude frontmatter 里的 `tools` 或 `model` 字段。首次派发同时传精简 context packet(当前计划路径、可写/禁区、相关验收、当前测试层、必要基线路径)；后续批次只用 follow-up 发送差量。角色文件引用 `CLAUDE.md` 时，同时检查项目的 `AGENTS.md`；两者都存在时都遵守，冲突时服从当前 Codex 的系统/用户指令。

首次需要某个角色时创建对应子 agent。qa 对已经完成的 dev 的打回、dev 对 qa 的复测请求，以及后续批次的续用，都用 follow-up 消息保留该角色上下文。fast 只创建 developer；standard 默认 planner/dev/qa，guardian 按风险创建，releaser 仅自动提交时创建；thorough 创建完整角色集。未列出的角色不得为了生成摘要而补建，摘要由调度员根据已有精简 artifact 完成。调度员继续负责轮次计数、闸口、CONCERNS 裁决和用户可见进度；不要把完整对话在角色间转发。

## 安全与运行边界

1. 开始前读取项目根 `PIPELINE.md` 并创建 `.active` 标记；只有 standard/thorough 初始化完整 state.md，fast 正常运行不创建。
2. 创建或编辑任何文件前，执行角色必须再次核对 `PIPELINE.md` ⑤ 节的可写范围和禁区；不在允许范围内时停下交由用户裁决。
3. Codex 没有本仓库 Claude hook 的 `PreToolUse` 等价配置。这个检查是强制流程步骤，不是不可绕过的工具级阻断；在最终报告中如实说明该差异。
4. `agents/*.md` 中的 Claude 模型字段仅供 Claude Code 使用。Codex 子 agent 默认继承当前模型；不要伪造或映射到不存在的 Claude 模型。
5. 不要自动执行全局包安装、外部仓库 clone 或写入 Codex/Claude 目录。先检测，说明影响，并在当前运行时要求授权；批准前把对应检查标为本轮跳过并写入报告。
6. 终止、异常或用户中止时，更新 `tmp/pipeline/state.md` 并移除 `tmp/pipeline/.active`。这项清理不依赖 hook。

## 协作失败时的回退

如果某个子 agent 超时、失联或无法接收 follow-up，只重试一次。仍失败时，调度员把结构化失败清单和 artifact 路径转给下一角色，向用户报告当前阶段和现场。不要静默继续，也不要绕过打回上限。
