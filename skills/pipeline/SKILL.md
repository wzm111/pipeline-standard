---
name: pipeline
description: 对话式研发流水线：描述需求即可按风险开发、测试与验收；也可初始化项目、配置角色模型、查看设置或继续上次任务。Claude Code 用 /pipeline，Codex 用 $pipeline；项目契约由智能体维护。
---

# 研发流水线编排

用户显式输入 Claude Code 的 `/pipeline <任务描述>` 或 Codex 的 `$pipeline <任务描述>`。简单任务由当前 agent 直接执行；复杂任务自动选择必要角色并应用已配置的角色模型，不因属于standard就一直直做。thorough 使用完整角色链。

## 运行时

- Claude Code 使用已注册的 `pipeline-*` agents、team 直聊与 `PreToolUse` hook。
- Codex 必须先读 [Codex 运行时说明](references/codex.md)，使用子 agent 与 follow-up；写入前对照 `PIPELINE.md` ⑤ 节校验，不得宣称具备 Claude 的机器级 hook。
- 两端共用档位、artifact、打回上限和 git 边界。Codex 的全局安装、外部 clone 与项目外写入始终先请求授权。

## 先识别用户意图

用户不需要了解参数、角色或配置文件。支持自然语言及原有 flags：
- 初始化/帮我接入、配置模型/换模型/恢复继承、查看设置/帮助：读 [对话操作](references/interaction.md)，必要时读 [模型配置](references/models.md)。这些操作不启动研发流水线、不创建 .active；已有任务的锁不移除。
- 继续上次、进度怎么样、停止、修复中断：读 [运行与恢复](references/operations.md)。只读状态不启动新 run；明确续跑不重复询问。
- 提交/发布刚完成的需求：按[交付承接](references/delivery.md)沿用验收、角色和脚本；已完成run通过delivery_from承接，不重新按开发需求规划和全量验收。实际部署先做现场预检和真实运行器快检；支持候选/激活分离的项目使用其发布控制入口，失败有界恢复、上线后一次收口，不把搜索收录等外部结果混入上线时长。远端断连或取消后先核验原执行与激活状态，禁止直接重跑；取消构建必须同时阻断后续激活，不能仅杀子进程。实际git/上线动作仍限用户授权。
- 开发需求：直接进入下方流程。简单任务自动 fast，其余 standard，真实高风险 thorough；不让用户先选档、预算或模型。项目没有配置时使用安装器生成的本机默认；旧安装缺本机文件时用defaults/models.json模板兜底，不发起设置问卷。
- 全流程只问无法从项目推断且影响结果的关键问题，每轮尽量一个；给简短建议，接受自然语言回答。低风险已授权动作直接执行，保留必要风险闸口。使用“已完成、剩余、需要你决定”说明进度，不要求用户打开内部报告找答案。
- 进行中收到进度、模型或其他插问时，简短回答后继续原任务，保留原run和未完成验收；只有明确取消或不兼容的新目标才替换。必须由用户操作才能继续时，按operations.md登记具体动作和验证标准后进入human_wait，不以一句答复遗留无人执行的execution状态，也不把用户回应直接当验收通过。
- 每次需要创建子agent前，都按models.md运行scripts/resolve-models.py解析当前运行时的角色设置；即使项目没有pipeline.models.json也不能跳过。优先级为本次 > 项目 > 本机默认 > skill模板 > 运行时默认。记录每个角色来源；配置模型不强制新增角色，主会话直通不会切换模型。
- 角色含task_routes时，按[任务模型路由](references/model-routing.md)利用已有范围判定任务类型/风险/复杂度，交给解析器选择；仅明确low/simple可走轻量路线，复杂或不明情况保留基础模型，不额外创建分类角色。

## 启动与路由

1. 读取项目根 `PIPELINE.md`。不存在或关键字段仍是占位符时，按 interaction.md 自动接入并继续原需求；不把填写模板交给用户。已有契约只补必要缺项，保留用户规则。
2. 若 `tmp/pipeline/.active` 存在或 state 显示未完成，先处理续跑/新开，禁止同项目并行两条流水线。有Python时标准直通优先使用scripts/pipeline_cli.py的paths/start/check/report/finish（例见standard.md），不为调用接口读取内部源码。读契约并确认锁归属后尽早start，再做业务扫描，使准备阶段进入真实计时。保存start返回的私有receipt路径，不打印其内容或自动认领别人的active；复杂状态/恢复才读[本地状态接口](references/operations.md)。真实状态位于runs/<run_id>.json，不并行维护state.md。无Python保留旧格式；收口确认执行者停止后只清自己的标记。
   阶段或执行/测试/人工等待/角色等待/交接实际改变时更新stage/activity，脚本自动记事件；查询和监控不制造事件，不反推旧记录缺失的耗时。
3. 用户显式档位优先；未声明时按风险选择并在首条简报说明。只能自动升级，不能自动降级：
   - **fast**：单个内聚、可定向验证的低风险改动；无新依赖、迁移、公开 API/数据结构、权限/支付/安全、部署配置、自动提交；范围和验收明确。文件数量不作为硬门槛，关联测试与样式可同批修改。只读 [fast 流程](references/fast.md)。
   - **standard**：默认。按[standard 流程](references/standard.md)先判断执行方式：范围清楚、可内聚验证的任务直做；跨模块耦合、多入口一致性、复杂排错或需多维独立验证的任务自动委派必要角色。复杂度与风险分别判断，不能把“非高风险”当作“简单”。进入 QA 时再读 [QA 循环](references/qa-loop.md)。
   - **thorough**：>10 个任务、预计 >半日、跨系统/高风险，或用户要求完整审查。只读 [thorough 流程](references/thorough.md)；进入 QA 时再读 [QA 循环](references/qa-loop.md)。
4. fast 发现任何风险信号、复杂度超出内聚定向验证或需要自动提交时，升级 standard（高风险升级 thorough），角色按新档位按需选择；thorough 的闸口 1 永不自动跳过。角色选择由智能体完成，沿用现有模型解析与派发接口，不增加配置问卷或分类agent。进行中的任务保留已有成果，只委派剩余工作或必要独立验收，不为换策略重做整轮。

## 一次生成、差量复用的上下文

fast/standard 在当前会话执行时，直接复用已读上下文；只记录最小 state（standard）和结束 metrics，不生成 base/run/角色差量文件。首次委派时才建立下述交接包。每次写入仍对照已读的 ⑤ 节校验范围；契约变化或上下文丢失时重新读取。

调度员读取 `PIPELINE.md`、项目根 `AGENTS.md`/`CLAUDE.md`(存在时)及相关基线切片，生成稳定的 `tmp/pipeline/context/base.md`；本次任务另写 `context/run.md`：

- base：来源文件路径与内容指纹、需求/规范定位、可写范围与禁区、快速/终验测试层、上线模式。run：run_id、档位、任务摘要、预算与开始时间。
- 若已有 base 的来源指纹全部未变，直接复用；不得重新概括。旧契约未分测试层时，安全可过滤的 lint/单测归快速层，其余声明命令归终验层，不暂停补契约。
- 有Python且确需交接包时，用[上下文缓存](references/context.md)的lookup检查来源；unchanged仅传已有reference与本批差量，不重新读正文或生成摘要。新角色或上下文丢失时显式load一次；缓存存在不等于该角色已加载。当前会话直通且上下文仍在时不为缓存另造交接文件。
- 每个角色只接收 base、run 路径和 `context/<role>.md` 差量(当前批次、artifact、验收变化)。同一角色后续批次只发差量，不重复冷启动或转发长对话。
- 角色只有在 base 缺字段、指纹失配或发现冲突时才打开原文；developer 每次写入前仍须复核 `PIPELINE.md` ⑤ 节，releaser 执行 git 前仍须复核 ④ 节。
- 新产生的模型、QA和验收报告必须绑定本run及记录时间；实际路径放交接包，不能信任共享目录里遗留的同名文件。生成报告、QA启动或复用检查时读[证据与复用](references/evidence.md)，优先用run_evidence.py保存与核验，不为fast新增原本不需要的报告。
- 普通前台检查经run_checks.execute_check统一执行；范围明确时impact_covered=True，自动先查可复用证据再决定是否执行，不能只在prompt里说“复用”后仍直接重跑命令。浏览器等专用工具按evidence.md记录。实际委派须在[角色派发边界](references/role-dispatch.md)调用begin→宿主工具→accepted，工作结束再complete；已注册未闭合派发不能finish。不为统计增加角色或必填配置；当前会话全程直做时收口confirm_direct，历史缺失不倒填。
- 多项验收或开发/QA交接时，按[检查计划](references/check-plan.md)让执行与required_checks使用同一声明，避免轮次标签漂移；简单单项检查沿用原入口，不增加用户配置。
- 读取不确定的内部路径前先窄范围定位（如rg --files或运行时等价工具）；角色目录按skill真实路径解析，不反复猜目录/通配符。当前上下文已读且未变的文件/行区间直接沿用，补丁核对优先读git diff；确需的新切片合批读取，多语言核对提取相关键而非整包反复输出，不为此新建读取索引。上下文丢失或来源变化时重读所需部分；搜索无匹配与路径错误分别处理。

## 按需加载

- 当前命令所需 CLI 缺失，或当前批次确实需要但缺少设计 skill 时，才读 [依赖与 skill 自检](references/dependencies.md)。未实际使用的工具不探测、不安装。
- 仅在续跑、异常、CONCERNS 裁决或契约启用 webhook 时读 [运行与恢复](references/operations.md)。正常 PASS 流程不加载它。

## 轻量历史与度量

- 裁决使用 `tmp/pipeline/rulings/index.md` + `rulings/<run-id>.md`。启动时只搜索 index 中与当前需求编号/文件/问题签名匹配的行；无匹配不读历史正文。
- 每次结束向 `tmp/pipeline/metrics.tsv` 追加一行：`run_id profile tasks batches agents context_bytes test_seconds retries human_wait_seconds wall_seconds token_usage status`。只记录运行时实际提供的数据；token 不可见时留空，禁止估算。
- planner 只读取 metrics 最后 5 条同档记录和最近一次 retro。至少 3 条时用中位数与 P80 给 ETA；不足时按阶段给宽区间并标记低置信度，不再使用固定“每任务几分钟”系数。

## 不可变边界

- 单批测试打回最多 3 轮，范围评审最多 2 轮；到顶停报，不得酌情放行。
- 默认禁止 git 写操作与部署；契约 ④ 节明确自动提交或用户当次明确授权时才执行对应动作，不把提交授权扩展为推送/发布/部署。已有用户授权不要求再手填契约。
- 角色之间只传 base/run 路径、差量路径、artifact 路径和结构化结论。
- 角色超时/失联只重试一次；仍失败时保存现场并报告用户。失联不等于停止，确认执行者停止及锁归属后才清理自己的 `.active`。
