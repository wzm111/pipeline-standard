# 角色模型：对话选择，按运行时执行

仅配置/查看模型或实际委派且存在配置时加载。本文件中的 JSON 是智能体维护格式，不要求用户填写。

## 对话与持久化

- 默认当前运行时、当前项目。用户只说“配置模型”时问“各角色想用什么模型？也可以说全部继承当前模型。”有实时可用列表时只展示少量对应选项；不要先问路径、作用域、全部角色、预算等一串问题。
- 自然语言角色映射：开发→developer，架构/规划→planner，架构评审/审查→scope_guardian，测试→tester，发布→releaser。中文“中/高/低”映射当前端支持的 effort 值；名称必须对应当前环境的真实模型 ID。
- 项目文件：项目根 `pipeline.models.json`。个人默认：`~/.config/pipeline-standard/models.json`（仅用户要求所有项目通用时写）。只这次：会话内覆盖。优先级：本次 > 项目 > 个人 > 运行时原有默认。
- 每个角色的 model/effort/fallback 是一个整体配置，上层定义角色时整体替换下层该角色；模型改动时不沿用旧模型的 effort，除非用户明确要求且新模型支持。仅修改 effort 时保留已解析 model 并校验组合。
- 未提及的角色、另一运行时和未知扩展字段必须保留。恢复继承写 `{"model":"inherit"}`，明确屏蔽下层该角色；“移除项目覆盖”才删除该角色条目并回到个人默认。范围不明确的“恢复默认”只问这一区别。
- 保存前解析已有 JSON，精确修改，使用编辑工具保存并回读验证；无效 JSON/未知 schema_version 不覆盖。不要修改安装仓库的共享角色文件、全局 Codex/Claude 设置来实现项目偏好。

文件结构示例（示例不自动保存）：

```json
{
  "schema_version": 1,
  "runtimes": {
    "codex": {
      "developer": {"model": "gpt-6.1-sol", "effort": "medium"},
      "planner": {"model": "gpt-6-astra", "effort": "high"}
    },
    "claude": {
      "tester": {"model": "inherit"}
    }
  }
}
```

model 必须是非空字符串。effort 可省略，表示使用选定模型/运行时默认；inherit 不携带 effort，表示继承父 agent 的模型与强度。fallback 可省略（默认不可静默替换），只有用户明确授权时设为 `inherit`。不要把 provider 模型名和推理档位跨端硬映射。

可用 `scripts/resolve-models.py --runtime codex --project <项目根>` 只读解析项目和个人有效配置；它校验格式与优先级，不验证账号模型权限。没有 Python 时按同一规则读取，不为设置安装依赖。

## 能力校验与应用

1. 从当前运行时的工具 schema、模型列表或官方配置能力校验模型和 effort；不要用历史名单断言账号可用性。未知或不支持时请用户选可用项，不保存成“已生效”。配置另一端且无法验证时可按用户指定保存，但明确“已保存，待该运行时验证”；首次使用必须验证。
2. 默认按需委派不变。仅配置某角色不创建它。当前 agent 直接开发时仍用主会话模型，报告“开发偏好已保存，仅委派时使用”。用户明确要求本次开发必须用指定模型时可委派一个 developer，不恢复整条五角色链。
3. **Codex**：创建子 agent 时将 model 与 effort 转为当前 spawn 工具支持的字段（例如 model、reasoning_effort）；当前工具若要求覆盖模型时不能完整 fork，使用 fork_turns=none 并传最小 base/run/角色差量。不能只把模型名写进 prompt 假装已切换。若工具不支持选模，说明限制，不自动改成侧栏任务或外部 API。
4. **Claude Code**：优先使用当前 Agent 调用支持的模型覆盖。没有该覆盖但支持自定义角色文件时，由智能体在项目 `.claude/agents/` 为所选角色生成专用 `pipeline-configured-<role>.md`，复制原角色规则并设置当前版本支持的 model/effort frontmatter，使用独立名称避免覆盖已有角色；已有非本工具维护文件不可覆盖。仅按运行时实际支持字段写入，不通过全局 effort 设置影响其他角色。加载需刷新时说明“下次会话生效”，不冒充已加载。继承配置需显式覆盖原角色的固定 model 或使用受支持的 inherit 角色。
5. Claude 项目配置明确要求模型/角色文件时，写入仍遵守项目禁区与运行时权限；没有权限或活动 run 阻止变更时保存待办，不放宽白名单或移除 .active。未配置角色保留旧默认，确保兼容。
6. 模型不存在、无权限或组合无效：只在该角色实际需要时阻止其启动，给可操作的选择；不得重复尝试多个昂贵模型。仅明确允许 fallback=inherit 时回退并说明。当前已运行的 agent 不因文件变更自动切换；下一次创建生效，不为配置变更中断工作。
7. 把 role、requested_model/effort、传入参数、runtime 报告的 actual_model/effort、fallback 写入本 run 的 context/models.json（仅委派时）；运行时不返回实际模型则标 unknown，不能把请求值当实际值。展示设置时区别“保存的偏好”与“本次实际使用”。

参考：[Codex 子 agent](https://learn.chatgpt.com/docs/agent-configuration/subagents)、[Claude Code 子 agent](https://code.claude.com/docs/en/sub-agents)。能力以当前宿主为准。
