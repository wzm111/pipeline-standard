---
name: pipeline
description: 端到端研发流水线:任务拆解 → 范围评审 → 开发⇄测试(打回≤3轮) → 验收 → 上线建议。当用户输入 /pipeline <任务描述> 时使用。需要项目根存在 PIPELINE.md 契约文件。
---

# 研发流水线编排

用户输入了 `/pipeline <任务描述>`。你是调度员,按以下固定流程用 Agent 工具调度角色 subagent(角色名见各步),不要自己干活,只编排与传递 artifact 路径。

## 前置检查

读项目根的 `PIPELINE.md`。
- 不存在 → 停止,告诉用户「本项目还未接入流水线」,并把契约模板(五节:需求基线/架构规范/测试命令/上线方式/禁区边界)展示给用户,等其补齐后再来。
- 存在 → 记住其中声明的需求基线、规范、测试命令、禁区,后续每个角色的 prompt 里都要带上「先读 PIPELINE.md」。

## 流程

### 第 1 步:任务拆解(Agent B)
调 `pipeline-planner`:传入用户任务描述,要求按契约的需求基线拆解,计划写到 `tmp/pipeline/plan.md`。

### 闸口 1:计划确认(人工)
把计划摘要展示给用户,**等用户明确确认后才继续**。用户有修改意见时,把意见交给 `pipeline-planner` 修订 plan.md 后再次展示确认——调度员不直接改计划。

### 第 2 步:范围评审(Agent A,≤2 轮)
调 `pipeline-scope-guardian` 评审 plan.md:
- 通过 → 下一步
- 打回 → 把问题清单带回 `pipeline-planner` 修订后再审;2 轮仍不过 → 停止,向用户汇报分歧点,人工裁决。

### 第 3 步:开发 ⇄ 测试(Agent C / D,≤3 轮)
循环:
1. `pipeline-developer` 按 plan.md 开发(首轮)或按打回反馈修复(后续轮)
2. `pipeline-tester` 按契约测试命令实测,报告写 `tmp/pipeline/qa-report.md`
3. 通过 → 下一步;不通过 → 把失败清单打回给 developer
4. 3 轮仍不过 → 停止,向用户汇报,附最新 qa-report.md 结论

### 第 4 步:验收(Agent A)
`pipeline-scope-guardian` 对照 plan.md 验收标准与测试报告做最终验收。不过 → 向用户汇报,人工决定回第 3 步还是调整计划。

### 第 5 步:上线建议(Agent E)
`pipeline-releaser` 产出 `tmp/pipeline/release-notes.md`(变更摘要 + Conventional Commits 建议 + 人工待办)。

### 闸口 2:提交与上线(人工)
展示 release-notes.md 摘要。git 提交、追踪矩阵打勾、部署由用户手工执行——流水线到此结束。

## 纪律

- 打回轮次上限是硬约束,到顶必须停下来汇报,不许「酌情放行」。
- 一个项目同一时刻只跑一条流水线:`tmp/pipeline/` 下的 artifact 是单例,并行触发会互相覆盖。
- 角色之间只传 artifact 文件路径 + 结构化结论,不转发长篇对话。
- 任何角色超时/崩溃,向用户报告当前阶段与已有产出,不要静默重试超过 1 次。
