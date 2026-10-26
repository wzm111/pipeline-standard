# fast 流程

仅在父级 `SKILL.md` 的全部 fast 条件满足时使用。

1. 记录开始时间和 run_id；创建 `.active`，正常流程不创建 state.md。
2. 当前 agent 在会话内明确目标、允许文件、验收、快速命令和禁区；无需变更卡文件、plan 或角色交接包。
3. 当前 agent 读取 developer 角色规则，直接实现并实际运行定向验证；上下文路径要求仅适用于委派，无需创建 developer 子 agent。
4. 调度员核对文件范围、验收和自测证据。符合即输出不超过 8 行的完成摘要与一条 Conventional Commits 建议；不创建 qa、acceptance、release-notes 或 retro。
5. 发现范围扩张、依赖/API/安全/部署/自动提交需求，或定向自测失败且不能在原范围内修复时，升级 standard（高风险升级 thorough）：保留已有改动，补建 state，按新档位选择所需角色，不从头重做。
6. 追加 metrics.tsv 一行并删除 `.active`。异常/中止时写最小 aborted state 后清理 `.active`。
