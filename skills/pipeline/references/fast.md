# fast 流程

仅在父级 `SKILL.md` 的全部 fast 条件满足时使用。

1. 记录开始时间和 run_id；创建 `.active`，正常流程不创建 state.md。
2. 调度员生成不超过 5 行的 `context/developer.md` 变更卡：目标、允许文件、验收、快速命令、禁区。无需 plan.md。
3. 创建或唤醒 developer，传角色规则、base/run 与变更卡路径。developer 只实现变更卡，返回变更文件、实际自测命令和结果。
4. 调度员核对文件范围、验收和自测证据。符合即输出不超过 8 行的完成摘要与一条 Conventional Commits 建议；不创建 qa、acceptance、release-notes 或 retro。
5. 发现范围扩张、依赖/API/安全/部署/自动提交需求，或定向自测失败且不能在原变更卡内修复时，升级 standard：保留已有改动，补建 state、planner 和正式计划，不从头重做。
6. 追加 metrics.tsv 一行并删除 `.active`。异常/中止时写最小 aborted state 后清理 `.active`。
