# 同一份检查计划执行与验收

同一需求有多个稳定验收类别、需要开发与QA交接时使用。由智能体根据需求和契约维护，用户无需配置。单个简单检查可用 verify 内联声明，无需手写计划文档；原 `check/report` 仍可用。

计划一次声明命令、真实输入范围和coverage；`run`执行该定义，`qa`从相同定义核验证据并生成required_checks。重试仅换name，不改coverage。示例中路径、命令和超时须按项目实际替换：

```sh
python3 <runner> plan --receipt <receipt> --name acceptance --json '{"checks":{"unit":{"argv":["pnpm","test:unit"],"cwd":"web","scope":["web/src","web/tests","web/package.json","pnpm-lock.yaml"],"coverage":["unit"],"timeout":120,"write_paths":[]}}}'
python3 <runner> run --receipt <receipt> --plan <返回的plan引用> --id unit --name unit-r1 --impact-covered
python3 <runner> qa --receipt <receipt> --plan <同一plan引用> --name final --summary '<业务验收结论与未验证项>'
```

`--plan` 必须使用 plan 返回的 `plan` 或 verify 返回的 `plan_ref` 实际完整引用；不按 name 猜测 artifacts/plan 路径。只为本轮检查声明时，可用 `verify --receipt <receipt> --name verify-r1 --json '<与plan相同的checks/required_checks对象>' --summary '<真实业务结论与未验证项>'`，与 `--plan` 二选一。入口以本轮 name 登记不可变计划，再调用同一 verify/QA 门禁；返回实际 plan_ref，后续入口沿用它。JSON 不接受 gate 或替代既有必验类别；baseline 观察不能列为验收检查。

每个check按该命令真实依赖分别声明scope，不把`git ls-files <整个子包>`的全量结果复用给所有检查。定向逻辑/夹具检查包含其源码、用例及共享配置/运行器/锁文件；独立文档或映射检查只在确实不依赖业务源码时使用独立scope。全量构建仍覆盖真实完整依赖，不能为命中复用自动排除文件。verify按每项身份核验：仅一项输入变化时，另一项仍有效的证据可继续复用，不重跑整个计划。范围完整性由执行者负责，脚本不推断语义依赖。

managed运行不允许用手写QA PASS跳过上述判断。qa/verify产生当前计划的检查/覆盖/缺项结论；普通report即使写PASS也必须接受同一验证，不能认领未执行、失效或其他任务的证据。完成门禁同时适用于共享Python入口；无计划的简单单检查保留standard.md的轻量引用路径。已有legacy记录不因此自动获得已采用证明。

## 原生宿主的多项正式检查

原生浏览器/API工具按[evidence.md](evidence.md)先observe-begin、实际调用一次宿主、返回后保存脱敏日志并observe-finish。多项正式检查仍使用同一计划；每项用`observed_ref`精确引用本run已完成的test/check宿主记录，不填假argv，也不把visual/baseline或普通metadata报告当正式检查。声明scope、coverage、intent须与原记录完全相同，environment为该记录payload.environment中的host_identity_sha256对象；它只保存宿主身份hash，不含原环境值。先确认当前真实宿主/页面/版本身份仍匹配，无法确认或已经变化则实际补验，不能复制旧hash冒充当前身份。源码、夹具、运行配置和依赖仍须完整纳入原观察scope。

以下为纯原生的两项示例；执行者用实际目标替换入口和scope，在每个begin与finish之间真实调用相应宿主。exit-code取实际断言结果，日志须已保存；不照抄0冒充通过：

```sh
python3 <runner> observe-begin --project <root> --receipt <receipt> --id ready-r1 --json '{"command":"browser.ready","scope":["web/src","web/tests","web/package.json","pnpm-lock.yaml"],"environment":{"browser":"<实际宿主与版本身份>"},"coverage":["browser_ready"],"write_paths":[]}'
# created=true时真实检查页面就绪，返回后保存tmp/pipeline/ready-r1.log。
python3 <runner> observe-finish --project <root> --receipt <receipt> --id ready-r1 --exit-code <实际退出码> --log-path tmp/pipeline/ready-r1.log --host-stopped --stop-evidence '<实际宿主返回>'
python3 <runner> observe-begin --project <root> --receipt <receipt> --id confirm-r1 --json '{"command":"browser.confirmation","scope":["web/src","web/tests","web/package.json","pnpm-lock.yaml"],"environment":{"browser":"<实际宿主与版本身份>"},"coverage":["browser_confirmation"],"write_paths":[]}'
# created=true时真实验证确认交互，返回后保存tmp/pipeline/confirm-r1.log。
python3 <runner> observe-finish --project <root> --receipt <receipt> --id confirm-r1 --exit-code <实际退出码> --log-path tmp/pipeline/confirm-r1.log --host-stopped --stop-evidence '<实际宿主返回>'
python3 <runner> plan --project <root> --receipt <receipt> --name native-acceptance --json '{"checks":{"ready":{"observed_ref":"<ready finish返回的report>","scope":["web/src","web/tests","web/package.json","pnpm-lock.yaml"],"coverage":["browser_ready"],"environment":{"host_identity_sha256":"<ready report中的实际64位hash>"},"layer":"quick"},"confirm":{"observed_ref":"<confirm finish返回的report>","scope":["web/src","web/tests","web/package.json","pnpm-lock.yaml"],"coverage":["browser_confirmation"],"environment":{"host_identity_sha256":"<confirm report中的实际64位hash>"},"depends_on":["ready"]}}}'
python3 <runner> plan-preflight --project <root> --receipt <receipt> --plan <plan返回的实际引用> --name native-final
python3 <runner> verify --project <root> --receipt <receipt> --plan <同一plan引用> --name native-final --summary '<实际业务判断与缺口>'
```

已完成的原生项可直接`qa --project <root> --receipt <receipt> --plan <同一plan引用> --name native-qa --summary '<实际业务判断>'`，与verify使用相同_valid判断；不必再调用宿主或先造一条OS检查。verify/run只绑定并复核原report，不生成新check或宿主耗时；preflight无写入且不会调用宿主，无效时保留缺项并阻断后续重检查。失败或身份/输入/日志变化须实际补验，保存新source引用与新版本计划，原失败不删除；稳定逻辑command、cwd（原生默认项目根）、scope与environment身份不能靠换描述绕开后续失败。修正locator后的真实重测使用新observation ID，沿用同一逻辑command。

旧task原生记录默认无效；确已确认相同宿主身份、输入和影响面时，可先用`run --project <root> --receipt <receipt> --plan <实际plan引用> --id <原生检查ID> --name native-reuse-r1 --impact-covered`显式核验并登记复用，再verify。普通planned绑定不能把旧记录变成当前观察。`observed_ref`项仅接受引用、scope、coverage、environment、intent、layer、depends_on，不能混入argv/cwd/timeout/写集；与argv项可混排，原quick/依赖、当前task、全部必要覆盖和其他QA门禁保持不变。接口只核验已登记的宿主证据，不能自动观察浏览器当前状态或把视觉记录提升为业务通过。

发布承接时，旧失败计划不会永久阻断已经真实恢复的同一验收：仅允许同当前task后登记且当前有效PASS的完整等价计划代表原义务，差异只能是更新的observed_ref。全部检查ID、required_checks、依赖/层级、scope、environment、intent、coverage和argv项须原样保留；更新来源须为同逻辑command、晚于旧观察结束的真实宿主记录。旧计划及其失败引用仍保持失败，不删除要求或取任意最后计划。变窄、换环境、漏项、未知来源或后续失败均继续阻断；旧source QA尚有未解决CONCERNS/FAIL时也不自动推断为已解决，尤其不能以新计划机器PASS覆盖业务判断。

所有命令均接受`--project <root>`。cwd相对项目根，scope和write_paths仍相对项目根；省略cwd沿用项目根。不确定子包/运行器时先用`preflight --project <root> --cwd web -- pnpm test:unit`只读核对cwd与可执行文件；它不运行测试，也不证明脚本/依赖/业务通过。

有正在使用构建产物的预览服务时，在首个可能写产物的检查前声明其受保护目录：`preflight --project <root> --cwd web --write-path web/.isolated-output --protected-path web/.nuxt -- pnpm build`。计划对应字段为`write_paths`和`protected_paths`，均相对项目根；真实`check`也接受重复的`--protected-path`。声明相同或父子目录重叠、或有保护目录但写集未知时，预检返回失败，真实检查在启动进程前拒绝。优先使用项目工具已支持的独立输出目录；不能确认隔离时先停止安排冲突检查，不擅自停服务或改端口。

该结果只比较调用方声明的目录；`not_declared`不表示已隔离，`disjoint`不证明预览服务健康、验证副本依赖完整或未声明资源安全。空写集`[]`只用于确实无写操作的检查；不要为通过保护而假报。仍有效的缓存结果无需启动进程，可按原严格规则复用，不因新增保护声明强制重跑。保护声明随检查计划传到真实执行入口，不能只预检一次后丢失。

本次已识别的高影响边界（缺参、旧数据归属、异步/跨时段状态）用已有可独立运行的定向用例声明`layer:"quick"`；重型或最终组合检查为`layer:"final"`（默认可省略）。verify先执行quick再执行其余检查，首个失败就停止后续重检查，QA仍保留所有缺项；不删除终验或把暂停的检查当PASS。直接run最终检查时，也须先有该计划全部quick的当前有效证据，否则启动进程前拒绝并列出有界检查ID；不自动运行前置检查。无quick的计划保持原行为。仅调整layer不会改变命令/输入/环境/coverage身份，不使仍有效的结果重跑；真实变更和最新失败仍失效。计划增项时可先用run quick对应ID加`--impact-covered`承接完全相同且仍有效的原证据，再运行最终检查；不因只有最终检查的独立输入变化而重跑quick。分类由智能体维护，不新增用户配置或通用矩阵文档。

检查可选声明`depends_on:["检查ID"]`，把项目已有的环境、认证或预览就绪短检查排在依赖它的检查之前，包括quick之间。例如项目已有`pnpm test:preview-ready`和`pnpm test:browser-smoke`时，可将两项均标quick，并在`browser-smoke`上声明`depends_on:["preview-ready"]`；各自仍须声明真实argv、cwd、完整scope与coverage。仅在本次检查确有该依赖时使用，不要求所有项目新增探针；脚本不会自动识别任意项目的API或浏览器状态。

保存和读取计划均拒绝缺失引用、自依赖或依赖环；quick依赖final也与原quick前置门禁冲突，不能保存。verify在原quick优先约束内稳定按依赖顺序执行，同样就绪的项沿用计划顺序。直接run须已有该计划所有直接及间接前置的当前有效证据，失效或未执行则在启动前拒绝；仍须满足原全部quick门禁。依赖是调度信息，不改变命令的真实身份，修改依赖后可在前置有效时复用完全相同的结果；输入、实际环境、日志、最新失败及原QA门禁均不放宽。

若本次改动涉及相应路径，先把已有的登录返回、焦点恢复、缓存刷新、超时/取消定向用例放入quick，核对关键状态转换后再跑全量回归；只选择与本次影响面有关的边界，不为每个需求强制新增整套组合。负向复现的非零退出继续按原分类记录，不能当作通过证据放行最终检查。

UI检查按实际改动的行为选用例，不仅按测试类型分层：本次触及确认提交、金额校验或阻止误操作时，先从已有E2E中选出对应的最小完整交互放入quick，验证触发条件、阻止/允许结果及状态变化，再跑重型全量E2E。单元测试或页面可见不能替代这条交互；缺覆盖才补本需求的定向用例。依赖登录、API或预览的用例先用项目已有短检查确认真实就绪，通过depends_on安排；CLI静态preflight或服务HTTP 200不能代替所需的会话/数据/页面就绪。宿主浏览器检查沿用原观察证据入口，不能编成未执行的命令。必要的完整终验仍保留，已有有效定向证据按原身份规则复用。

本次修改共享composable、store、注入项或模块导出时，把直接受影响页面已有的定向测试加入quick，尽早核对mock、setup、全局stub、可选字段和真实模块实例是否匹配新接口，避免首次到完整单测才发现夹具契约漂移。scope包含这些实际依赖及运行器配置，不能只指纹业务文件。优先使用已有用例的子集；缺覆盖时才补本需求的最小行为用例，不另建全站夹具扫描或配置问卷。导入成功不代表页面行为通过，quick不能替代必要终验。后续夹具修正仍按真实影响面决定复测，原失败证据和QA轮次保留。

已登记计划可用 `verify --receipt <receipt> --plan <原plan引用> --name verify-r1 --summary '<真实业务结论与未验证项>'` 在同一进程逐项执行/严格复核并记录QA，避免拆成多个宿主命令导致环境漂移。仍有效的原证据经原 `run_planned` 校验后复用；实际环境（包括SHLVL）、输入、日志和最新失败均保留原校验，不回放旧环境。首个命令非零即停止安排后续检查，返回skipped_checks并照常记录缺项与CONCERNS；不能将跳过视为通过。每次使用新name，检查尝试名自动为 `<name>-c<序号>`，连同后缀须满足原64字符标识限制。默认仅生成证据/QA，不结束run。

预检同时比较当前task已经执行或显式复用的检查身份，遗漏时返回`current_task_checks_not_in_plan`和对应引用，在任何新进程前停止；相同coverage不能替代不同命令的义务。补齐计划后仍须按原规则核验有效性，不删除旧失败或回填历史。旧task和baseline/search等非验收观察不因此新增义务。

verify在启动任何检查前统一预检整份计划：当前任务已登记及继承的required coverage须由计划项或仍有效的显式继承证据覆盖；所有检查的cwd、可执行文件、声明资源与当前执行占用、派生attempt名称及已有报告名称须满足原调用约束。仍有效且可复用的检查无需启动进程，不因资源占用或保护声明冲突被阻止；执行时继续严格复核，失效证据不能走该豁免。后置runner缺失或coverage漏项时，前面的耗时检查也不启动，返回全部skipped_checks、预检原因和原QA的CONCERNS/缺项，安全原因同时保存在QA摘要；原失败记录与轮次保留，不产生PASS。QA名称已占用时返回CONCERNS和`report:null`，不执行、不覆盖原报告或伪造新轮次，应换有效新name重试；格式无效的name仍按原规则拒绝。单项run与保存计划保持原接口。

可先用 `plan-preflight --receipt <receipt> --plan <原plan引用> --name verify-r1` 只读查看同一判断：返回ready、required_checks、missing_checks及安全的检查ID/类别/原因，不写盘、不启动进程、不输出命令或环境值。ready仅表示当前静态调用条件可安排，不是API、认证、预览或业务通过；真实就绪仍用项目已有短检查和depends_on表达。它不推断完整语义依赖，也不保证预检后资源不会变化，真实执行与QA继续核验当前状态。计划读取可保留已消失cwd的原声明，以便预检定位缺失项；保存与实际执行仍要求cwd存在。

只有用户已显式要求收口且宿主确已确认全部执行者停止时，才在同一条verify命令附加 `--finish --stopped --stop-evidence '<真实宿主停止证据>'`；无委派时可附 `--direct`。启动检查前先核验收口前提：停止证据、当前规则确认、已结束task、已闭合检查/宿主观察/角色派发、已解决人工等待、已有有效release收口及direct声明；未就绪先拒绝，不启动检查。PASS后调用原finish路径并在原归属锁内重新核验这些前提和当前QA完整输入/实际环境，再封存；有过角色派发不得用direct重标。预检不预先要求本次verify将生成的QA，不回放旧环境，也不能保证执行期间不会发生变化；CONCERNS保留活动run。统一入口的证据判断不代替独立业务QA或发布授权，原分开的run/qa/finish命令仍可用。

计划是本run登记的不可变报告；实际命令、目录和coverage不能在run时另行覆盖。相同检查ID可复用仍有效的结果；计划增项时，只有规范化定义与coverage完全相同才可承接，并保留原计划摘要和原始结果引用。输入、环境、日志、来源及最新失败仍逐次校验，不重贴旧PASS标签。每个声明检查都须有有效结果，不能用共享coverage让漏跑项通过。读取计划不再反复哈希所有输入；执行与QA仍检查当前输入。QA索引仅限本次操作，证据中途变化会拒绝该快照。

QA自动包含计划的全部coverage和继承的必要类别；当前任务已声明计划的必要类别也不能通过另建窄计划或手写QA删除；legacy仍沿用原run级要求，run级交付收据及显式继承要求不因任务切分被删除。真实范围变化时保存新版本并明确处理原有要求；缺项继续显示CONCERNS/coverage_missing。命令通过不能替代业务验收，独立QA仍核对真实行为和计划是否完整。专用浏览器等保留原证据接口，已继承且有效的记录无需改造成OS命令。

采用计划的CLI任务先生成当前QA判断，再finish。缺少当前计划的QA PASS，或完成前只读复核发现输入、环境、日志及后续观察已使证据失效时，finish保留活动run并给出下一步提示，不生成新的通过结论。需要暂停时仍可用原paused流程。环境不匹配时，QA及检查返回安全的差异诊断：只展示允许的运行环境键名，其他环境归为聚合变化；旧证据无诊断时标unknown。严格环境校验不变，不靠改标签、忽略未知环境或强写PASS消除CONCERNS。

Python对应接口：`check_plan.save_plan`、`run_planned`、`qa_from_plan`、只读`preflight_plan(root, run_id, owner, plan_ref, name)`，以及 `verify_plan(root, run_id, owner, plan_ref, name, summary="")`；verify_plan只生成检查与QA证据，不自动finish。不得将凭据放进argv、计划environment或summary；environment只是身份信息，子进程仍由统一入口管理。

候选产物上的定向快检可标记 `layer: "candidate"`，通过 `depends_on` 指定实际候选构建及其必要依赖。执行顺序为 quick、候选所需前置、候选快检、剩余终验。候选快检失败时停在当前结果，修复后复用仍有效的前置证据。候选依赖里只放真实前置，避免把本可后置的全量检查拖入关键路径。

高风险任务补跑前先看同一份 `plan-preflight` 的 `review`、`unplanned_checks` 和 `reuse`。它们分别显示方案记录是否对应本计划、哪些当前检查未纳入计划，以及当前环境下各检查的复用判定。方案引用变化时返回已有差量。静态 `ready` 只表示执行入口可用，QA 仍独立核验方案和结果。
