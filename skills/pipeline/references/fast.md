# fast 流程

使用run_state.py时保留最小归属状态runs/<run_id>.json，不另建state.md；下文“无state”指不生成额外规划/角色状态。结束先核实本run执行者停止，再按归属清理自己的标记。

仅在父级 `SKILL.md` 的全部 fast 条件满足时使用。

1. 读取项目契约后用现有 CLI start 创建最小 run，保存返回的 receipt；读取本轮规则并 policy-ack 后继续。不另建 state.md、手写计划文档或角色交接文件。
2. 当前 agent 在会话内明确目标、允许文件、验收、快速命令和禁区；无需变更卡文件、手写 plan 文档或角色交接包；收口入口可内部登记机器检查计划。
3. 当前 agent 读取 developer 角色规则，直接实现并实际运行定向验证；上下文路径要求仅适用于委派，无需创建 developer 子 agent。
   实际涉及 UI 时先用[UI 设计与现有视觉基线](visual.md)，自动选用适用的已可用设计 skill 并保留真实视觉自查；无需用户补截图、手动配置或额外设计角色。
4. 调度员核对文件范围、验收和自测证据。单项检查沿用下方轻量机器验收，不创建独立 QA 角色或手写 QA 文档；不省略 managed run 的验收记录。符合后输出不超过 8 行的完成摘要与一条 Conventional Commits 建议；不另建 acceptance、release-notes 或 retro。
5. 发现范围扩张、跨模块耦合/多入口一致性等复杂度信号、依赖/API/安全/部署/自动提交需求，或定向自测失败且不能在原范围内修复时，升级standard（高风险升级thorough）：保留当前run与已有改动，按standard规则自动选择必要角色，不从头重做。
6. 确认实际执行者停止后调用原 finish，核验当前验收并按归属收口；不手动删除标记绕过门禁。异常/中止沿用原状态入口，保留失败，不标完成。UI摘要仍按[视觉交付](visual.md)展示已经实际查看的图片；收口受阻时也如实给图和验收缺口。

## 单项检查的轻量收口

改前需要基线时用 `check --intent baseline` 记录真实退出码、日志及耗时，保留原失败历史；它不属于最终验收，也不进入验收 plan。修改后通过下方 verify 按实际覆盖执行或严格复核最终 `test`/`check`。`coverage` 仍描述实际内容，不能通过把标签改成 `baseline` 排除旧的正式检查或既有要求。

输入冻结、已有收口授权且宿主已确认全部执行者停止后，优先用一条 `verify --json ... --finish`：入口登记同名机器检查计划，在同一进程执行/严格复核、记录 QA 并调用原 finish，避免分次调用产生环境漂移或手抄报告路径。不因内部计划升级 standard、派角色或重跑仍有效的检查。

```sh
python3 <runner> verify --project <root> --receipt <receipt> --name final-r1 --json '{"checks":{"unit":{"argv":["<实际运行器>","<检查参数>"],"scope":["<真实源码>","<测试及运行器依赖>"],"coverage":["unit"],"write_paths":[]}}}' --summary '<实际业务验收结论与未验证项>' --finish --direct --stopped --stop-evidence '<实际执行者停止依据>'
```

示例 argv/scope/写集须按真实命令与依赖替换；只有确实无写操作才声明 `write_paths:[]`。`PASS` 仍须由真实业务验收支持，不能直接照抄 summary。有委派时省略 direct 并先关闭派发；没有收口授权或停止证明时不传 finish/停止选项。每轮使用新 name，失败保留活动 run 和原记录。单项例子不覆盖多检查/多类别：确有多项验收时按[检查计划](check-plan.md)完整声明，或传已返回的实际 plan 引用；不删检查来套单项示例。原 `check/report/finish` 分离入口仍可用，实际环境、输入或日志变化时按原门禁补必要复核。
