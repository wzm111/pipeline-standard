# 当前run的报告与验证复用

生成模型、QA、验收报告或决定复用检查时读取。由智能体维护，不让用户填写run_id、路径或指纹，不为记录增加角色。

## 报告归属

新报告必须带当前run_id、kind和recorded_at。优先使用标准库模块`scripts/run_evidence.py`：

```python
from run_evidence import write_report, read_report
path = write_report(root, run_id, owner_token, "qa", "batch1-r1",
                    {"gate": "PASS", "required_checks": ["unit"], "summary": "..."})
report = read_report(root, run_id, path, kind="qa")
```

脚本按活动run身份写入`tmp/pipeline/runs/<run_id>/artifacts/<kind>/<name>.json`并注册到state.artifacts。payload可为结构化数据或Markdown正文。每轮用新名称，旧轮保留；不反复复制历史全文，索引只指向本run最新轮报告。模型反馈同样用kind=models记录，不从请求值推定实际值。

下文及角色规则中的`qa/index.md`、`qa/final.md`、`context/models.json`等是旧逻辑名；脚本模式交接时传实际run报告路径，以本规则为准，不要求生成两份。若保留便捷入口，只写run_id与实际报告链接，不覆盖旧报告正文。旧计划/报告只有路径、时间或mtime都不证明属于本run；归属未确认时不作为成功证据，按缺口核验，不能直接套上新run_id认领。

`write_report`只允许活动run拥有者写新报告，拒绝重复名称、越界路径、内部软链接和payload中的owner_token字段。报告与状态分别原子写入，非跨文件事务；中断时检查已落盘报告及注册情况，不盲目覆盖或删锁。owner_token仅用于本地函数调用，不放入报告或交接prompt。无Python时由智能体在报告头部保留同样归属和时间，手动核验，不能声称已由脚本校验。

## 成功检查何时可复用

前台检查优先用`scripts/run_checks.py`的统一入口；用户仍只说需求，智能体复用当前run身份，不让用户传参数或编写配置。一个调用完成归属校验、冲突检查、执行前后指纹、阶段切换/恢复、计时、日志及报告保存：

```python
from run_checks import execute_check, check_summary
result = execute_check(root, run_id, owner_token, "unit-r1",
                       argv=["pnpm", "test:unit"],
                       scope=["src", "tests", "package.json", "pnpm-lock.yaml", "vite.config.ts"],
                       environment={"node": actual_node_version}, write_paths=[".nuxt"], coverage=["unit"])
# 范围已确认时impact_covered=True，后续新名称调用自动查找有效结果。
totals = check_summary(root, run_id)
```

argv是原有获授权检查的参数列表，脚本不做shell展开或新增执行权限。需要管道等shell语义时由智能体明确选择shell命令并遵守原权限。检查前后自动进入/退出测试阶段，搜索/定位保留原执行阶段；完整输出只进日志，返回简短结果。工具按子进程目录解析PATH并核验身份，不可识别时禁止复用。进程环境仅保存整体hash，不存原值，owner凭证不传给子进程。

environment补充本次实际使用的运行时、工具版本和必要外部状态标识，不含凭据。scope取自命令真实依赖范围；目录指纹包含新增/删除文件及可执行权限。两个证据接口共用input_fingerprint.py扫描与读取一致性校验，保留原有digest编码。`impact_covered=True`只能在确认当前变更及外部依赖均被记录范围覆盖时提供；脚本不会推断语义影响。未覆盖的服务数据、浏览器状态或未知环境变化均需补测。检查过程中输入或工具变化会标inputs_stable=false，不能在稍后恢复原文件后重新认领该PASS。

必须通过浏览器等专用工具执行时，可保留run_evidence.py的fingerprint/record_check/reusable_check接口，由智能体记录真实结果；不伪称专用工具已经经由本地执行入口运行。没有Python时按报告头部规则手动核验。

复用要求原exit_code=0且命令/环境/输入/日志指纹相同、影响面明确；默认只查本run登记的证据。impact_covered=True时入口自动查找最新匹配结果，无需手动传reuse_from；命中记check_reuse并跳过执行。跨run仅允许按[交付承接](delivery.md)显式继承并验证来源链，不能直接读取别的run报告作为本轮PASS。底层reusable_check仍只读，统计区分继承与实际执行。必要全量门禁在冻结后集中收口；最终业务产物验收保留。

coverage标明该命令实际覆盖的验收类别（如unit/e2e/build/browser/metadata），QA required_checks来自本需求及契约。交付凭证逐项列required/covered/missing及实际来源；metadata检查不能补足unit，未声明的旧证据完整性为unknown。缺项只补对应检查，不丢弃其他有效证据；后续提交阶段不能清掉继承的原验收要求。标签变化不会直接重贴旧PASS，仍需实际匹配的证据。

## 失败分类与角色开销

intent按实际用途选择test/search/locate/reproduce/check。入口记录原始退出码和result_class，区分成功、搜索无匹配、定位失败、故障复现非零、测试失败、一般命令失败、工具不可用、超时和中断。test_failure表示测试命令非零，不自动判定业务回归或QA FAIL。

需要区分环境、测试夹具与真实回归时，用`review_check(root, run_id, owner_token, name, source, result_class, reason)`写有依据的独立诊断；result_class为environment_failure/fixture_failure/regression_failure。check_summary同时保留原始及诊断分类、执行与复用次数，不改原结果、不自动增加或重置QA打回轮次，也不把并行命令时长相加。

角色事件用role_metrics.py的`record_role_event(root, run_id, owner_token, role, executor_id, event)`记录；executor_id为本run内稳定短标识。宿主实际创建/续用后记created/reused；交接、工作或等待开始记preparing/working/waiting，本次角色工作完成记stopped。重复相同phase不会反复写盘；stopped后再次工作须先记reused，不为记录额外创建角色。

`role_metrics.summary(root, run_id)`只读汇总角色创建/续用计数、各phase区间并集及全部角色墙钟并集。不同角色/phase可能重叠，不能把各列相加当总时间。未观测的阶段为null，既有角色首次reused不补造created；running中断恢复排除缺停止边界的旧区间，历史缺失标partial/unknown。脚本聚合已上报事件，不能自动获知宿主未上报的等待。

## QA与并行

- 首次QA通常在首批可测产物冻结后创建。确有独立可执行的验收准备工作时可提前启动，交接写清产出和结束条件，完成后复用同一角色；不创建角色只让它空等，也不为测等待再增角色。
- QA测上一批、开发下一批，只在已交付输入冻结且生成目录、浏览器profile/端口等资源不冲突时并行。先看命令真实配置，不因源码文件分离就认定安全。
- `write_paths_conflict(root, left, right)`检查已声明写目录的相同/父子重叠；任一集合未知按冲突处理。共用`.nuxt`、`.output`、`dist`等目录时顺序执行，或使用工具明确支持的隔离目录。它是调度判断辅助，不锁进程，也不验证未声明资源；隔离证据不足就顺序运行。
- 统一入口在活动state中排他登记running_checks，未知或重叠写集合会拒绝另一个合作检查；已确认隔离时才传不同写目录。端口、浏览器及非合作命令仍由调度员核对。只运行前台有限任务；可选timeout在POSIX终止本次进程组，不能用于启动长期服务。宿主中断后遗留claim不自动清除；确认全部执行者停止后用run_state.resume恢复，它才清理旧claim。日志保留，报错不伪装成功。
- 命令非零先分辨定位错误、搜索无匹配、环境/测试隔离问题、故障复现和真实回归。范围外半成品不判本批失败；已给出的QA FAIL仍计打回轮次，不能重命名批次或改分类重置最多3轮上限。可修正错误的测试假设，但保留真实业务断言和修正依据。
