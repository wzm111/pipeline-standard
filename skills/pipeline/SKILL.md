---
name: pipeline
description: 端到端研发流水线：澄清、拆解、范围评审、批次级开发和测试、验收与上线建议。Claude Code 用 /pipeline，Codex 用 $pipeline；需要项目根存在 PIPELINE.md 契约文件。
---

# 研发流水线编排

用户显式输入了 Claude Code 的 `/pipeline <任务描述>` 或 Codex 的 `$pipeline <任务描述>`。你是调度员：为标准/里程碑任务编排角色 subagent，不亲自实现功能；只编排并传递精简的 context packet 与 artifact 路径。低风险直通任务可以省略规划和独立 QA，但必须满足下述全部条件。

## 运行时适配

- **Claude Code**：使用已注册的 `pipeline-*` agents、team 直聊和本仓库的 `PreToolUse` hook。保留原有完整并行流程。
- **Codex**：先读 [Codex 运行时说明](references/codex.md)。使用 Codex 子 agent 与 follow-up 消息执行同一角色流程；每次写入前由调度员和执行角色对照 `PIPELINE.md` ⑤ 节校验路径。Codex 没有 Claude `PreToolUse` hook 的同等配置入口，不得把这项流程校验描述为机器级硬拦截。
- 两端共同遵守本文件的闸口、批次、打回轮次、artifact、验收与 git 边界。Claude Code 保留本文件既有的、契约声明范围内的自动 CLI/skill 安装流程；Codex 对全局 CLI 安装、外部 skill clone 和其他项目外写入先请求授权，未获授权时跳过对应检查而不阻断其余流程。

## 执行档位与成本控制

先根据用户显式档位(`--fast`、`--standard`、`--thorough`)或 `PIPELINE.md` ⑥ 节选择；未声明时按风险自动选择，并在首条简报说明档位和原因。用户随时可升级档位，不能被自动降级。

- **直通(`fast`)**：仅限改动不超过 2 个既有文件、无新依赖/迁移/公开 API/鉴权支付安全/部署配置/自动提交、验收路径明确的低风险任务。执行链固定为 `developer → 定向自测 → 调度员核对变更卡并摘要`；不创建 planner/guardian/qa/releaser，不生成 plan、qa、acceptance、release-notes 或 retro。
- **标准(`standard`)**：默认档。执行链为 `planner → (按风险 guardian) → developer ⇄ qa → (同一 guardian 终验或 qa 收口) → 调度员摘要`。guardian 仅在涉及 5 个以上文件、新依赖、公开接口/数据结构、架构边界、权限/支付/安全或需求基线冲突时创建；只有契约声明自动提交时才创建 releaser。
- **里程碑(`thorough`)**：超过 10 个任务、预计超过半日、跨系统/高风险改动，或用户明确要求完整审查时使用。执行链固定为 `planner → guardian → developer ⇄ qa → guardian → releaser`，保留两道闸口、分批、终验与全套 artifact；先给出拆分建议，默认一次只执行经用户确认的一组批次。

`--auto` 或契约的「低风险自动进入开发」只可跳过标准档的闸口 1，前提是计划没有新增依赖、禁区触碰、外部写入、范围不确定或上述高风险信号。任一信号出现立即回到人工闸口；里程碑任务的闸口 1 永不自动跳过。fast 运行中一旦发现风险信号或需要自动提交，立即升级 standard 并补 planner，不得继续直通。

为每个角色生成 **context packet**，只包含本批计划路径、可写范围/禁区、相关验收标准、当前测试层和必要的基线路径。它是交接摘要，不替代 `PIPELINE.md`：developer 每次写入前仍须复核 ⑤ 节，其他角色在 packet 缺字段或发现冲突时才回读契约和原始文档。不得把完整 PRD、完整 plan 或长篇对话重复转发给每个角色。

## 前置

1. 读项目根的 `PIPELINE.md`。
   - 不存在 → 停止,告诉用户「本项目还未接入流水线」,契约模板见 pipeline-standard 的 `templates/PIPELINE.md`,等其补齐后再来。
   - 存在 → 提取需求基线、规范、测试命令、禁区和 ⑥ 节执行策略，供后续生成 context packet。
2. 运行标记与断点:
   - `mkdir -p tmp/pipeline && touch tmp/pipeline/.active`(启用禁区硬拦截)。**流程无论以何种方式结束(完成/打回到顶/用户中止),都要 `rm -f tmp/pipeline/.active`**,否则会影响普通会话的写操作。
   - `tmp/pipeline/state.md` 存在且 status 非 done → 上次 run 未完成,向用户确认「续跑(现场在 state.md)还是新开」;新开 → 旧 state.md 改名 `state-<日期>.md` 留档。
   - standard/thorough 初始化并维护 state.md(status / stage / batches / pending / next 五要素)；fast 正常完成不创建 state.md，只在异常/中止时写最小 aborted 现场。示例:

     ```markdown
     status: running
     stage: gate-1
     batches: A,B,C
     pending: A
     next: 等待用户确认闸口 1
     ```
   - `tmp/pipeline/rulings.md` 存在 → 读入已有裁决记录;续跑时遇到相同批次/相同问题,直接按文件结论执行,不再重复询问用户。
3. 按「执行档位与成本控制」分流。拿不准、存在实质歧义或无法确认风险时，至少走标准档，不用 fast 猜测。
4. 测试分层与工具自检:
   - 优先使用 PIPELINE.md ③ 节声明的「每批快速检查 / 终验全量」。旧契约未分层时不暂停、不要求先改契约：把能安全按路径/测试名过滤的 lint/单测归为快速层，其余声明命令归为终验层；无法确定安全过滤方式时，本批只做明确的定向验证，全部原命令在收口运行一次，并把自动分类写入 context packet。
   - 只检查本 run 当前测试层会实际执行的外部 CLI；收口/里程碑工具在进入终验时再检查，避免为不执行的命令安装或消耗时间。
   - **Claude Code**：缺失 → 告知用户并按 [tools/quick-install.md](../../tools/quick-install.md) 自动安装,装完跑 `--help` 验证可用；速查表没有的 CLI → 尝试 `npm install -g <同名包>`,失败按降级处理
   - **Codex**：缺失 → 说明工具、安装命令和影响，等待用户明确授权后才安装；未授权或安装失败 → 跳过对应扫描项，并在 qa-report 说明
   - 契约声明的是**项目 devDep 型**工具(如 playwright、@axe-core/playwright)→ 不全局安装,提示该项目应自行 `npm i -D` 并接入 npm scripts,本轮跳过对应项并在 qa-report 说明
   - 安装失败(断网/权限不足)→ 明确告知用户,并在派发给 tester 的 prompt 里注明「该工具本轮不可用,跳过对应扫描项,在 qa-report 说明」,**不因此中断流水线**(核心测试命令不受影响)
   - 契约没声明的工具不装,不做多余动作
5. skill 自检(惰性执行):只有当前变更卡/计划批次确实需要契约 ② 声明的设计类 skill 时才检查；非 UI 或基线已经覆盖的任务跳过。
   - Claude Code 探测 `~/.claude/skills/<name>/` 与项目 `.claude/skills/<name>/`；Codex 探测 `~/.codex/skills/<name>/` 与项目 `.codex/skills/<name>/`。存在 → 过
   - 缺失且契约附了 git 来源 → Claude Code 克隆到临时目录后安装；Codex 先展示来源、目标目录与安全扫描范围，等待用户明确授权后才 clone 和安装(兼容两种仓库布局):
     - 仓库根有 `SKILL.md` → 整体拷到当前运行时的 skill 目录
     - 否则查仓库内 `.claude/skills/<name>/` 或 `.codex/skills/<name>/` 子目录 → 拷它到当前运行时的 skill 目录
     - 两种都没有 → 按安装失败处理
   - **拷贝安装前先做安全扫描**:Grep 克隆内容(SKILL.md 及附带脚本)的可疑模式——`curl`/`wget` 管道执行、`base64 -d`、向声明外域名外发数据、读写目标 skill 目录之外的路径、访问 `~/.ssh`/`~/.aws` 等敏感目录。命中 → 不安装,告知用户「该 skill 内容可疑,请人工审查后手动安装」,按降级处理
   - 缺失且无来源/安装失败 → 告知用户,并在派发给 developer 的 prompt 里注明「该 skill 本轮不可用,降级按基线与规范处理」,**不因此中断流水线**
6. 通知通道(契约声明了通知 webhook 时):URL 从环境变量读(如 `FEISHU_WEBHOOK_URL`),未设置 → 静默跳过不阻断。三个触发时机各发一条纯文本(curl,内容=项目目录名 + 当前阶段 + 在等用户做什么):①闸口 1 等待确认;②CONCERNS 待裁决;③打回到顶或闸口 2 完成。除此之外不刷屏。

## 流程

### 第 1 步:澄清 + 拆解(Agent B；fast 档跳过)
调 `pipeline-planner`,传入用户任务描述:
- 返回**待澄清问题** → 把问题(含建议答案)展示给用户,收集回答后交给 planner 再拆。
- 返回**计划**(`tmp/pipeline/plan.md`;按批次组织时另有 `plan-<批次>.md` 详情文件)→ 进闸口 1 或按已授权策略自动进入开发。

### 闸口 1:计划确认(条件人工)
计划摘要必须附任务数、批次数、墙钟/人力 ETA；里程碑级还须附拆分建议和本 run 上限。标准档只有在用户未给 `--auto`、契约未授权自动进入，或出现风险信号时才等待明确确认；其余仅发送简短计划简报后继续。用户有修改意见时，把意见交给 `pipeline-planner` 修订计划——调度员不直接改计划。

### 第 2 步:范围评审(Agent A,≤2 轮；standard 按风险触发，thorough 必做，fast 跳过)
需要 guardian 时调 `pipeline-scope-guardian` 评审 plan.md，并保留同一角色供终验复用:
- 通过 → 下一步
- 打回 → 把问题清单带回 `pipeline-planner` 修订后再审;2 轮仍不过 → 停止,向用户汇报分歧点,人工裁决。

### 第 3 步:开发 ⇄ 测试

fast:把不超过 5 行的变更卡交给 developer；developer 返回变更文件、定向自测命令和结果。调度员核对变更卡，符合即直接进入 fast 收尾；不创建 qa artifact。出现失败、范围扩张或风险信号则升级 standard。

standard/thorough:进入下述批次级循环 + team 直聊，打回 ≤3 轮/批。

计划按批次组织时逐批循环;无批次的计划视为单批(=整体),流程相同。首轮交接经由你，打回循环让两个 teammate 直接续跑对话（Claude Code 用 SendMessage；Codex 用 follow-up 消息）。

每一批次:

1. `dev` 开发该批:完成后一次性更新本批 checkbox；只运行 ③ 节标为「每批快速检查」且受本批影响的命令，不在每批重跑全量 E2E/构建/性能/收口扫描 → 向你简报「批次 X 交付」。
2. 你收到交付简报后**两件事同时做**:
   - 起(或唤醒)`pipeline-tester`(命名 `qa`)测该批——首轮 qa 还不存在,由你起并交接(传批次计划文件路径);后续批次通过当前运行时的续跑消息唤醒它并保留上下文。
   - 让 `dev` 继续开发下一批,**不等待 qa 结果**(测上一批与开下一批并行重叠)。
3. qa 按「测试范围 = 该批核心任务 + 核心验收标准」做**快速检查**：只运行本批相关的快速命令和定向复现；全量命令只在最后一批/契约指定边界/终验运行一次。报告写 `tmp/pipeline/qa/<批次>.md`，只保留当前轮完整结果；`tmp/pipeline/qa/index.md` 每批一行记录 gate/轮次/报告路径/终验复核项。旧轮次只在批次报告末尾保留一行「轮次、gate、失败编号、处理结论」，不保留旧全文:
   - `gate: FAIL` → qa 把失败清单(复现步骤 + 实际 vs 预期)通过当前运行时的续跑消息直接发给 `dev`;dev 暂停手头批次、优先修复被打回的批次,修完后通知 qa 复测。打回循环在 dev ⇄ qa 之间直聊,你只收简报。复测走收敛范围(修复项 + 影响面 + 快速命令层),不整批重跑。
   - `gate: CONCERNS` → qa 停下等你;你把非阻断问题展示给用户裁决:**豁免 → 该批封版;不豁免 → 转交 dev 修复**。
     - **裁决必须落文件**:无论豁免与否,都要追加写入 `tmp/pipeline/rulings.md`(格式见下);跨会话续跑时以该文件为唯一事实源,避免多会话对同一问题给出不同结论。
     - rulings.md 单条格式:
       ```markdown
       ## 批次 <批次名> / 第 N 轮 / YYYY-MM-DD
       - 问题:简述
       - 建议裁决:修复 | 豁免
       - 实际裁决:修复 | 豁免
       - 理由:
       - 关联文件:
       ```
   - `gate: PASS` → 该批封版。
4. 进度可见性(只转述已有信息，不额外读文件):
   - **每批 gate 出结果后**:一行简报(批次名、总进度 N/M 任务、gate 结论、下一步);
   - **表格式快照**只在等待用户、预算预警、异常停止/打回到顶时发送；不要为正常进度制造额外交接。

并行期间的现场保护:qa 正在测某批时,`dev` 不得改动该批已交付的文件(收到打回再动);dev 写后续批次的半成品可能被 qa 的全量命令扫到——qa 会按文件归属分类,范围外报错不判 FAIL(见 tester 规则)。

你的职责只是:**轮次计数(单批到 3 轮仍 FAIL → 停止汇报,附对应批次报告)、CONCERNS 裁决、批次简报、监督角色是否失联/跑偏**。

回退预案:team 协作异常(消息丢失、角色反复跑偏)时,改用中转模式——每轮由你把对应批次报告的失败清单带给 developer,修复后再起 tester 复测。

### 第 4 步:按档位收口

- **fast**:调度员核对变更卡、变更文件与 developer 自测结果，输出不超过 8 行的完成摘要；不生成其他 artifact。
- **standard**:qa 读取 `qa/index.md`，汇总去重后的终验复核项，运行终验/全量命令一次并写 `qa/final.md`。若第 2 步创建过 guardian，唤醒同一 guardian 只读 plan、qa/index.md 和 qa/final.md 完成终验；否则调度员据这三份精简 artifact 收口。终验不过时由用户决定回第 3 步还是调整计划。
- **thorough**:同一 guardian 读取 plan、qa/index.md、qa/final.md 做整体终验，并写 `tmp/pipeline/acceptance.md`，列出仍须人工 check 的项及复现路径。

### 第 5 步:上线建议或自动提交

- **fast**:调度员在完成摘要中给出一条 Conventional Commits 建议；不创建 releaser。
- **standard + 人工执行**:调度员从 plan 和 qa/final.md 生成精简变更摘要、提交信息建议和人工待办；不创建 releaser、不生成 release-notes.md。
- **standard + 自动提交 / thorough**:创建 `pipeline-releaser`。人工模式只产出 `tmp/pipeline/release-notes.md`；自动提交模式按契约执行 `git add`/`git commit`(/`git push`)并记录结果。

### 闸口 2:提交与上线
fast 在完成摘要后清理 `.active` 即结束，不写 retro/state；以下只适用于 standard/thorough。先判断 PIPELINE.md ④ 节上线方式:
- **自动提交**:releaser 已自动完成 git 写操作;调度员落复盘、更新 state.md、删 `.active`。
- **standard + 人工执行(默认)**:调度员展示精简摘要与建议命令，**必须由人类执行 git 写操作**；没有 release-notes.md。
- **thorough + 人工执行**:调度员展示 release-notes.md 摘要，**必须由人类执行 git 写操作**；releaser 只产出建议命令。

standard/thorough 都先落复盘:把本次 run 的**既有信息**(转述,不额外读文件)写入 `tmp/pipeline/retro.md`。retro.md 固定头部(供 planner 下次校准用):

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

正文再写:每批打回轮次、CONCERNS 次数与裁决结果、自检缺装项、一行经验(偏差最大的是什么)。state.md 的 status 置 done。随后按本档实际存在的 artifact 展示摘要，不得尝试读取未生成的 release-notes.md 或 acceptance.md。

**闸口 2 默认由人类执行 git 写操作**,除非 PIPELINE.md 第 ④ 节声明「自动提交」。standard 人工模式把建议命令放在调度员摘要，thorough 人工模式放在 release-notes.md；调度员和任何角色都不得代用户执行。用户核对无误后手工执行,然后打勾追踪矩阵、择机部署——流水线到此结束。自动提交模式下,releaser 已按契约执行 git 写操作,调度员直接收尾。

standard/thorough 在闸口 2 明确提醒用户检查本档实际生成的 qa/final.md 或 acceptance.md 中的终验复核项、已知观察项和人工确认项，再进行部署或下一跑。

## 纪律

- 打回轮次上限是硬约束,到顶必须停下来汇报,不许「酌情放行」。
- 一个项目同一时刻只跑一条流水线:`tmp/pipeline/` 下的 artifact 是单例,并行触发会互相覆盖。
- 角色之间只传 artifact 文件路径 + 结构化结论(第 3 步 team 直聊除外),不转发长篇对话。
- 同一角色首次拿到完整 context packet 后后续批次用 follow-up 只发送「批次差量」(计划路径、变更文件、验收/测试变化)，不得重复冷启动或重灌相同背景。
- **git 写操作默认严禁**:commit/push/add/tag/reset/merge/deploy 一律是人类在闸口 2 的权限,除非 PIPELINE.md 第 ④ 节明确声明「自动提交」。若角色在人工执行模式下试图执行 git 写操作,调度员必须制止并把建议命令放入本档摘要或 release-notes.md；自动提交模式下由 releaser 按契约执行。
- 任何角色超时/崩溃,向用户报告当前阶段与已有产出,不要静默重试超过 1 次。
- state.md 维护:standard/thorough 在阶段切换、每批 gate 出结果、等待用户(闸口/裁决)、异常停止前更新；fast 正常流程不创建。
- 任何提前停止的分支,结束前都必须删除 `tmp/pipeline/.active`；state.md 已存在时置 aborted，fast 则写最小 aborted 现场。
