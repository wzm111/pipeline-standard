---
name: pipeline
description: 端到端研发流水线:澄清 → 拆解 → 范围评审 → 批次级开发⇄测试(team 直聊,打回≤3轮/批) → 验收 → 上线建议。当用户输入 /pipeline <任务描述> 时使用。需要项目根存在 PIPELINE.md 契约文件。
---

# 研发流水线编排

用户输入了 `/pipeline <任务描述>`。你是调度员,按以下固定流程用 Agent 工具调度角色 subagent(角色名见各步),不要自己干活,只编排与传递 artifact 路径。

## 前置

1. 读项目根的 `PIPELINE.md`。
   - 不存在 → 停止,告诉用户「本项目还未接入流水线」,契约模板见 pipeline-standard 的 `templates/PIPELINE.md`,等其补齐后再来。
   - 存在 → 记住其中声明的需求基线、规范、测试命令、禁区,后续每个角色的 prompt 里都要带上「先读 PIPELINE.md」。
2. 运行标记与断点:
   - `mkdir -p tmp/pipeline && touch tmp/pipeline/.active`(启用禁区硬拦截)。**流程无论以何种方式结束(完成/打回到顶/用户中止),都要 `rm -f tmp/pipeline/.active`**,否则会影响普通会话的写操作。
   - `tmp/pipeline/state.md` 存在且 status 非 done → 上次 run 未完成,向用户确认「续跑(现场在 state.md)还是新开」;新开 → 旧 state.md 改名 `state-<日期>.md` 留档。随后初始化本次 state.md(status / stage / batches / pending / next 五要素),示例:

     ```markdown
     status: running
     stage: gate-1
     batches: A,B,C
     pending: A
     next: 等待用户确认闸口 1
     ```
   - `tmp/pipeline/rulings.md` 存在 → 读入已有裁决记录;续跑时遇到相同批次/相同问题,直接按文件结论执行,不再重复询问用户。
3. 规模分流(依据任务描述预判,拿不准就问用户):
   - **小任务**(预计改动 ≤2 个文件、无新依赖、不涉及新目录):走快速通道——跳过第 2 步范围评审,其余不变。
   - **标准任务**:走全流程。
4. 工具自检(契约声明的外部 CLI 缺失时自动安装):
   - 扫描 PIPELINE.md ③ 测试命令里引用的外部 CLI,逐个 `command -v <cli>` 探测;已装 → 过
   - 缺失 → 告知用户并按 [tools/quick-install.md](../../tools/quick-install.md) 自动安装,装完跑 `--help` 验证可用
   - 速查表没有的 CLI → 尝试 `npm install -g <同名包>`,失败按降级处理
   - 契约声明的是**项目 devDep 型**工具(如 playwright、@axe-core/playwright)→ 不全局安装,提示该项目应自行 `npm i -D` 并接入 npm scripts,本轮跳过对应项并在 qa-report 说明
   - 安装失败(断网/权限不足)→ 明确告知用户,并在派发给 tester 的 prompt 里注明「该工具本轮不可用,跳过对应扫描项,在 qa-report 说明」,**不因此中断流水线**(核心测试命令不受影响)
   - 契约没声明的工具不装,不做多余动作
5. skill 自检(契约 ② 声明了设计类 skill 时):
   - 探测 `~/.claude/skills/<name>/` 与项目 `.claude/skills/<name>/` 是否存在;存在 → 过
   - 缺失且契约附了 git 来源 → 克隆到临时目录后安装(兼容两种仓库布局):
     - 仓库根有 `SKILL.md` → 整体拷到 `~/.claude/skills/<name>/`
     - 否则查仓库内 `.claude/skills/<name>/` 子目录 → 拷它到 `~/.claude/skills/<name>/`
     - 两种都没有 → 按安装失败处理
   - **拷贝安装前先做安全扫描**:Grep 克隆内容(SKILL.md 及附带脚本)的可疑模式——`curl`/`wget` 管道执行、`base64 -d`、向声明外域名外发数据、读写 `~/.claude/skills/<name>/` 之外的路径、访问 `~/.ssh`/`~/.aws` 等敏感目录。命中 → 不安装,告知用户「该 skill 内容可疑,请人工审查后手动安装」,按降级处理
   - 缺失且无来源/安装失败 → 告知用户,并在派发给 developer 的 prompt 里注明「该 skill 本轮不可用,降级按基线与规范处理」,**不因此中断流水线**
6. 通知通道(契约声明了通知 webhook 时):URL 从环境变量读(如 `FEISHU_WEBHOOK_URL`),未设置 → 静默跳过不阻断。三个触发时机各发一条纯文本(curl,内容=项目目录名 + 当前阶段 + 在等用户做什么):①闸口 1 等待确认;②CONCERNS 待裁决;③打回到顶或闸口 2 完成。除此之外不刷屏。

## 流程

### 第 1 步:澄清 + 拆解(Agent B)
调 `pipeline-planner`,传入用户任务描述:
- 返回**待澄清问题** → 把问题(含建议答案)展示给用户,收集回答后交给 planner 再拆。
- 返回**计划**(`tmp/pipeline/plan.md`;按批次组织时另有 `plan-<批次>.md` 详情文件)→ 进闸口 1。

### 闸口 1:计划确认(人工)
把计划摘要展示给用户,**必须附带规模信息**:任务总数、批次数、预估时长量级(由 planner 写在计划头部);展示时同时说明**墙钟时间**与**人力时间**两种口径,方便用户判断排期与 token 投入。里程碑级计划同时展示 planner 的「拆分建议」(本次跑哪几批、其余如何分次跑),由用户决定整体跑还是切片跑。附一句:可为本 run 设上限(时长或批次数),不设则不限;设了就记入 state.md,此后表格式快照对照预算,≈80% 时主动预警并请示继续/收口。**等用户明确确认后才继续**。用户有修改意见时,把意见交给 `pipeline-planner` 修订 plan.md 后再次展示确认——调度员不直接改计划。

### 第 2 步:范围评审(Agent A,≤2 轮;小任务跳过)
调 `pipeline-scope-guardian` 评审 plan.md:
- 通过 → 下一步
- 打回 → 把问题清单带回 `pipeline-planner` 修订后再审;2 轮仍不过 → 停止,向用户汇报分歧点,人工裁决。

### 第 3 步:开发 ⇄ 测试(批次级循环 + team 直聊,打回 ≤3 轮/批)

计划按批次组织时逐批循环;无批次的计划视为单批(=整体),流程相同。首轮交接经由你,打回循环让两个 teammate 直接对话(SendMessage 会唤醒已完成的对端续上下文)。

每一批次:

1. `dev` 开发该批:任务 checkbox 逐个勾选,批次收口按契约测试命令自测全绿 → 向你简报「批次 X 交付」。
2. 你收到交付简报后**两件事同时做**:
   - 起(或唤醒)`pipeline-tester`(命名 `qa`)测该批——首轮 qa 还不存在,由你起并交接(传批次计划文件路径);后续批次直接 SendMessage 唤醒它续上下文。
   - 让 `dev` 继续开发下一批,**不等待 qa 结果**(测上一批与开下一批并行重叠)。
3. qa 按「测试范围 = 该批任务 + 验收标准」实测,报告写 `tmp/pipeline/qa-report.md`(`batch:` 行注明批次):
   - `gate: FAIL` → qa 把失败清单(复现步骤 + 实际 vs 预期)直接 SendMessage 给 `dev`;dev 暂停手头批次、优先修复被打回的批次,修完 SendMessage qa 复测。打回循环在 dev ⇄ qa 之间直聊,你只收简报。复测走收敛范围(修复项 + 影响面 + 快速命令层),不整批重跑。
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
4. 进度可见性(两档,都是把已有信息转述给用户,不额外读文件):
   - **每批 gate 出结果后**:一行简报(批次名、总进度 N/M 任务、gate 结论、下一步);
   - **表格式快照**(批次状态表 + 实测节奏 + 修正 ETA),三个时点:①dev 批次完成过半;②dev 全批交付、进入整体测试;③异常停止/打回到顶的现状汇报(纪律第 4 条的场景)。

并行期间的现场保护:qa 正在测某批时,`dev` 不得改动该批已交付的文件(收到打回再动);dev 写后续批次的半成品可能被 qa 的全量命令扫到——qa 会按文件归属分类,范围外报错不判 FAIL(见 tester 规则)。

你的职责只是:**轮次计数(单批到 3 轮仍 FAIL → 停止汇报,附最新 qa-report.md)、CONCERNS 裁决、批次简报、监督角色是否失联/跑偏**。

回退预案:team 协作异常(消息丢失、角色反复跑偏)时,改用中转模式——每轮由你把 qa-report.md 的失败清单带给 developer,修复后再起 tester 复测。

### 第 4 步:验收(Agent A)
`pipeline-scope-guardian` 对照 plan.md(及批次详情文件)验收标准与 qa-report.md 做最终验收(关键验收点亲自抽查代码)。不过 → 向用户汇报,人工决定回第 3 步还是调整计划。

### 第 5 步:上线建议(Agent E)
`pipeline-releaser` 产出 `tmp/pipeline/release-notes.md`(变更摘要 + Conventional Commits 建议 + 人工待办)。

### 闸口 2:提交与上线(人工)
先落复盘:把本次 run 的**既有信息**(转述,不额外读文件)写入 `tmp/pipeline/retro.md`。retro.md 固定头部(供 planner 下次校准用):

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

正文再写:每批打回轮次、CONCERNS 次数与裁决结果、自检缺装项、一行经验(偏差最大的是什么)。state.md 的 status 置 done。随后展示 release-notes.md 摘要 + retro.md 摘要。**删除运行标记 `rm -f tmp/pipeline/.active`**。

**闸口 2 必须由人类执行 git 写操作**:releaser 只产出 release-notes.md,其中会写明建议的 `git add`/`git commit`/`git push` 命令;调度员和任何角色都不得代用户执行这些命令。用户核对无误后手工执行,然后打勾追踪矩阵、择机部署——流水线到此结束。

## 纪律

- 打回轮次上限是硬约束,到顶必须停下来汇报,不许「酌情放行」。
- 一个项目同一时刻只跑一条流水线:`tmp/pipeline/` 下的 artifact 是单例,并行触发会互相覆盖。
- 角色之间只传 artifact 文件路径 + 结构化结论(第 3 步 team 直聊除外),不转发长篇对话。
- **任何角色(含 releaser)严禁执行 git 写操作**:commit/push/add/tag/reset/merge/deploy 一律是人类在闸口 2 的权限;若角色试图执行,调度员必须制止并提醒"请把命令写进 release-notes.md,由人工执行"。
- 任何角色超时/崩溃,向用户报告当前阶段与已有产出,不要静默重试超过 1 次。
- state.md 维护:阶段切换、每批 gate 出结果、等待用户(闸口/裁决)、异常停止前都要更新(五要素,转述既有信息不额外读文件)。
- 任何提前停止的分支,结束前都必须删除 `tmp/pipeline/.active` 并把 state.md 置 aborted 写明现场——续跑靠它自恢复。
