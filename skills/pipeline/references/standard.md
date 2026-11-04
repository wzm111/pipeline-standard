# standard 流程

下文 state.md 是旧格式名称；使用 run_state.py 时统一写对应 runs/<run_id>.json，不维护双份状态。结束步骤必须先确认执行者停止和本run归属，再调用finish；不能为满足“清理”而删除未知活动锁。

同一需求的提交/发布不重启本流程：按delivery.md承接已通过的验收、检查和浏览器脚本，仅验证本次变化与上线产物。纯发布门禁失败不进入下方开发/QA修复循环，按发布范围报告阻塞。开发过程只修本需求和必要配套；无关问题仅记录，新增目标另获授权。检查调用execute_check时，确认影响范围后启用自动复用；QA结果按下方受控验收入口落盘，收口核对当前证据后再封存交付凭证。

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

真实开始实现前更新当前run为implementation/execution；进入独立验收时更新为verification/testing。主流程实际派发开发/独立QA时，优先按[角色边界](role-dispatch.md)在begin显式绑定workflow_phase，由宿主accepted同步阶段；未接受的尝试不记实际QA开始。主会话确实等待已接受角色时用wait_for_dispatches绑定所选集合，全部返回后进入handoff，再处理验收结论。直做在首次写入前用现有`update --receipt <receipt> --stage <当前阶段> --activity <实际活动>`切换，不把已发生的实现时间留在planning。不为每条命令或报告新增事件；后台角色不绑定主阶段，角色并行只更新主流程真实状态，不能覆盖人工等待或前台检查。

## 简单任务直接执行

1. 契约和锁确认后尽早初始化本run，再读取paths给出的developer规则，在会话内列简短计划与验收项并直接实现。无需planner/dev/qa子agent、context、plan或retro文件；角色文件的交接路径要求只在委派时适用。使用下方CLI即可，不读run_state/run_checks等内部源码猜参数或角色路径。
2. 无实质歧义且在用户授权内时发简报后继续。契约明确要求每次人工确认时遵守；新增依赖、禁区、公开接口/数据、权限/支付/安全、范围不确定时保留闸口 1。已批准的相同范围不重复确认。
3. 默认一个交付批次，不按文件数、任务数或预计时长机械拆批。共享逻辑变更先按developer规则快检实际调用入口，避免到全量终验才发现上下文/默认值不兼容。UI首次执行目标操作前按[浏览器短探测](browser-handoff.md)短查目标，不要求已有recipe。涉及关键交互时，按[检查计划](check-plan.md)将本次改变的完整交互及真实就绪前置排入quick，再安排重型E2E；不把所有E2E一律留到final。实际运行核心验收与契约要求的终验命令；UI 行为需运行态验证。相同代码、配置、依赖、环境下的成功检查只跑一次，记录命令、范围、时间、退出码和输出位置。无法确认结果有效性时重跑；失败后只复测失败项与影响面，最多 3 轮。
4. 收口写qa/final（摘要、验收证据、未验证项、人工待办），不把未执行检查标通过。输入冻结、执行者实际停止且已有收口授权时，优先用[同进程verify收口](check-plan.md)，将检查、QA与finish放在同一次调用；单项或本轮声明可用 `--json`，已有计划则传返回的实际 `--plan` 引用，不猜报告路径。不在PASS后仅为换收口名称另起全套检查。已有分离证据先核验有效性，确有输入/环境/日志变化才补必要复测；未知环境差异仍阻止复用，不回放旧环境或删环境键来制造命中。结束回复前核对原待办；仍可执行就继续，必需人工操作则按operations.md的wait登记动作与验证标准，不把未完成验收置done。普通提交由当前调度员复核契约④后执行，不为提交或摘要单独创建角色；需要独立部署判断时才使用/续用releaser。完成后追加metrics，按归属finish。
5. 用户/契约要求独立检查、发现复杂度上升或实际风险需要时，按上表转入必要角色流程，检查差量、复用有效证据并补测缺口；文件数不是独立评审触发器。跨系统/高风险升级 thorough。
6. 每次增加角色、拆批、重跑检查前明确具体收益，无收益就省略。区分实现/验证耗时与交接/报告/等待开销；不承诺固定加速倍数。用户预算到达 80%、CONCERNS 或异常时读取 operations.md，报告完成项、剩余检查和可交付范围。

### 直通调用（由智能体执行）

runner为本skill实际目录的`scripts/pipeline_cli.py`；`paths --project <root>`返回真实存在的skill、runner、各角色、release_runner、模板、init_project及hook路径，保留原skill/runner/developer字段。资源从实际runner位置跟随安装软链接解析，与业务项目目录分开；unavailable列出缺失资源，不猜templates位于skill内或使用项目同名文件补齐。缺少必要资源时报告安装缺口，不凭空拼路径。调用参数不确定先读该入口`--help`，再用已有preflight核对真实cwd/运行器；未知参数应在操作前纠正，静态快检不代替业务或上线验收。常用命令如下，私有receipt由start返回并由当前调度上下文保存，用户无需配置：

```sh
python3 <runner> paths --project <root>
python3 <runner> status --project <root>
python3 <runner> start --project <root> --stage review
# 读取本轮适用规则后，使用start返回的policy.revision确认；不是让用户填写。
python3 <runner> policy-ack --project <root> --receipt <receipt> --revision <policy.revision>
python3 <runner> check --project <root> --receipt <receipt> --name feature-r1 --scope src e2e package.json package-lock.json --coverage e2e_feature --impact-covered --timeout 120 -- <实际检查命令及参数>
python3 <runner> report --project <root> --receipt <receipt> --kind qa --name final --json '{"gate":"PASS","required_checks":["e2e_feature"],"checks":["<check返回的完整report引用>"]}'
python3 <runner> finish --project <root> --receipt <receipt> --direct --stopped --stop-evidence '<宿主确认本run执行者均停止的实际证据>'
```

scope包含命令真实依赖的源码、用例、工具/配置与依赖身份；例中路径不是所有项目的固定范围。name区分批次/轮次，coverage使用不随重试变化的语义类别；局部用例不能冒充全量门禁。impact-covered仅在影响面确已覆盖时传入。report的PASS由实际验收决定，CLI不会自动将命令成功转为业务通过。finish不代替终止执行者；实际委派时省略direct并先关闭角色派发。update可调整stage/activity，--help按需查参数；高级恢复、浏览器等继续用既有接口。

managed运行的QA PASS必须由入口核验，不接受只有gate、执行名列表或无required_checks的手写PASS。简单单项使用上例：checks是同run已登记的完整check.report引用，required_checks是稳定覆盖类别；入口核对当前输入、实际环境、日志及最新失败。多项检查或覆盖类别走plan/qa/verify；不能少列某个检查或换task名称来绕过。task-end仍只表示执行结束，不代替验收。

多个验收项或开发/QA交接改用[检查计划](check-plan.md)的plan/run/qa，共享稳定类别，不手工抄写两份列表；采用计划后，CLI缺少QA PASS会保留活动run并提示先验收，不能跑完命令就直接finish。子包检查可传`check --cwd <真实子目录>`；目录或运行器不确定时先用只读preflight，scope仍从项目根计算。成功finish自动置done，并返回未封存交付的具体原因；开发、测试和等待的实际切换仍及时update，不在收尾推算旧阶段耗时。

新CLI start默认管理规则版本，内容摘要只覆盖固定关键规则和项目PIPELINE.md、AGENTS.md、CLAUDE.md的存在/缺失，不扫描业务文件。首次确认后可继续使用；已确认的当前任务沿用记录的共享skill规则版本；共享指导更新只标记`update_available`，不打断检查、构建或完成。`effective_revision`是当前采用版，`revision`是可更新版；`current`表示当前任务采用仍有效，不代表已读取最新版。新task开始前须刷新确认；项目PIPELINE.md/AGENTS.md/CLAUDE.md变化、规则缺失/不安全或快照损坏仍立即阻断。补读变化规则后`policy-ack`沿用原run，仍有效检查不重跑。查询、记录已开始宿主的真实返回、人工等待和暂停/中止不因规则过期被封死。

`caller_acknowledged`仅记录调用方声明已采用该版本，不能证明模型阅读理解或每次进度格式合规。旧run及旧Python默认start保持`legacy_untracked`，不得称已采用；旧run经CLI开启此前不存在的新task时必须先policy-ack，同值旧task重试和既有工作收尾不追溯改写。己方活动run可显式确认升级并沿用同run，不回填历史。新Python调用必须使用`run_state.start(..., managed=True)`接同一门禁，不得为绕过规则选择legacy默认路径；legacy仅保留旧调用兼容，不能作为本轮采用合规证明。只约束这些已接入入口，不能拦截任意shell或替代原生聊天输出。

CLI不生成Python字节码缓存，避免自身导入改动被计入源码输入。直接通过Python API操作且脚本目录也在检查scope内时，使用`python3 -B`或`PYTHONDONTWRITEBYTECODE=1`运行，避免无关缓存更新使证据失效；这不允许忽略真实生成产物或源码变化。

## 按需委派：计划与闸口

1. 沿用当前run，不重复初始化。已有明确方案时由主会话维护短计划，直接进入对应developer/tester或审查角色；仅需要方案取舍时创建planner，传最小base/run和当前差量。
2. planner需要时，发现实质歧义只返回带建议答案的问题；无歧义时写 `plan.md`。仅天然分层或有提前QA收益时按依赖/文件归属拆批；没有收益保持单批，不机械按任务数或预计时长增加交接。
3. plan 只记录任务、验收、涉及文件、来源定位、快速/终验测试层和基于 metrics 的 ETA，不复制基线正文。
4. 用户给 `--auto` 或契约允许低风险自动进入时，发送计划简报后继续；新增依赖、禁区、公开接口/数据、权限/支付/安全、范围不确定时必须等待闸口 1。
5. 上述风险信号或用户/契约要求独立评审时创建 guardian 评审 plan，最多 2 轮；保留同一 guardian 供终验复用。仅文件数量增加不触发。

## 开发、QA 与收口

1. 复杂开发由现有主调度员按独立模块安排1至3个持久developer，最多3个并行，不默认开满，也不新增专门拍板的角色。调度员先划定写入归属与接口依赖：共享文件指定一人负责，有依赖或输出/浏览器冲突的部分排队；各开发只做本批定向验证，合并冻结后统一完成必要终验并复用有效证据。首批可测产物冻结后读 `qa-loop.md`，按宿主剩余名额安排独立qa；宿主总容量包括主会话，容量不足先等开发实际返回，不取消仍工作的角色来腾位。纯审查沿用已选审查角色，不机械补开发链。仅确有独立、可完成的验收准备工作时提前创建QA；后续批次只用 follow-up 发送 context 差量。并行前核对生成目录及其他共享资源，冲突则顺序执行或显式隔离。
2. qa 完成所有批次后只读 `qa/index.md`，运行终验层一次并写 `qa/final.md`。若已有 guardian，唤醒它只读 plan、qa/index.md、qa/final.md 做最终范围检查；否则调度员据这三份精简 artifact 收口。
3. 人工上线模式由调度员直接输出摘要和待办；普通自动提交也由调度员执行，不重复创建releaser或长篇release-notes。已有releaser可续用，需要隔离部署判断时才委派，遵守契约④与本次用户授权。
4. 把既有数据写入简短 retro，state 置 done，追加 metrics.tsv，展示 qa/final.md 中的人工确认项并清理 `.active`。
5. 终验失败、预算预警、CONCERNS 或异常时读 `operations.md`；正常 PASS 不加载。
