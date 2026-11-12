# 开发到发布的一次验收链

同一需求进入验收、提交、发布或续用已验收成果时读取。动作名称变化不意味着重新规划、重建上下文或重跑全量测试；用户仍只用自然语言操作。

## 发布范围先固定

从当前用户指令和已有交付引用确定目标提交/版本/产物及实际发布端，复用已有state.next或交付摘要说明，不新增范围文件或问卷。只交付该成果，不混入无关工作区改动、不修改业务逻辑、不顺手升级依赖、调整配置或修历史缺陷；必要版本/发布元数据按原契约处理。

发布只走已有流程的必要现场预检、有效证据核验、所需构建/激活和本次关键路径冒烟。检查范围来自既定项目契约与本次目标，不能临时增加全站巡检、架构评审、额外功能验收或一套开发角色。已通过且仍有效的检查复用，生产产物身份和必要现场观察保留。

普通提交、推送和已确定成果的发布由现有主调度员推进；已有明确目标与入口就直接执行下一必要动作。阶段变化不新增planner、QA、releaser或手写计划，只有确需独立部署判断/契约要求才使用角色。start/verify/finish可用`--compact`保留必要返回；完整证据仍在原报告，不为组织输出重新执行检查。

门禁失败时，本次发布停在阻塞状态：简报失败项、线上是否变更及可用回退状态，不自动“修复后再发布”，不换名无限重跑，也不改测试放行。需要业务代码或额外范围修复时，取得针对该问题的授权后单独处理；已明确授权的修复不重复确认。断连等同一候选的恢复仍按下方有界规则，不能借恢复扩展功能。超过该环境已知正常时长或约定窗口时，先报告当前阶段与异常，不自行扩大排查、修改或重试。

## 高风险 PR 的合入说明

准备高风险 PR 时，引用已有验收证据，并写明受影响的入口、调用方或数据范围。涉及迁移、权限或跨系统状态时，说明回退方式、回退后仍需处理的数据或外部状态，以及尚待验证的步骤。回退可行性以实际验证或已有运行手册为依据，缺少依据时标为待核实。

## 调度调用点

1. 同一活动run内继续阶段，直接复用已读范围和有效检查；当前执行者仍可用时续用，不为“提交”“发布”再冷启动一套角色。
2. 原run已完成，用户要求提交/发布该成果时，用`run_state.start(root, {"stage": "release", "delivery_from": "latest"})`开启后续执行段。这是对同一交付的承接，不启动一轮新需求规划。其他新需求不传delivery_from。
3. 查看返回state.delivery_status：adopted=true时先用继承检查及已校验的原验收脚本/样本路径。前台验证必须先经run_checks.execute_check；范围明确时传impact_covered=True，它自动查找本run已登记结果（含继承结果），命中则跳过执行，无需手工查日志或再传reuse_from。
4. 实际部署时，在首次发布检查、构建或激活前确定工程验收入口。分阶段入口先调用下文的`release_runner prepare`，用同一计划执行并收口。单体脚本沿用[检查计划](check-plan.md)，先登记可执行的必要检查及带`release_activation`类别的原部署命令，做计划快检。必要的原生线上回读先确定验收条件，实际返回后再将`observed_ref`纳入收口计划，保留原检查定义和有效引用。把目标提交和部署候选绑定到本次检查。若用户同时要求推送，先核对待推送提交范围，分别报告 Git 远端提交和实际线上版本。
5. 按已选入口完成 QA 收口。`run_state.finish(completed)`在原归属锁内自动封存交付。缺 QA 通过、证据失败或变动、旧报告归属不明时不会产生有效交付凭证。发布脚本结果、线上回读和凭证封存分别记录。线上已切换而凭证未封存时，保留现场与现有证据，明确缺项；先核对现状，再补可安全执行的缺项。

首次委派或旧角色续用时，把本次证据入口和增量验收范围直接放入短交接，不转发整段历史，不要求角色重读所有PASS报告。普通提交与摘要由当前调度员完成；实际需要独立部署判断或用户指定角色时，才使用/续用releaser。thorough保留完整角色链与必要闸口，后续阶段沿用已有角色与结论。

## 跨run继承的边界

交付凭证放在源run的artifacts/delivery，项目tmp/pipeline/delivery.json只保存最近凭证引用及hash，不含owner token。start显式承接时校验同项目、源run已完成、QA/可选验收通过、契约指纹及源报告hash；不会认领其他项目、未完成或失败run、无元数据的历史文件。

继承到当前run的检查保留原始来源、报告hash、验收凭证链、原命令/环境/输入和日志指纹，不伪称当前run又执行过。统计分别呈现executed/inherited/reused。后续重用仍核验整个来源链；源代码、环境、日志、验收或来源记录变化会失效，不因跨阶段放宽质量门槛。

凭证还带可继承证据覆盖矩阵：QA required_checks与各检查coverage逐项对应到原始报告。complete=true才表示已声明的类别齐全；缺项为false，旧报告未声明为unknown。它衡量可核验复用证据，不把缺少缓存记录直接判为业务失败。单测、E2E、构建等功能证据必须各有实际引用，提交metadata检查不能冒充完整功能验收。部分证据有效时仍可承接，保留原要求，仅补失效或缺失类别。

finish直接返回coverage_missing及coverage_issues，凭证保留excluded_checks和coverage.unavailable中的失效原因，避免为解释缺项反复全读历史报告。封存时排除的旧检查只作诊断，不在后续阶段因文件恢复而自动重新纳入；有效检查仍逐次校验。QA PASS与可复用矩阵不完整可同时存在，按inputs_changed、log_changed等实际原因处理；源码等价检查不会自动重贴旧unit/lint标签，也不能补足已失效的现场预检。

智能体可在原run更新handoff_files，列出已验证的浏览器脚本、合成样本或候选构建目录；finish封存其路径与指纹。后续state.delivery_status.assets逐项标unchanged，优先复用有效脚本和样本，失效项单独处理，不丢弃其他有效检查，也不重新猜DOM入口。目录存在不代表可上线，构建版本/提交身份仍须符合本次目标。

浏览器验收后按[browser-handoff.md](browser-handoff.md)记录脚本、样本和实际页面前置条件，接口自动将这些文件加入handoff_files。后续使用先做短探测并preflight；页面或样本不匹配时直接定位差异，不在错误入口上反复等控件。recipe可用只表示复用验收方法，不能替代本次必要的线上验收结果。

继承失败会返回delivery_status.adopted=false；先看拒绝原因和影响面，再补缺口，不静默改成PASS，也不不加区分地重跑整套。最新阶段未通过、缺交付证据、暂停或中止时，latest明确阻塞，不默认回退旧PASS。显式选择旧引用仍需完整校验及对应用户意图。旧格式不能只补上当前run_id当作合格历史证据。

PIPELINE.md保存稳定的权限、范围、测试与上线规则；本次版本号、进度、发布说明和验收结果写入run报告或已有发布说明，不为每次收尾改写契约。真实契约变化仍严格失效，不忽略文件片段来强行命中。reuse_decision/承接状态区分delivery_contract_changed、delivery_reference_changed、delivery_acceptance_changed、delivery_lineage_invalid等原因；按实际受影响范围补验，不将它们统一解释为源码测试失败。

## 最终验收前冻结候选

按项目原发布入口执行同一条顺序：完成目标源码 → 准备版本号和发布元数据 → 固定这些真实输入 → 构建候选 → 验收该候选 → 激活同一候选 → 核对线上身份与关键路径。版本文件、锁文件、运行时依赖、嵌入产物的提交/构建标识都在冻结前确定；不能终验通过后才补版本，再把旧构建或旧PASS当作新版本验收。单纯整理不参与构建/运行的发布说明按其真实影响处理，不制造全量重测。

项目已有分阶段入口时，复用下方release_checks适配方式：

1. 在版本准备完成后的输入上运行本轮preflight/compatibility；build的scope覆盖实际源码、版本/依赖文件、配置与原构建入口，artifact_paths指向本次候选完整产物或已有可靠完整性清单。
2. build成功后，沿用当前run的`release_plan.candidate`：`id`绑定命令、源输入指纹、实际环境和产物指纹，`check`保存真实构建证据引用。把该候选引用交给最终验收，使用原项目支持的预览/验收入口指向这些产物；不另构建一份作为受验对象，不用手写版本字符串替代候选身份。必要的源码检查继续按自己的完整输入身份核验和复用。
3. 验收通过后调用activate，原项目入口只激活这份已验候选，不能在激活时隐式改版本或重新构建；随后smoke验证同一候选的线上身份。release_checks自动把候选ID加入激活/冒烟证据上下文，closeout核验当前候选及其激活后的观察，不手填或复制该上下文冒充绑定。

冻结后源码、版本/锁文件、依赖、构建环境或产物发生真实变化时，原指纹判断决定哪些证据失效；恢复必要检查并重新形成候选，保留旧失败和来源。候选身份及构建验收始终使用真实输入，不采用下节版本无关源码检查的身份归一化，不排除package/lock或回放旧环境来维持候选。当前适配器只能核验调用方声明的输入与本地产物，不能自动证明远端激活的是该目录；远端候选选择与身份观察仍由项目已有入口完成。只有单体脚本时遵守下方单体入口规则，不虚构可拆分的候选验收能力。

## 只改版本号时

源码测试、依赖变化和发布元数据检查分别声明范围。明确不依赖版本字段的源码检查，可在environment中记录`input_fingerprint.dependency_identity(root, ["package.json", "package-lock.json"])`，scope仍覆盖源码、测试与配置。该身份只忽略根版本号三处，依赖、scripts、engines、锁文件和间接依赖变化仍会失效。

这不是全局忽略package/lock文件的许可。版本敏感测试、构建和产物身份检查仍把版本文件纳入scope。无法确认测试是否版本无关时保留原范围，补受影响检查；不能为了命中缓存删掉依赖输入。

## 发布只补必要差量

在设计和发布预检时尽早识别本次必需的人工登录、权限、平台控制台及服务端接收/去重验收；能自主核验的先完成，用户专属步骤给出具体动作和成功标准。客户端请求发出、HTTP 200或网关可达不证明平台实际收到事件。必需验收受阻时登记human_wait并保留原待办；可选外部结果可以单列pending，不能把必需项改称可选以提前完成。

- 发布前先在实际目标环境检查当前健康入口、必要配置/凭据是否可用、共享资源及部署适配器；只保留通过/拒绝与证据，不记录凭据值。若本次已授权的开发包含测试修改，用真实运行器执行受影响用例；纯发布不因此新增测试开发或扩大验收。
  首次调用本次必需的发布/刷新接口前，优先用既有调用方或本地dry-run核对实际入参契约，特别是可选字段省略、null与空集合是否等价；不通过真实发布写操作试探参数。已有可靠调用继续沿用，不额外发明通用校验器或必填问卷。发现参数缺口按本次授权纠正调用方式或报告阻塞，不扩展服务端/业务修改范围。
- 版本和提交元数据确定后构建一次；发布脚本支持复用该产物时，用其已有选项跳过再次构建。先核验产物版本、提交和完整性，不猜跳过参数或擅改部署脚本。
- 相同脚本/依赖/测试输入已通过的发布逻辑单测继续复用；本次候选版本和构建身份校验仍执行。只有依赖或实现变化影响到检查时才复测对应范围。
- 上线后只做版本/资源及本次改动的关键路径冒烟。需要验证升级兼容时复用既有合成样本与浏览器入口，先确认当前页面前置条件；不要重新编写整套脚本来重复开发期覆盖。
- 必须保留备份、实际产物完整性、权限边界和必要线上验收。提交/继承通过不等于发布获授权；同一需求的用户逐项提交要求不改变。

### 源码与目标平台产物清单

优先复用项目已有的打包、上传和完整性校验命令；已有可靠校验覆盖本次目标时，不临时再造清单或增加第二层发布协议。只有确有文件级校验缺口时，才使用`scripts/release_artifacts.py`的纯本地POSIX工具。`capture(root, relative_directory, layer="source"|"artifact", identity={"build_id": ..., "platform": ...})`完整扫描传入目录，不解析项目的上传排除规则；`compare(expected, observed)`返回added/missing/changed/complete，`loads_manifest(text)`严格读取JSON。拒绝越界、软链接、扫描期间输入变化及格式歧义，不调用网络或发布命令。

源码清单必须从**本次实际打包、上传的同一暂存目录**采集，文件集与排除规则由原打包步骤确定。不要对项目根或另一份Git文件列表生成清单，再与过滤后的上传包比较；不手抄一套排除规则、事后忽略差异或改变原上传策略来凑齐清单。只有归档包且已有可靠包校验时，沿用原校验，不为调用目录扫描工具重新打包。配置文件是否纳入取决于实际上传包：原流程不上传的本地配置不会进入暂存目录，实际需要上传的配置仍须完整核验，禁止为此全局忽略`.npmrc`等文件。

- source对：原打包步骤形成的实际上传暂存目录与目标机实际收到的同一源码包比较，保留来源build_id/platform身份；复用这次形成的文件集，不再次扫描项目目录另造范围。
- artifact对：在真正构建目标（如Linux）生成最终产物清单，与激活前/后实际候选产物比较，身份为实际构建ID和目标平台。
- 两层分开保存，不把macOS本地生成的文件列表直接当Linux最终产物清单；源码一致不代表目标产物完整。layer或identity不同会拒绝比较，不能靠忽略差异放行。

每份observed须在实际被核验目录采集。它校验本地传入的两份清单，不证明远端曾执行、清单来源可信或线上文件已核验；远端采集与传回使用项目已有授权入口，保留实际命令/日志。build_id/platform由调用者核对，不能仅复制字段冒充真实身份。平台缺安全目录描述符时明确拒绝，保留已有部署校验。

### 发布控制入口（已有候选/激活命令时）

由智能体从现有项目契约与已验证部署命令维护，不让用户填写新配置。先确认项目已有候选构建、完整产物验收及只激活既有产物的可靠入口；再用`scripts/release_checks.py`对接。所有命令仍须已有授权，不会因套用接口自动获得线上权限。

可直接调用同一模块的薄CLI，无需每轮编写Python协调脚本。`--receipt`沿用pipeline_cli start返回的私有引用；以下JSON中的命令、范围、产物路径均由智能体替换为项目已有且已确认的实际值，不照抄占位命令：

```sh
python3 <release_runner> prepare --project <root> --receipt <receipt> --json '{}'
python3 <release_runner> execute --project <root> --receipt <receipt> --json '{"name":"preflight-r1","phase":"preflight","argv":["<真实现有快检命令>"],"scope":["<真实依赖路径>"],"timeout":120}'
python3 <release_runner> status --project <root> --receipt <receipt>
python3 <release_runner> closeout --project <root> --receipt <receipt> --json '{}'
```

`release_runner`为本skill实际`scripts/release_checks.py`。execute每次只安排声明的一个phase，依次沿用原preflight/compatibility/build/activate/smoke门禁；build另传`artifact_paths`，smoke声明实际验证的release_identity。允许的执行字段为name、phase、argv、scope、artifact_paths、timeout、cwd、environment、coverage、write_paths、protected_paths；prepare仅接受required_before/required_after，closeout仅接受external_pending。未知字段/旗标拒绝；不自动激活、重试或轮询。退出码0表示本次调用成功，1表示实际命令未被接受，2表示请求或门禁拒绝；命令真实结果与失败详情保留在原检查证据中。status只读返回当前有效候选ID、激活状态、前置缺项、恢复阻塞及尝试状态，不输出receipt内容、命令或环境。它不是线上观察或最终验收结论。

- `execute(root, run_id, owner_token, name, phase=..., argv=[...], scope=[...], timeout=..., **check_options)`沿用run_checks，phase为preflight/compatibility/build/activate/smoke。scope声明真实输入与工具/依赖配置，输出目录放write_paths；不能为了缓存删依赖。额外coverage只写命令实际覆盖的类别。
- preflight执行本轮目标现场快检；compatibility用真实运行器执行受影响用例。两者必须本轮实际执行，不能以继承的旧现场观察替代。相关开发单测、E2E等已验证证据仍按原指纹复用。项目有额外必要门禁时，`prepare(..., required_before=[...], required_after=[...])`只能追加要求，不能删除默认门禁或原验收要求。
- build传`artifact_paths=[...]`绑定已有产物或项目适配器生成的真实完整性清单。清单必须来自实际产物核验，远端清单不能冒充本机已校验远端文件。build命令仍执行项目必需的候选门禁；本地入口核验输入、命令、当前工具环境、日志及实际产物后才可跳过重复构建。缺失/损坏产物、新规格失败均阻止静默沿用旧候选。
- activate使用项目已有的完整性、当前版本/提交、回退及共享服务隔离校验。构建期间环境变化导致回退时，先核对当前目标和环境稳定性，再复用同一合格产物恢复一次；不能继续使用过时PID基线。每个候选最多两次自动激活尝试（首次及一次恢复），连续失败就报告具体阻塞和已保留的产物，不改名字或重建相同产物来重置计数。不要临时编写并反复调试一套新的部署协议。
- smoke合并当前候选上线后的版本/身份与本次变更关键路径；实际核验身份的命令加`coverage=["release_identity", ...]`。它和preflight/activate每次真实执行，不缓存现场；必须来自本候选成功激活后的新观察。正常已通过时不重做全套移动端、浏览器、全站巡检，除非影响面或项目契约要求。
- `closeout(..., external_pending=["search_indexing", ...])`生成一份简短工程QA；相同输入重复收口只返回原报告。Google收录、canonical重新抓取、AI自然推荐等外部结果单列待确认，不循环刷新等待外部平台。需要人工验证码/登录时报告人工动作；后续观察仅在用户已授权时安排。明确属于本次必需验收的外部结果不能靠pending绕过，追加的required_after及原需求约束仍生效。

确认执行者停止后按原规则finish；采用本入口的run必须有仍有效的closeout，不能漏掉线上冒烟就终结。未采用本入口的旧run保留原流程。模块提供已接入调用的校验，不拦截未接入的宿主工具，也不自动观察远端进程；外部时间/token缺数据不估算。

managed run的closeout使用同一release验收重算生成machine_validation.mode=release，继续核对当前task、原源码检查计划及必要类别、源QA未解决结论和当前环境/日志；不能用release标记、候选ID字符串或旧任务PASS替代这些证据。收口与finish都会复核，CLI不另造一条QA捷径。

已完成的激活需要持久保留操作身份时，可显式调用`release_checks.capture_operation_proof(root, run_id, owner_token, name, target_environment={...})`。目标字段必须已作为environment上下文真实用于同一候选的activate和smoke，smoke须实际覆盖release_identity。接口只保存当前run/task最新成功激活、当前环境下有效的线上回读、精确候选与产物指纹及报告hash；目标值只保留hash，不执行发布、刷新缓存或其他写操作。已有激活报告是历史操作事实，不能把其环境拷贝成当前环境来补QA。

`inspect_operation_proof(root, run_id, proof={run_id,path,sha256}, target_environment={...}, readback=可选的新smoke报告引用)`只读诊断，不输出命令、环境值或私有receipt。`historical_mutation_valid`表示历史操作绑定仍可核验，`current_readback_valid`另核对真实当前进程的完整环境；`valid_operation`要求两者同时有效。进程环境变化时只补必要的只读线上身份/关键路径smoke并显式传新引用，保留原激活身份；候选、输入、日志、报告、目标、任务或后续失败变化仍拒绝旧证明。没有项目可靠只读回读命令时报告适配缺口，不重放有副作用的发布/缓存操作。

操作证明始终返回`acceptance_status=CONCERNS`，不作为通用check复用、QA PASS或closeout的替代。即使`valid_operation=true`，仍须通过原检查计划、完整环境及最新失败约束；`next_action=validate_original_strict_qa_contract`明确标出未被本接口解决的验收契约。若现有QA仍要求历史写操作在新本地环境中重新运行，保留CONCERNS并报告该契约缺口，不能自动豁免或反复执行写操作来凑PASS。

薄CLI沿用同一接口；target_environment替换为原activate/smoke已真实使用的上下文，proof替换为capture返回的完整引用：

```sh
python3 <release_runner> operation-capture --project <root> --receipt <receipt> --json '{"name":"activation-proof","target_environment":{"release_target":"<实际目标>"}}'
python3 <release_runner> operation-inspect --project <root> --run-id <run> --json '{"proof":{"run_id":"<run>","path":"<capture返回路径>","sha256":"<capture返回摘要>"},"target_environment":{"release_target":"<实际目标>"}}'
```

capture要求当前owner私有receipt；inspect只读，不要求或输出receipt，可选`readback`为当前run新smoke的`{run_id,path,sha256}`引用。inspect退出码0仅表示`valid_operation=true`，仍为CONCERNS，不是QA PASS；1为绑定/当前回读无效，2为语法、声明或请求错误。它不自动执行线上回读或任何写操作。

项目只有单体部署脚本时，先完成快预检，再经execute_check执行现有脚本一次并保留原门禁；不为套用分阶段接口额外部署一次，不虚构skip-build参数。无法安全复用时说明项目适配器缺口，按当前授权处理；不能自动改项目部署脚本或绕过保护。没有明确缺口时，不为了收尾另起规划/角色、重复整理多份报告或反复全量核对。

单体脚本已执行后，使用计划的`qa`核验现有证据。含部署命令的计划不交给会自动执行缺项的`verify`收口。证据缺失或过期时先核对原发布结果及线上版本，再决定可补的只读观察。

### 远端断连与取消后的恢复

长任务优先使用项目已有的持久执行入口：在启动前确定执行身份、目标、候选版本，以及远端日志与最终结果位置；日志与退出结果保存在执行端，连接仅负责提交和观察。没有这样的入口时保留原脚本和门禁，不能临时拼接一套后台部署协议、擅改脚本或把启动命令的exit 0当完整发布成功。日志只提取进度/结果，不传凭据；不要持续输出整份构建日志。

SSH断连、超时、取消只说明当前观察或调用结束，远端进程可能仍在执行。恢复时先通过原适配器核对**同一执行**的进程、结果、候选与线上版本；身份或结果未知时禁止重试、激活和验收通过。仍在执行且有进展则续看原日志，不再启动一套；确实停止且未激活/已安全回退，才按证据补失败步骤。合格产物按原指纹复用，未完成/不合格产物不能复用；现有每候选两次激活上限不重置。

取消必须阻断整条发布链的后续激活，不能仅终止某个构建子进程后让父脚本继续。优先项目已有的完整停止/回退入口；无法确认安全取消时保留现场并报告缺口，不靠杀进程名或反复重跑恢复。激活仍必须绑定本候选完整门禁、产物身份和实际成功结果。发现未验收的切换按项目原回退规则处理，不把停止后的晚到exit 0或日志中的成功字样自动提升为PASS。pipeline本地保护只约束接入的调用，不能阻止未接入的远端脚本自行激活。

已采用`release_checks.execute`时，build/activate的断连类退出、超时或执行中断会登记recovery_hold，阻止换名重跑、激活和closeout/finish。`cancel_attempt(root, run_id, owner_token, name, reason=...)`仅登记取消与阻塞，实际安全停止仍由项目入口执行；已取消尝试即使晚到exit 0也不能成为合格候选。原退出码和失败记录保留。

核验后由当前owner保存本run已注册的`kind=release_recovery`报告，payload为`{attempt: 原name, executors_stopped: true, stop_evidence: 具体停止证据, remote_state: "not_activated"或"safely_rolled_back"}`，再把`{run_id, path, sha256}`引用交给`reconcile(..., name, evidence=引用)`。记录时间须晚于hold，仍有原执行者、远端状态未知/仍激活、引用跨run/过时/被改均拒绝解除；已解除的证据仍复核。它只允许后续按原规则恢复，不改旧失败为PASS、不重置激活次数、不替代现场观察或自动执行回退。用户无需填写该报告。

### 发布耗时归因

已有适配器需要定位慢因时，可在**原有操作**外包`scripts/release_timing.py`的`with step(root, run_id, owner, "build"):`；支持dependencies/gates/build/transfer/readiness/activate/smoke，沿用operation_metrics，不新增部署、门禁或角色。只记录短标识和起止，不存命令凭据。失败保留failed，开放/中断跨度不猜时长。单体或远端脚本无法安全内部分段时，保留原脚本一次执行及其当前run的check引用。

`pipeline_cli.py release-timing --project <root> --run-id <run> [--check <本run部署检查引用>]`只读汇总已登记阶段和显式部署检查；重复引用去重、并行/嵌套取区间并集、继承检查不计为本轮执行，human_wait区间不计入步骤。无内部观察的安装、传输等为unknown，命令内未归因时间单列，不能当作推理耗时、队列耗时或浪费。计时不决定QA、上线成功或候选有效性；原质量门禁与复用校验继续生效。

该链条证明证据可复用，不证明所有需求已经提速或节省固定比例的token。没有宿主实际数据时保持未知。

复核发现新上线阻断时，用 `report --kind release_blocker --name <记录名>` 登记 `{"action":"open","coverage":"<专项验收类别>","reason":"<具体问题>"}`。代码 QA 保留原结论，发布激活和 run 完成会检查未解除的阻断，切换 task 不丢失同一 run 的阻断。完成专项复验后，同类 report 写 `{"action":"resolve","blocker_ref":"<阻断报告引用>","check_ref":"<新检查引用>"}`。检查必须在发现之后实际执行、覆盖指定类别，且仍是对应检查的最新有效结果。阻断保持在本地运行记录中，实际激活须走受管理的发布或检查入口。

有发布检查计划时，在实际发布执行器里先跑 `plan-preflight`，查看逐项 `reuse` 判定及需要补跑的原因。单项仍可用 `reuse-preflight`。预测可复用的结果在执行时重新核验，环境不匹配时只补受影响的检查。正常 `verify` 不重复做一轮全计划复用预测。

单体发布脚本已经执行，后续计划仅缺原检查绑定时，用 `plan-bind --receipt <receipt> --plan <计划引用> --id <检查ID> --name <新记录名> --check <已执行检查引用>`。为激活和线上回读分别绑定当前任务的实际成功报告，保留原命令、目录、范围、覆盖和执行环境，按项目既有证据核对候选与目标。此命令核验证据并写入绑定，不执行发布脚本。之后用 `qa` 收口。若绑定失效，`run` 会报出原因，不会重放该绑定的命令。
