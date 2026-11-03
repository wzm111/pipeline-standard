# fast 流程

使用run_state.py时保留最小归属状态runs/<run_id>.json，不另建state.md；下文“无state”指不生成额外规划/角色状态。结束先核实本run执行者停止，再按归属清理自己的标记。

仅在父级 `SKILL.md` 的全部 fast 条件满足时使用。

1. 读取项目契约后用现有 CLI start 创建最小 run，保存返回的 receipt；读取本轮规则并 policy-ack 后继续。不另建 state.md、计划或角色交接文件。
2. 当前 agent 在会话内明确目标、允许文件、验收、快速命令和禁区；无需变更卡文件、plan 或角色交接包。
3. 当前 agent 读取 developer 角色规则，直接实现并实际运行定向验证；上下文路径要求仅适用于委派，无需创建 developer 子 agent。
4. 调度员核对文件范围、验收和自测证据。单项检查沿用下方轻量机器验收，不创建独立 QA 角色或手写 QA 文档；不省略 managed run 的验收记录。符合后输出不超过 8 行的完成摘要与一条 Conventional Commits 建议；不另建 acceptance、release-notes 或 retro。
5. 发现范围扩张、跨模块耦合/多入口一致性等复杂度信号、依赖/API/安全/部署/自动提交需求，或定向自测失败且不能在原范围内修复时，升级standard（高风险升级thorough）：保留当前run与已有改动，按standard规则自动选择必要角色，不从头重做。
6. 确认实际执行者停止后调用原 finish，核验当前验收并按归属收口；不手动删除标记绕过门禁。异常/中止沿用原状态入口，保留失败，不标完成。

## 单项检查的轻量收口

使用现有 CLI 的 `check → report → finish`：`check` 返回完整报告引用；`report` 传入该引用和一个稳定覆盖类别，机器核对当前输入、环境、日志及最新失败；`finish` 再核验收口条件。不因这条内部验收记录升级 standard、派角色或重跑仍有效的检查。

```sh
python3 <runner> check --project <root> --receipt <receipt> --name unit-r1 --scope <真实源码及测试依赖> --coverage unit --impact-covered -- <实际检查命令及参数>
python3 <runner> report --project <root> --receipt <receipt> --kind qa --name final --json '{"gate":"PASS","required_checks":["unit"],"checks":["<check返回的完整report引用>"]}'
python3 <runner> finish --project <root> --receipt <receipt> --direct --stopped --stop-evidence '<实际执行者停止依据>'
```

`PASS` 仍须由真实业务验收支持，不能直接照抄。单项例子不覆盖多检查/多类别：确有多项验收时使用[检查计划](check-plan.md)的现有入口；不删检查来套单项示例。证据变化则保留拒绝并补必要复核，不放宽 QA 或原失败上限。
