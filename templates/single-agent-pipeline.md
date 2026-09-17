# 便携版流水线(单 Agent 降级模式)

> pipeline-standard 的便携形态,给没有 subagent / hook 机制的 agentic 编码工具用
> (Gemini CLI、Cursor 等无子 agent/hook 的工具)。一个 agent 依次扮演全部 5 个角色,串行执行。
> 方法论、闸口、批次、打回上限与 Claude Code 版完全一致;执行保障的降级项见文末。
>
> 本文件是 `agents/*.md` 的蒸馏版,只保留核心纪律;完整角色定义见同仓库 `agents/` 目录。

## 项目契约摘要(使用本文件前必须替换以下 ❏)

本文件是通用模板,接入具体项目前请把下列占位符替换为该项目真实信息;未替换前不要直接用于生产任务:

- **项目名**: ❏ 例如 `my-project`
- **可写目录/文件白名单**: ❏ 例如 `src/`, `config/`, `package.json`(`templates/PIPELINE.md` ⑤ 节格式)
- **禁区(绝对不可碰)**: ❏ 例如 `.env`, `secrets/`, `dist/`, 上游 git 写操作
- **测试命令**: ❏ 例如 `npm run lint && npm run typecheck && npm test`
- **上线方式**: ❏ 例如 人工 `git add`/`git commit`/`git push`,Vercel 自动部署
- **外部 CLI/Skill 依赖**: ❏ 例如 `sg`, `lhci`, `ui-ux-pro-max`(完整安装命令见 `tools/quick-install.md`)
- **需求基线文档**: ❏ 例如 `PRD.md`, `README.md` 需求章节,或 Issue 链接

> 提示:把替换后的本文件保存到所用工具的自定义命令目录，并在每次触发时把任务描述附到末尾。

## 用法

把本文件接入所用工具的自定义命令机制,触发时附带任务描述:

- Gemini CLI:包一层自定义 command 引用本文件
- Cursor / 其他:直接把全文 + 任务描述粘进对话

(各工具的命令机制以其官方文档为准。)

以下「你」指执行任务的 agent 本体。

## 前置

1. 读项目根的 `PIPELINE.md`。不存在 → 停止。把 PIPELINE.md、AGENTS.md/CLAUDE.md 和相关基线切片摘要为稳定的 `context/base.md`，记录来源指纹；指纹未变时复用。每次只新建含 run_id/档位/任务/预算的 `context/run.md`，后续换装角色只补阶段差量。
2. 选择档位:`fast` 仅限 ≤2 个既有文件且无依赖/API/安全/部署/自动提交风险，执行「开发→定向自测→摘要」；`standard` 执行规划→开发/测试→收口，范围自评按风险触发；`thorough` 保留完整五角色换装、两道闸口和全套 artifact。拿不准至少 standard。
3. 测试分层与工具自检:优先采用契约的快速/终验层；旧契约未分层时，把可安全过滤的 lint/单测作为快速层，其余命令终验只跑一次，不暂停补契约。只检查当前层实际使用的 CLI，缺失告知用户；能装则装，装不上记录但不阻断。
4. 断点、裁决一致性与通知(可选):
   - 断点:`tmp/pipeline/state.md` 存在且 status 非 done → 上次 run 未完成,向用户确认续跑还是新开；standard/thorough 在阶段切换/等待用户/异常停止时维护它，fast 正常运行不创建。
   - 裁决一致性:只搜索 `rulings/index.md` 中与当前需求/文件/问题签名匹配的行，命中才读 `rulings/<run-id>.md`；CONCERNS 后更新本 run detail 和 index。
   - 通知:契约声明了 webhook 时从环境变量读 URL,闸口等待/待裁决/完成三时机各发一条纯文本;未设置静默跳过。

## 流程

### 第 1 步:澄清 + 拆解(角色 B；fast 跳过)

- 先查实质性歧义(多种合理解法 / 缺验收口径 / 范围边界不清)→ 有则不写计划,直接给用户「待澄清问题清单」,每条附建议答案。
- 无歧义 → 拆解,计划写到 `tmp/pipeline/plan.md`:
  - 任务 >4、预计超过 40 分钟或天然分层时组织为 2–4 个批次(通常每批 2–5 个任务):`plan.md` 只写头部 + 批次索引,明细写 `plan-<批次>.md`;没有并行/提前测试收益时保持单批。
  - 每任务一行 `- [ ] T编号:内容`,下方附验收标准(可执行/可测试)、涉及文件、来源条目编号。
  - 头部注明任务总数、批次数、预估时长量级;里程碑级(>10 任务或 >半日)末尾附「拆分建议」供人工定夺。
- 契约引用的大文档(PRD/Roadmap)用搜索定位章节再读,不全文通读。
- ETA 只读 metrics.tsv 最后 5 条同档记录和最近 retro；≥3 条时给中位数/P80，历史不足时按阶段给低置信度宽区间，禁止固定每任务分钟系数。

### 闸口 1:计划确认(条件人工)

fast 无歧义时跳过。standard 在用户给 `--auto` 或契约允许低风险自动进入时发简报后继续；出现风险信号则等待确认。thorough 必须等待人工确认。

### 第 2 步:范围自我评审(角色 A,≤2 轮；standard 按风险，thorough 必做，fast 跳过)

切换到评审视角,对照契约 ① 需求基线与 ② 规范逐条核 plan:有没有漏需求条目、超范围、违反禁区与架构约束。
**注意「换装」**:重新打开基线文档对照,不凭刚写完计划的记忆自评。
发现问题 → 修订计划再审;2 轮仍不过 → 停下向用户汇报分歧点,人工裁决。

### 第 3 步:开发 → 测试 批次循环(角色 C/D,打回 ≤3 轮/批)

fast 只执行变更卡、定向自测和不超过 8 行的完成摘要，追加一行 metrics 后结束，不写 plan/qa/release/retro。standard/thorough 的无批次计划视为单批，每一批次:

1. **角色 C 开发**:批次结束时一次性勾选 checkbox；只运行本批快速检查 → 向用户一行简报「批次 X 交付」。
2. **角色 D 测试**(换装:把刚写的代码当别人写的):
   - 测试范围 = 该批任务 + 验收标准;结论只能来自**实际运行的命令输出**,不凭印象。
   - 对抗式找茬:边界值、异常路径、契约声明的专项扫描都跑到。
   - 复测收敛:被打回后的复测只覆盖打回项复现 + 修复影响面 + 快速命令层;首轮已过且修复未触及的重命令不重复跑,报告注明收敛范围。
   - 报告写 `tmp/pipeline/qa/<批次>.md`，只保留当前轮全文和紧凑历史表；同步更新 `tmp/pipeline/qa/index.md` 一行索引。
   - `FAIL` → 切回角色 C 修复,修完 D 复测;单批 ≤3 轮,到顶停下汇报,不酌情放行。
   - `CONCERNS` → 把非阻断问题列给用户裁决；结果写 `rulings/<run-id>.md`，并在 rulings/index.md 追加稳定问题签名与 detail 路径。
3. 进度简报:每批 gate 出结果后给用户一行(批次名、总进度 N/M、gate 结论、下一步)。

### 第 4 步:按档位收口

standard 读取 qa/index.md、汇总终验项并把一次全量结果写入 qa/final.md；只有风险任务再做范围自评。thorough 做完整范围终验并产出 acceptance.md。fast 已在第 3 步结束。

### 第 5 步:上线建议(按档位)

fast 和 standard 人工模式直接输出精简摘要与提交信息建议，不生成 release-notes.md；thorough 才产出完整 release-notes.md。便携模式始终不做 git 写操作与部署。

### 闸口 2:提交与上线(人工)

standard/thorough 把既有信息落 `tmp/pipeline/retro.md`；fast 不写。固定头部(供 planner 下次校准用):

```markdown
# Retro

- run_id: 可空
- 任务总数: N
- 批次数: M
- 计划 ETA: X 人日 / Y 小时
- 实际时长: Z 小时(墙钟)
- 平均每任务墙钟: Z/N 分钟
- 主要偏差原因: 简述
```

正文再写:每批打回轮次、CONCERNS 次数与裁决结果、自检缺装项、一行经验。结束时向 metrics.tsv 追加 run_id/profile/tasks/batches/agents/context_bytes/test_seconds/retries/human_wait_seconds/wall_seconds/token_usage/status；不可见数据留空。只展示本档实际生成的摘要/artifact。**你不执行任何 git 写操作与部署**;git 提交、追踪矩阵打勾、部署由用户手工执行。

## 与 Claude Code 版的差异(降级项)

| 能力 | Claude Code 版 | 便携版 |
| ---- | -------------- | ------ |
| 角色上下文 | 每角色独立 subagent 冷启动,tester 看不到 dev 的实现思路 | 同一上下文,对抗性靠「换装」纪律模拟 |
| 批次节奏 | qa 测上一批与 dev 开下一批并行重叠 | 串行(开发完整批才测) |
| 禁区 ⑤ | PreToolUse hook 机器硬拦截越界写 | prompt 自觉,无硬拦截 |
| 打回循环 | dev⇄qa teammate 直聊,不占调度员上下文 | 单上下文内切换,无此需求 |
| 闸口 / 批次 / 打回上限 / 契约 | — | **完全一致** |

## 纪律

- 打回轮次上限是硬约束,到顶必须停下来汇报,不许「酌情放行」。
- 一个项目同一时刻只跑一条流水线:`tmp/pipeline/` 下的 artifact 是单例,并行触发会互相覆盖。
- **严禁执行 git 写操作**:commit/push/add/tag/reset/merge/deploy 一律由人类在闸口 2 执行；建议命令写进本档精简摘要，只有 thorough 写进 release-notes.md。
- mock/dev-only 守卫只能限定在明确 mock 路由;基础设施端点(sitemap/robots/health/manifest/openapi/_nuxt/PWA sw)必须显式豁免,不得被 mock 断言误杀。
- 任何阶段出错/超时,向用户报告当前阶段与已有产物,不要静默重试超过 1 次。
