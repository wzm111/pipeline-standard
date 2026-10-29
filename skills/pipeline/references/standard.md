# standard 流程

下文 state.md 是旧格式名称；使用 run_state.py 时统一写对应 runs/<run_id>.json，不维护双份状态。结束步骤必须先确认执行者停止和本run归属，再调用finish；不能为满足“清理”而删除未知活动锁。

同一需求的提交/发布不重启本流程：按delivery.md承接已通过的验收、检查和浏览器脚本，仅验证本次变化与上线产物。检查调用execute_check时，确认影响范围后启用自动复用；QA结果用write_report落盘，使finish能自动封存交付凭证。

## 先选择执行方式

依据已有需求与必要的窄范围定位判断复杂度，不为分类创建agent或新报告；在首条简报或现有state.next里用一句话说明原因及选用角色。文件数量、任务标题、检查命令数量或等待外部结果的时长都不能单独决定复杂度。仅涉及多个文件或少量已知调用点、仍可内聚定向验证的小改动继续直做。

| 任务特征 | 自动选择 |
| --- | --- |
| 范围明确、单个内聚改动、已有定向验收；普通文案/样式/小修复 | 主会话直做，不创建角色 |
| 需协调多模块约束、多入口共享状态/隐含上下文，或局部用例不足以覆盖复杂边界 | developer实现，产物冻结后tester独立验收；主会话编排 |
| 根因不明且需多条假设交叉验证的复杂排错 | developer定向诊断；涉及修复时由tester独立核验复现与影响面 |
| 全面审查、多个相互独立的检查方向 | 按检查方向派tester或scope_guardian，主会话整合；没有实现工作不强造developer |
| 有架构取舍、依赖或方案顺序需要先厘清 | 先planner，再安排必要的开发/QA；真实跨系统或高风险升级thorough |

复杂任务必须在实际开始对应工作前调用所选角色，不能只在简报里列角色名后仍由主会话包办；普通复杂开发不因此机械创建五角色。若进行中才发现复杂度上升，沿用当前run和有效成果，仅把剩余工作或独立验收交给角色。只有宿主不支持、角色启动失败或用户明确要求直做时才说明限制并按已有授权处理，不能静默改成直做或声称已应用角色模型。

每次真实派发前按models.md解析该角色模型与强度，通过role-dispatch.md在原生工具传入实际参数并记录请求/反馈；Claude与Codex分别使用其支持的参数。简单直做沿用主会话模型。后续批次复用原角色和差量上下文；QA核验已有证据并补测缺口，不重新执行相同输入/环境下有效的全部检查。生成目录或浏览器资源冲突时顺序运行，不为并行相互覆盖。

## 简单任务直接执行

1. 契约和锁确认后尽早初始化本run，再读取paths给出的developer规则，在会话内列简短计划与验收项并直接实现。无需planner/dev/qa子agent、context、plan或retro文件；角色文件的交接路径要求只在委派时适用。使用下方CLI即可，不读run_state/run_checks等内部源码猜参数或角色路径。
2. 无实质歧义且在用户授权内时发简报后继续。契约明确要求每次人工确认时遵守；新增依赖、禁区、公开接口/数据、权限/支付/安全、范围不确定时保留闸口 1。已批准的相同范围不重复确认。
3. 默认一个交付批次，不按文件数、任务数或预计时长机械拆批。共享逻辑变更先按developer规则快检实际调用入口，避免到全量终验才发现上下文/默认值不兼容。UI首次执行目标操作前按[浏览器短探测](browser-handoff.md)短查目标，不要求已有recipe。实际运行核心验收与契约要求的终验命令；UI 行为需运行态验证。相同代码、配置、依赖、环境下的成功检查只跑一次，记录命令、范围、时间、退出码和输出位置。无法确认结果有效性时重跑；失败后只复测失败项与影响面，最多 3 轮。
4. 收口写qa/final（摘要、验收证据、未验证项、人工待办），不把未执行检查标通过。结束回复前核对原待办；仍可执行就继续，必需人工操作则按operations.md的wait登记动作与验证标准，不把未完成验收置done。普通提交由当前调度员复核契约④后执行，不为提交或摘要单独创建角色；需要独立部署判断时才使用/续用releaser。完成后追加metrics，按归属finish。
5. 用户/契约要求独立检查、发现复杂度上升或实际风险需要时，按上表转入必要角色流程，检查差量、复用有效证据并补测缺口；文件数不是独立评审触发器。跨系统/高风险升级 thorough。
6. 每次增加角色、拆批、重跑检查前明确具体收益，无收益就省略。区分实现/验证耗时与交接/报告/等待开销；不承诺固定加速倍数。用户预算到达 80%、CONCERNS 或异常时读取 operations.md，报告完成项、剩余检查和可交付范围。

### 直通调用（由智能体执行）

runner为本skill实际目录的`scripts/pipeline_cli.py`；`paths`返回真实存在的skill、runner和developer路径，不猜agents/developer.md或run_dispatch.py。常用命令如下，私有receipt由start返回并由当前调度上下文保存，用户无需配置：

```sh
python3 <runner> paths
python3 <runner> status --project <root>
python3 <runner> start --project <root> --stage review
python3 <runner> check --project <root> --receipt <receipt> --name feature-r1 --scope src e2e package.json package-lock.json --coverage e2e_feature --impact-covered --timeout 120 -- <实际检查命令及参数>
python3 <runner> report --project <root> --receipt <receipt> --kind qa --name final --json '{"gate":"PASS","required_checks":["e2e_feature"]}'
python3 <runner> finish --project <root> --receipt <receipt> --direct --stopped --stop-evidence '<宿主确认本run执行者均停止的实际证据>'
```

scope包含命令真实依赖的源码、用例、工具/配置与依赖身份；例中路径不是所有项目的固定范围。name区分批次/轮次，coverage使用不随重试变化的语义类别；局部用例不能冒充全量门禁。impact-covered仅在影响面确已覆盖时传入。report的PASS由实际验收决定，CLI不会自动将命令成功转为业务通过。finish不代替终止执行者；实际委派时省略direct并先关闭角色派发。update可调整stage/activity，--help按需查参数；高级恢复、浏览器等继续用既有接口。

多个验收项或开发/QA交接改用[检查计划](check-plan.md)的plan/run/qa，共享稳定类别，不手工抄写两份列表；采用计划后，CLI缺少QA PASS会保留活动run并提示先验收，不能跑完命令就直接finish。子包检查可传`check --cwd <真实子目录>`；目录或运行器不确定时先用只读preflight，scope仍从项目根计算。成功finish自动置done，并返回未封存交付的具体原因；开发、测试和等待的实际切换仍及时update，不在收尾推算旧阶段耗时。

CLI不生成Python字节码缓存，避免自身导入改动被计入源码输入。直接通过Python API操作且脚本目录也在检查scope内时，使用`python3 -B`或`PYTHONDONTWRITEBYTECODE=1`运行，避免无关缓存更新使证据失效；这不允许忽略真实生成产物或源码变化。

## 按需委派：计划与闸口

1. 沿用当前run，不重复初始化。已有明确方案时由主会话维护短计划，直接进入对应developer/tester或审查角色；仅需要方案取舍时创建planner，传最小base/run和当前差量。
2. planner需要时，发现实质歧义只返回带建议答案的问题；无歧义时写 `plan.md`。仅天然分层或有提前QA收益时按依赖/文件归属拆批；没有收益保持单批，不机械按任务数或预计时长增加交接。
3. plan 只记录任务、验收、涉及文件、来源定位、快速/终验测试层和基于 metrics 的 ETA，不复制基线正文。
4. 用户给 `--auto` 或契约允许低风险自动进入时，发送计划简报后继续；新增依赖、禁区、公开接口/数据、权限/支付/安全、范围不确定时必须等待闸口 1。
5. 上述风险信号或用户/契约要求独立评审时创建 guardian 评审 plan，最多 2 轮；保留同一 guardian 供终验复用。仅文件数量增加不触发。

## 开发、QA 与收口

1. 复杂开发创建一个持久developer；首批可测产物冻结后读 `qa-loop.md`并创建持久qa。纯审查沿用已选审查角色，不机械补开发链。仅确有独立、可完成的验收准备工作时提前创建QA；后续批次只用 follow-up 发送 context 差量。并行前核对生成目录及其他共享资源，冲突则顺序执行或显式隔离。
2. qa 完成所有批次后只读 `qa/index.md`，运行终验层一次并写 `qa/final.md`。若已有 guardian，唤醒它只读 plan、qa/index.md、qa/final.md 做最终范围检查；否则调度员据这三份精简 artifact 收口。
3. 人工上线模式由调度员直接输出摘要和待办；普通自动提交也由调度员执行，不重复创建releaser或长篇release-notes。已有releaser可续用，需要隔离部署判断时才委派，遵守契约④与本次用户授权。
4. 把既有数据写入简短 retro，state 置 done，追加 metrics.tsv，展示 qa/final.md 中的人工确认项并清理 `.active`。
5. 终验失败、预算预警、CONCERNS 或异常时读 `operations.md`；正常 PASS 不加载。
