# pipeline-standard

![GitHub stars](https://img.shields.io/github/stars/wzm111/pipeline-standard?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)
![Last commit](https://img.shields.io/github/last-commit/wzm111/pipeline-standard?style=flat-square)

**用对话完成需求、开发、测试和交付，支持 Claude Code 与 Codex。**

pipeline 是一套由编码智能体执行的 skill 和本地工具。你描述需求，智能体明确范围与验收标准，按复杂度选择直接实现或必要的角色协作，并记录可核验的执行证据。项目差异由智能体维护的 `PIPELINE.md` 承载，无需先填写配置、选择流程档位或学习内部命令。

已发布：[v3.11.0：执行门禁、具体进度与任务耗时诊断](docs/releases/v3.11.0.md)。

## 三步开始

### 1. 安装

本机需要 Git 和 Python 3；仓库 CI 使用 Python 3.11。在终端执行，或让智能体协助安装：

```bash
git clone https://github.com/wzm111/pipeline-standard.git
cd pipeline-standard
bash install.sh
```

默认同时安装 Claude Code 和 Codex；仅安装一端可用 `bash install.sh --claude` 或 `bash install.sh --codex`。

安装器将 skill 链接到当前仓库，并为 Claude 准备角色和 hook 链接，请保留仓库目录。首次安装准备本机默认模型配置；重装保留已有选择。上述命令安装当前默认分支的内容，需要固定版本时可让智能体切换到指定发布标签。

### 2. 在业务项目会话中描述需求

**Codex：**

```text
$pipeline 给列表增加搜索功能，并验证空结果和清空搜索的行为
```

**Claude Code：**

```text
/pipeline 给列表增加搜索功能，并验证空结果和清空搜索的行为
```

缺少项目契约时，智能体识别项目并补齐 `PIPELINE.md`，保留已有规则；只询问无法推断且影响结果的关键信息。每个需求只做当前目标及必要配套，无关问题不自动加入开发或验收。

### 3. 用自然语言继续

| 你想做什么 | Codex 示例 |
| --- | --- |
| 查看具体步骤 | `$pipeline 列出已完成、正在执行、剩余步骤和阻塞` |
| 继续原任务 | `$pipeline 继续上次任务，复用仍有效的检查` |
| 配置角色模型 | `$pipeline 开发继承当前模型，测试使用……` |
| 查看设置 | `$pipeline 查看当前模型设置` |
| 接入已有项目 | `$pipeline 帮我接入项目` |
| 提交验收成果 | `$pipeline 提交已验收的改动` |
| 发布本次改动 | `$pipeline 发布这次已验收的改动` |
| 分析运行开销 | `$pipeline 扫描本项目的运行记录，找可优化点` |

Claude Code 将 `$pipeline` 换成 `/pipeline`。**提交、推送和发布按各自授权执行**；一次提交授权不会扩大为部署或修改其他项目。

## 执行时能看到什么

主会话在开始、阶段切换、重要检查结果和阻塞出现时反馈具体步骤。长操作通常每 60 至 90 秒给出有依据的简报；宿主长调用尚未返回时，不能保证实时推送。

例如，测试阶段的进度可以是：

> 🔵 **[进行中｜测试]** 当前：验证重新登录后的状态恢复。已完成：超时取消快检。
>
> 剩余：缓存刷新验证、完整回归。阻塞：无。下一步：根据定向验证结果进入完整回归。

| 标识 | 含义 |
| --- | --- |
| 🔵 **[进行中]** | 有实际动作或反馈依据，正在开发、测试、构建或审查 |
| ✅ **[已完成]** | 某个明确步骤已有成功结果，不代表整个交付完成 |
| 🟠 **[修复中]** | 正在修复已确认的问题，并说明随后要补的检查 |
| ⏳ **[等待中]** | 等待具体命令、角色或外部结果 |
| 🔴 **[需你处理]** | 必须由用户操作，并给出恢复后的验证标准 |
| 🔎 **[待核实]** | 反馈不足或状态冲突，不能直接判断正常或卡死 |

v3.11.0 提供只读 `progress` 摘要，整理当前任务、检查与角色、登记成功项、剩余项、阻塞和下一步，并区分最近结果时间与状态更新时间。登记成功不等于证据当前仍有效；`running` 也不单独证明宿主健康。没有记录的步骤、耗时或实际模型保持未知，不编造百分比或 ETA。

**进度脚本不会自行发送聊天消息，需由实际执行会话采用。** 旁观监听默认只报有意义的变化，不能代替执行者的过程反馈。格式和调用方式见[可见进度规范](skills/pipeline/references/progress.md)。

## 需求如何推进

```mermaid
flowchart TD
    A["描述需求"] --> B["明确范围、风险与验收标准"]
    B --> I{"选择执行方式"}
    I -->|简单任务| J["当前会话直做"]
    I -->|复杂任务| K["必要角色协作"]
    J --> C["开发与验证"]
    K --> C
    C --> D{"必要评审和验收通过？"}
    D -->|通过| E["完成摘要"]
    D -->|未通过且未达上限| G["修复失败项并定向复验"]
    G --> D
    D -->|达到上限或需裁决| H["暂停并报告阻塞，等待裁决"]
    E --> F["授权范围内交付"]
```

| 档位 | 适用场景 | 执行方式 |
| --- | --- | --- |
| fast | 范围明确、低风险、可定向验证的小改动 | 当前会话直接实现和自测 |
| standard | 日常功能、修复和优化 | 简单任务直做；复杂开发派必要的 developer 与独立 tester |
| thorough | 跨系统、高风险或明确要求完整审查 | 完整规划、范围评审、开发、QA 与必要人工闸口 |

智能体自动选档；用户指定的档位仍生效，发现风险可以升级，不能自行降低要求。复杂度看模块耦合、多入口一致性和验证需要，不只看文件数。单批测试打回最多 3 轮，范围评审最多 2 轮，到顶停报。纯发布门禁失败时报告阻塞，不自动扩展为开发修复。

### 哪些规范由工具校验

管理型运行通过已接入的脚本入口确认当前规则版本、核验真实检查证据，并在收口时复核完整性。不能仅写一份 PASS 报告代替验收；输入、环境、日志或后续失败使旧证据失效时，不能继续放行。旧运行保留兼容，但不能据此认定已采用新规则。

规则确认只记录调用方声明，不证明模型已理解或执行了所有文字规范。聊天反馈、范围判断及原生工具调用仍需智能体遵守；已接入入口的门禁不等于能拦截任意 shell 或宿主操作。详见 [skill 入口](skills/pipeline/SKILL.md)与[双端兼容说明](docs/COMPATIBILITY.md)。

## 如何减少重复工作

| 做法 | 实际行为 |
| --- | --- |
| 先查关键边界 | 已声明的 quick 检查须有效，才运行重型终验；保留必要完整回归 |
| 复用有效证据 | 核对命令、目录、输入、环境、日志和来源，只补失效或缺失项 |
| 减少交接漂移 | 角色续用已有上下文；同进程完成检查、QA 与可选封存，收口前先查停止和等待条件 |
| 保留真实阶段 | 显式绑定的主流程角色在宿主接受后同步阶段；所选角色全部返回后再交接 |
| 承接已验收交付 | 提交或发布沿用同一成果；必要现场预检、产物身份和上线冒烟仍执行 |
| 有界处理异常 | 断连先核对原执行和候选状态，不盲目重部署；必须人工操作时保留具体待办 |

角色并行不能相互覆盖构建目录、浏览器或其他共享资源。源码、依赖、环境、产物或目标状态变化后的必要复测保留；没有可靠复用入口时，不猜“跳过构建”参数。

这些机制不承诺固定提速或 token 降幅。实现与本地测试通过，不等于其他会话已采用，也不等于业务项目已获得同样收益。已有真实观察见 [2026-10-28 记录](docs/FIELD-OPTIMIZATION-20261028.md)；检查与交付规则见[检查计划](skills/pipeline/references/check-plan.md)和[交付承接](skills/pipeline/references/delivery.md)。

## 运行日志与后续优化

经本地状态接口执行时，在启动、恢复和结束等生命周期节点写入 `tmp/pipeline/runs/<run_id>/audit.json` 安全摘要。原状态和检查证据保留在对应 run 目录；未接入的历史任务或原生调用不能假定已自动采集。

摘要记录阶段与等待、检查执行/复用/继承、失败分类及任务和角色统计。v3.11.0 增加按任务划分的耗时摘要：检查、操作和等待区间取并集，不把并行时间重复相加；**未归因时间不等于浪费或排队**。未知模型、token 和缺失历史保持未知。

分析时先用只读 `scan` / `audit`，默认不打开原始业务日志、不修改历史、不重跑检查。安全摘要不保存凭据、用户目标正文、客户数据或环境原文；原始输出仍需项目自身脱敏。摘要仅供诊断，不是 QA、复用或发布凭证。详见[运行记录](skills/pipeline/references/audit.md)。

记录默认不提交 Git；清理 `tmp/pipeline/` 会删除本地记录。长期归档只保留已确认脱敏的摘要，不打包整个运行目录。

## 角色模型与运行边界

模型优先级为：**本次指定 → 项目配置 → 本机默认 → skill 模板 → 运行时默认**。

- 项目配置为 `pipeline.models.json`；本机默认位于 `~/.config/pipeline-standard/models.json`，升级保留已有选择。
- 新装模板先继承当前会话模型；Claude 与 Codex 可分别设置，也可按任务类型路由。
- 配置只在实际委派时应用，不能切换当前主会话的界面模型；请求参数不等于宿主确认的实际型号。

详见[模型设置](skills/pipeline/references/models.md)和[任务路由](skills/pipeline/references/model-routing.md)。

| 运行时 | 入口 | 执行边界 |
| --- | --- | --- |
| Claude Code | `/pipeline` | 原生角色与 team；项目接入后可通过 `PreToolUse` hook 拦截受支持工具的越界写 |
| Codex | `$pipeline` | 子 agent 与 follow-up；按项目契约做写前校验，使用本地归属和检查接口 |

Codex 的流程校验不能称为与 Claude 同层级的 hook 硬拦截，Bash hook 也不是完整沙箱。没有原生子 agent/hook 的工具可参考[单 agent 模板](templates/single-agent-pipeline.md)，不能宣称具备角色隔离。同项目只运行一条流水线，中断后先核对执行者和归属，不自行删除未知锁。

## 更新与已有会话

更新仓库后，已安装的软链接指向新的检出内容；需要同步新增角色或 hook 时再运行安装器。本机模型选择不会因重装被覆盖。

已有会话不会自动刷新上下文，可让执行者：

> $pipeline 请重新读取本机最新版 SKILL.md 及本次需要的引用文件，保留当前任务和有效验收证据，按最新版继续，显示具体步骤，不重复已完成的工作。

已有子角色在后续交接时同步新规则。若宿主仍未发现 skill 更新，再重启应用或新开会话；无需为每次更新重新创建任务。

## 维护入口

| 需要了解什么 | 入口 |
| --- | --- |
| 流程与直通命令 | [SKILL.md](skills/pipeline/SKILL.md)、[standard](skills/pipeline/references/standard.md) |
| 项目接入 | [对话接入](skills/pipeline/references/interaction.md)、[init-project.sh](init-project.sh) |
| 证据与浏览器验收 | [证据复用](skills/pipeline/references/evidence.md)、[浏览器交接](skills/pipeline/references/browser-handoff.md) |
| 状态与中断恢复 | [运行接口](skills/pipeline/references/operations.md) |
| 进度与安全摘要 | [进度规范](skills/pipeline/references/progress.md)、[运行记录](skills/pipeline/references/audit.md) |
| 版本与建设进度 | [发布说明](docs/releases/)、[实施记录](docs/IMPLEMENTATION.md)、[路线图](docs/ROADMAP.md) |

维护者在仓库根目录运行 `bash tests/run-all.sh` 做本地隔离回归。真实模型、业务验收和实际提速需要对应环境的证据，本地测试不能替代。

## License

MIT
