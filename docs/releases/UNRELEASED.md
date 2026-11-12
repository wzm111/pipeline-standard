# main 开发更新

以下更新已同步到 `main`，尚未创建新版本标签或 GitHub Release；当前已发布稳定版仍为 `v3.13.0`。这份文档是开发分支更新记录，不是新版本发布说明。代码的本地回归通过，真实宿主的完整采用仍有待验项，不能据此认定所有会话已遵循规范。

## 本次变化

- **跨 Agent 交接**：在同一项目或 worktree 内，新 Agent 可凭 run ID 用 `handoff-read` 读取已保存的任务、阶段、下一步和证据引用，即使原 Agent 没来得及补写摘要。主调度员可用 `handoff-save` 补记关键决策，较长详情按需读取。运行事实由脚本保存，无需调用模型。未记录的决策无法恢复，读取交接也不会转移运行权限。

- **页面任务的视觉证据链**：沿用项目已有设计，自动选用适用且已可用的设计 skill；分别登记实际截图、查看和复核。源码、图片或来源变化后要求重新核验，修复循环保留原问题与历史。重要或较大改动按风险复用独立 tester，不为每个小页面强制增加角色。
- **视觉 QA 与交付门禁**：显式 UI 要求参与 single、plan、release QA 和 finish；缺图、未看、未复核或失效证据不能当作完整通过。显式交付承接携带不可变视觉引用，合法替代保留原范围和未解决问题。
- **原生工具的多项检查计划**：`observed_ref` 绑定已完成的正式宿主观察，纯原生或与命令检查混合时使用同一计划。保留当前任务、完整输入、宿主身份、日志、覆盖和最新失败校验，不编造新一次执行或耗时。严格等价的新有效观察计划可承接原检查义务；旧失败仍保留，未解决的 source QA 结论不自动豁免。
- **发布与检查复用**：新增稳定的发布模块薄 CLI，接通 managed release QA；前置关键交互和真实就绪检查，明确候选终验前冻结版本与元数据。既有发布脚本、候选身份和上线冒烟要求继续保留。
- **入口与文档**：`paths` 支持真实资源发现与 `--project`，缺失资源如实报告。补充无计划直做的进度结果、改前 baseline、同进程 verify 收口；中英文 README 同步开发/修复循环、视觉验收与独立发布流程图，新增合成页面试用。

- **回归定位与工单核验**：对照可验证的正常和异常版本定位回归区间。收到外部功能工单时，先沿实际入口核对已有覆盖和启用条件，再确认开发缺口。

详细说明：[视觉验收](../../skills/pipeline/references/visual.md)、[检查计划](../../skills/pipeline/references/check-plan.md)、[交付承接](../../skills/pipeline/references/delivery.md)、[独立试用](../TRY-PIPELINE.md)。

## 验证与已知限制

- 跨 Agent 交接在 `9caa32a` 的 [macOS 和 Ubuntu CI](https://github.com/wzm111/pipeline-standard/actions/runs/42941919829) 均通过。覆盖无手写交接时读取已落盘状态，跨宿主实测另行确认。
- 视觉与发布流程的原始验收记录：本地 45 套 Python 测试共 608 项通过，另有 Codex 隔离安装冒烟、Shell 语法、文档链接和流程图结构检查。原版本准备提交 `81187d1` 的 GitHub Ubuntu/macOS CI 已通过；本次文档调整的目标提交仍单独核验，不用旧提交结果冒充新提交通过。
- 首次全新 Codex 页面试用实际采用设计能力，并取得三个视口的截图、查看和复核；收口暴露的原生检查计划接口缺口已修复并经独立探针核验，原失败 run 不改写。
- 第二次全新试用遇到浏览器自动审批拒绝本地 `file://` 入口，未绕过，缺少截图的 QA 保留 `CONCERNS`。修复后的完整原生浏览器端到端采用尚未确认。
- **进度格式仍可能遗漏。** 只读观察与 `progress` 摘要不会自行发送或逐条纠正聊天消息；读取 skill、policy ack 或脚本门禁均不能证明每条消息已合规。
- 截图、查看和审美复核仍依赖实际观察者，机器核验的是声明与证据绑定，不是审美质量认证。Claude 原生账号试用暂缓，Mermaid 实际渲染未验证；实际模型、token 和纯排队时间缺数据时保持未知。
- 诊断记录仅在项目本地保存与预览，不上传运行目录、私有 receipt、凭据或客户数据。不承诺固定提速或 token 降幅。

## 更新与试用

试用这些未发布更新时使用 `main`，完整安装继续使用仓库根 `bash install.sh`；目前没有可检出的新 RC 标签。更新后，已有聊天需重新读取最新版入口及本次适用引用，保留原任务与仍有效的检查；不要求重做已通过的工作。旧状态保留兼容，不倒填采用记录。

需要稳定版时继续使用 `v3.13.0`；开发分支试用请从合成任务开始，并明确区分本地检查、实际宿主采用和业务验收。完整验证补齐后再考虑正式版本发布。

## English summary

These are **unreleased main-branch updates**, not a new tag or GitHub Release. Stable `v3.13.0` remains unchanged. The updates add cross-agent recovery from persisted run state, optional handoff notes, explicit visual evidence and QA gates, native `observed_ref` check plans, managed release integration, resource discovery, and bilingual workflow diagrams. This update also adds version regression isolation and checks for existing functionality before implementing feature requests. Local tests pass; complete native-host adoption is still unconfirmed. Progress formatting is not enforced at message-send time, and observer-declared reviews are not machine aesthetic certification. Diagnostics remain local; no run-data upload service is introduced.
