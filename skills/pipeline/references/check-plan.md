# 同一份检查计划执行与验收

同一需求有多个稳定验收类别、需要开发与QA交接时使用。由智能体根据需求和契约维护，用户无需配置。单个简单检查继续用`check/report`，不强制另造计划。

计划一次声明命令、真实输入范围和coverage；`run`执行该定义，`qa`从相同定义核验证据并生成required_checks。重试仅换name，不改coverage。示例中路径、命令和超时须按项目实际替换：

```sh
python3 <runner> plan --receipt <receipt> --name acceptance --json '{"checks":{"unit":{"argv":["pnpm","test:unit"],"cwd":"web","scope":["web/src","web/tests","web/package.json","pnpm-lock.yaml"],"coverage":["unit"],"timeout":120,"write_paths":[]}}}'
python3 <runner> run --receipt <receipt> --plan <返回的plan引用> --id unit --name unit-r1 --impact-covered
python3 <runner> qa --receipt <receipt> --plan <同一plan引用> --name final --summary '<业务验收结论与未验证项>'
```

每个check按该命令真实依赖分别声明scope，不把`git ls-files <整个子包>`的全量结果复用给所有检查。定向逻辑/夹具检查包含其源码、用例及共享配置/运行器/锁文件；独立文档或映射检查只在确实不依赖业务源码时使用独立scope。全量构建仍覆盖真实完整依赖，不能为命中复用自动排除文件。verify按每项身份核验：仅一项输入变化时，另一项仍有效的证据可继续复用，不重跑整个计划。范围完整性由执行者负责，脚本不推断语义依赖。

managed运行不允许用手写QA PASS跳过上述判断。qa/verify产生当前计划的检查/覆盖/缺项结论；普通report即使写PASS也必须接受同一验证，不能认领未执行、失效或其他任务的证据。完成门禁同时适用于共享Python入口；无计划的简单单检查保留standard.md的轻量引用路径。已有legacy记录不因此自动获得已采用证明。

所有命令均接受`--project <root>`。cwd相对项目根，scope和write_paths仍相对项目根；省略cwd沿用项目根。不确定子包/运行器时先用`preflight --project <root> --cwd web -- pnpm test:unit`只读核对cwd与可执行文件；它不运行测试，也不证明脚本/依赖/业务通过。

有正在使用构建产物的预览服务时，在首个可能写产物的检查前声明其受保护目录：`preflight --project <root> --cwd web --write-path web/.isolated-output --protected-path web/.nuxt -- pnpm build`。计划对应字段为`write_paths`和`protected_paths`，均相对项目根；真实`check`也接受重复的`--protected-path`。声明相同或父子目录重叠、或有保护目录但写集未知时，预检返回失败，真实检查在启动进程前拒绝。优先使用项目工具已支持的独立输出目录；不能确认隔离时先停止安排冲突检查，不擅自停服务或改端口。

该结果只比较调用方声明的目录；`not_declared`不表示已隔离，`disjoint`不证明预览服务健康、验证副本依赖完整或未声明资源安全。空写集`[]`只用于确实无写操作的检查；不要为通过保护而假报。仍有效的缓存结果无需启动进程，可按原严格规则复用，不因新增保护声明强制重跑。保护声明随检查计划传到真实执行入口，不能只预检一次后丢失。

本次已识别的高影响边界（缺参、旧数据归属、异步/跨时段状态）用已有可独立运行的定向用例声明`layer:"quick"`；重型或最终组合检查为`layer:"final"`（默认可省略）。verify先执行quick再执行其余检查，首个失败就停止后续重检查，QA仍保留所有缺项；不删除终验或把暂停的检查当PASS。直接run最终检查时，也须先有该计划全部quick的当前有效证据，否则启动进程前拒绝并列出有界检查ID；不自动运行前置检查。无quick的计划保持原行为。仅调整layer不会改变命令/输入/环境/coverage身份，不使仍有效的结果重跑；真实变更和最新失败仍失效。计划增项时可先用run quick对应ID加`--impact-covered`承接完全相同且仍有效的原证据，再运行最终检查；不因只有最终检查的独立输入变化而重跑quick。分类由智能体维护，不新增用户配置或通用矩阵文档。

若本次改动涉及相应路径，先把已有的登录返回、焦点恢复、缓存刷新、超时/取消定向用例放入quick，核对关键状态转换后再跑全量回归；只选择与本次影响面有关的边界，不为每个需求强制新增整套组合。负向复现的非零退出继续按原分类记录，不能当作通过证据放行最终检查。

本次修改共享composable、store、注入项或模块导出时，把直接受影响页面已有的定向测试加入quick，尽早核对mock、setup、全局stub、可选字段和真实模块实例是否匹配新接口，避免首次到完整单测才发现夹具契约漂移。scope包含这些实际依赖及运行器配置，不能只指纹业务文件。优先使用已有用例的子集；缺覆盖时才补本需求的最小行为用例，不另建全站夹具扫描或配置问卷。导入成功不代表页面行为通过，quick不能替代必要终验。后续夹具修正仍按真实影响面决定复测，原失败证据和QA轮次保留。

已登记计划可用 `verify --receipt <receipt> --plan <原plan引用> --name verify-r1 --summary '<真实业务结论与未验证项>'` 在同一进程逐项执行/严格复核并记录QA，避免拆成多个宿主命令导致环境漂移。仍有效的原证据经原 `run_planned` 校验后复用；实际环境（包括SHLVL）、输入、日志和最新失败均保留原校验，不回放旧环境。首个命令非零即停止安排后续检查，返回skipped_checks并照常记录缺项与CONCERNS；不能将跳过视为通过。每次使用新name，检查尝试名自动为 `<name>-c<序号>`，连同后缀须满足原64字符标识限制。默认仅生成证据/QA，不结束run。

只有用户已显式要求收口且宿主确已确认全部执行者停止时，才在同一条verify命令附加 `--finish --stopped --stop-evidence '<真实宿主停止证据>'`；无委派时可附 `--direct`。启动检查前先核验收口前提：停止证据、当前规则确认、已结束task、已闭合检查/宿主观察/角色派发、已解决人工等待、已有有效release收口及direct声明；未就绪先拒绝，不启动检查。PASS后调用原finish路径并在原归属锁内重新核验这些前提和当前QA完整输入/实际环境，再封存；有过角色派发不得用direct重标。预检不预先要求本次verify将生成的QA，不回放旧环境，也不能保证执行期间不会发生变化；CONCERNS保留活动run。统一入口的证据判断不代替独立业务QA或发布授权，原分开的run/qa/finish命令仍可用。

计划是本run登记的不可变报告；实际命令、目录和coverage不能在run时另行覆盖。相同检查ID可复用仍有效的结果；计划增项时，只有规范化定义与coverage完全相同才可承接，并保留原计划摘要和原始结果引用。输入、环境、日志、来源及最新失败仍逐次校验，不重贴旧PASS标签。每个声明检查都须有有效结果，不能用共享coverage让漏跑项通过。读取计划不再反复哈希所有输入；执行与QA仍检查当前输入。QA索引仅限本次操作，证据中途变化会拒绝该快照。

QA自动包含计划的全部coverage和继承的必要类别；当前任务已声明计划的必要类别也不能通过另建窄计划或手写QA删除；legacy仍沿用原run级要求，run级交付收据及显式继承要求不因任务切分被删除。真实范围变化时保存新版本并明确处理原有要求；缺项继续显示CONCERNS/coverage_missing。命令通过不能替代业务验收，独立QA仍核对真实行为和计划是否完整。专用浏览器等保留原证据接口，已继承且有效的记录无需改造成OS命令。

采用计划的CLI任务先生成当前QA判断，再finish。缺少当前计划的QA PASS，或完成前只读复核发现输入、环境、日志及后续观察已使证据失效时，finish保留活动run并给出下一步提示，不生成新的通过结论。需要暂停时仍可用原paused流程。环境不匹配时，QA及检查返回安全的差异诊断：只展示允许的运行环境键名，其他环境归为聚合变化；旧证据无诊断时标unknown。严格环境校验不变，不靠改标签、忽略未知环境或强写PASS消除CONCERNS。

Python对应接口：`check_plan.save_plan`、`run_planned`、`qa_from_plan`，以及 `verify_plan(root, run_id, owner, plan_ref, name, summary="")`；verify_plan只生成检查与QA证据，不自动finish。不得将凭据放进argv、计划environment或summary；environment只是身份信息，子进程仍由统一入口管理。
