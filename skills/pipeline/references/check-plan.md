# 同一份检查计划执行与验收

同一需求有多个稳定验收类别、需要开发与QA交接时使用。由智能体根据需求和契约维护，用户无需配置。单个简单检查继续用`check/report`，不强制另造计划。

计划一次声明命令、真实输入范围和coverage；`run`执行该定义，`qa`从相同定义核验证据并生成required_checks。重试仅换name，不改coverage。示例中路径、命令和超时须按项目实际替换：

```sh
python3 <runner> plan --receipt <receipt> --name acceptance --json '{"checks":{"unit":{"argv":["pnpm","test:unit"],"cwd":"web","scope":["web/src","web/tests","web/package.json","pnpm-lock.yaml"],"coverage":["unit"],"timeout":120,"write_paths":[]}}}'
python3 <runner> run --receipt <receipt> --plan <返回的plan引用> --id unit --name unit-r1 --impact-covered
python3 <runner> qa --receipt <receipt> --plan <同一plan引用> --name final --summary '<业务验收结论与未验证项>'
```

所有命令均接受`--project <root>`。cwd相对项目根，scope和write_paths仍相对项目根；省略cwd沿用项目根。不确定子包/运行器时先用`preflight --project <root> --cwd web -- pnpm test:unit`只读核对cwd与可执行文件；它不运行测试，也不证明脚本/依赖/业务通过。

计划是本run登记的不可变报告；实际命令、目录和coverage不能在run时另行覆盖。相同检查ID可复用仍有效的结果；计划增项时，只有规范化定义与coverage完全相同才可承接，并保留原计划摘要和原始结果引用。输入、环境、日志、来源及最新失败仍逐次校验，不重贴旧PASS标签。每个声明检查都须有有效结果，不能用共享coverage让漏跑项通过。读取计划不再反复哈希所有输入；执行与QA仍检查当前输入。QA索引仅限本次操作，证据中途变化会拒绝该快照。

QA自动包含计划的全部coverage和继承的必要类别；本run已声明计划的必要类别也不能通过另建窄计划或手写QA删除。真实范围变化时保存新版本并明确处理原有要求；缺项继续显示CONCERNS/coverage_missing。命令通过不能替代业务验收，独立QA仍核对真实行为和计划是否完整。专用浏览器等保留原证据接口，已继承且有效的记录无需改造成OS命令。

采用计划的CLI任务先生成当前QA判断，再finish。缺少当前计划的QA PASS，或完成前只读复核发现输入、环境、日志及后续观察已使证据失效时，finish保留活动run并给出下一步提示，不生成新的通过结论。需要暂停时仍可用原paused流程。环境不匹配时，QA及检查返回安全的差异诊断：只展示允许的运行环境键名，其他环境归为聚合变化；旧证据无诊断时标unknown。严格环境校验不变，不靠改标签、忽略未知环境或强写PASS消除CONCERNS。

Python对应接口：`check_plan.save_plan`、`run_planned`、`qa_from_plan`，参数与CLI一致。不得将凭据放进argv、计划environment或summary；environment只是身份信息，子进程仍由统一入口管理。
