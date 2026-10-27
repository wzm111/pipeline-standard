# 角色模型：对话选择，按运行时执行

配置/查看模型或实际委派时加载。skill始终附带默认配置，项目无文件也要解析。本文件中的JSON是智能体维护格式，不要求用户填写。

## 对话与持久化

- 默认当前运行时、当前项目。用户只说“配置模型”时问“各角色想用什么模型？也可以说全部继承当前模型。”有实时可用列表时只展示少量对应选项；不要先问路径、作用域、全部角色、预算等一串问题。
- 自然语言角色映射：开发→developer，架构/规划→planner，架构评审/审查→scope_guardian，测试→tester，发布→releaser。中文“中/高/低”映射当前端支持的 effort 值；名称必须对应当前环境的真实模型 ID。
- skill模板：`<skill真实目录>/defaults/models.json`，随安装提供。安装时初始化本机默认`~/.config/pipeline-standard/models.json`；已有文件在升级/重装时保留，不覆盖。项目文件：项目根`pipeline.models.json`，优先覆盖同一角色的本机默认；未配置的角色继续使用本机默认，不因存在一个项目文件就清空其余角色。只这次：会话内覆盖。
- 完整优先级：本次 > 项目 > 本机默认 > skill模板 > 运行时原有默认。模板作为旧安装缺少本机文件时的兼容兜底。每次解析显示来源；内部source=personal表示本机默认，skill_default表示模板。用户说“改本机默认/所有项目通用”时由智能体更新本机文件，不修改会随升级更新的skill代码目录。
- 新安装模板的两端五角色均为inherit，不写死任何模型版本或effort。智能体按用户选择将当前宿主可用型号保存为本机或项目偏好；Codex/Claude分别保存，不把某台机器的选择当通用推荐。继承只表示用父模型，不虚构不同角色已使用不同型号。
- 升级可能移除用户保存的型号或改变effort支持，因此实际创建角色前仍需当前宿主预检；旧选择继续保留但不等于仍可用。不可用时给“重选可用模型/继承当前/暂不执行该角色”的简短选择，确认后更新实际来源的配置，不能擅自永久换模。来源为skill_default时写本机文件，不改只读模板；若项目覆盖导致失败，只改该项目。权限/网络/型号不存在分别处理；不因升级每次探测所有角色。
- 每个角色的 model/effort/fallback 是一个整体配置，上层定义角色时整体替换下层该角色；模型改动时不沿用旧模型的 effort，除非用户明确要求且新模型支持。仅修改 effort 时保留已解析 model 并校验组合。
- 未提及的角色、另一运行时和未知扩展字段必须保留。恢复继承写`{"model":"inherit"}`，明确屏蔽下层该角色；“用skill默认/移除项目覆盖”删除该角色项目条目并回到本地默认（有本机自定义时应展示该来源）。范围不明确的“恢复默认”只问这一区别。
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

创建单个角色时，智能体优先使用`--role tester --compact`，只返回当前角色的preference/source/path，避免每次转发五个角色的完整配置；查看全部设置时仍使用原命令。此为智能体内部接口，不要求用户记参数。

需要转换启动参数时，可加`--host-evidence <本run宿主能力.json>`：Codex返回launch_plan.parameters，Claude返回独立角色frontmatter修改参数。该文件由智能体依据当前宿主真实能力准备，不要求用户填写，不把历史名单或型号可见当作权限已确认。只支持与--role配合，不能与--read混用；权限未知/型号失效/不支持的强度会拒绝生成成功计划，显式授权的fallback=inherit才可回退。Claude结果的effort=null表示删除旧字段，不应原样写入frontmatter。所有模式均只读，不创建角色、不写设置，预检verified=false；实际执行仍由运行时工具完成并单独记录反馈。

解析器通过自身真实位置定位defaults/models.json，不能依赖当前工作目录或借用pipeline-standard项目根的pipeline.models.json。每个角色返回source/path：skill_default、personal、project或temporary。默认文件缺失/损坏应提示修复安装，不静默返回空roles。安装是完整skill目录链接，默认文件无需拷贝到每个项目；读取它不写项目或个人文件。正在运行的角色不因默认文件更新自动换模。

### 本地可靠工具（不启动模型）

`scripts/model_config.py` 只用 Python 标准库。CLI 仍然只读：`--read` 返回项目文档及 revision；`--temporary '{"tester":{"model":"inherit"}}'` 添加本次覆盖，不写文件。临时覆盖采用当前运行时的角色对象，完整替换对应角色配置。

智能体先读取 `read_document(path)` 获取 `(document, revision)`，用 `prepare_update(document, runtime, role, changes)` 生成精确更新。`remove=True` 删除该端该角色的覆盖；`{"model":"inherit"}` 屏蔽低层配置。仅改 effort 且项目未定义该角色时，传入已解析的 `effective` 偏好；切换 model 自动清除旧 effort/fallback，明确传入的新值才保留。格式校验不证明模型与 effort 的实际可用性，应用前还要能力预检。

保存前由当前智能体复核用户授权、目标项目契约与写权限。可通过编辑工具保存已生成的局部文档并回读；需要可靠原子保存时调用 `save_project(project_root, updated_document, revision)`。该函数固定写指定项目根的 `pipeline.models.json`，没有 CLI 写入口，也没有个人/全局配置写入口；不会自动放宽权限、删除活动标记或生成角色文件。项目目录先解析为实际目录，配置文件、内部控制路径和锁文件拒绝符号链接；损坏 JSON、未知版本、活动 `tmp/pipeline/.active`、旧 revision 和现有 `.pipeline.models.lock` 均拒绝保存。临时文件 fsync 后原子替换并回读，失败清理自己的锁与临时文件。锁存在时先查明来源，不自动删除别人的锁。

revision 在保存前和替换前两次检查，同模块写入由排他锁串行化，可发现常规外部编辑；非协作进程仍可能在最后检查与 replace 之间抢写。此保护不承诺对抗恶意并发写入，也不提供断电后目录元数据持久化保证。若替换后回读报错，检查当前文件，不盲目覆盖或回滚其他进程的修改。

宿主适配器提供当前工具事实，例如以下对象（型号键仅为输入示意，不是推荐名单）：

```json
{
  "agent_spawn": true,
  "model_override": true,
  "effort_override": true,
  "fork_with_override": false,
  "models_complete": false,
  "models": {"host-reported-id": {"permission": null, "efforts": ["medium"]}}
}
```

`assess_capability(preference, host)` 返回 `available_precheck`（可用预检）、`unavailable`（不可用）、`unverifiable`（无法验证）、`host_unsupported`（宿主不支持）。缺能力、缺名单、权限未知均不提升为已验证；所有预检结果的 `verified` 都为 false。`agent_spawn` 区分继承模型与宿主能否创建 agent；继承无需独立选模，但仍需启动能力。只有完整名单明确缺项才判型号不可用，名单不可读不等于型号不存在。

`codex_spawn_parameters(preference, host)` 是纯转换函数，输出 `parameters`、预检状态及回退原因；显式型号转换为 `model` / `reasoning_effort`，宿主未明确支持带覆盖的 fork 时加入 `fork_turns=none`。预检不通过时只有配置明确 `fallback=inherit` 且宿主可创建 agent 才输出继承参数，否则报错。函数不创建 agent、重试或调用外部 API。真实启动失败由调度员按已有规则处理，网络/限流/宿主二进制错误不得改写为型号不存在或自动永久换模。

`claude_role_parameters(preference, host)` 是 Claude 专用角色 frontmatter 的纯参数适配；宿主还须明确报告 `role_frontmatter=true`，继承须报告 `role_inherit=true`。返回 `parameters` 包含 model/effort；其中 null 表示调用方必须删除旧字段，不能把 null 直接写进 frontmatter。继承始终明确写 `model: inherit` 并删除旧 effort，压过来源角色的固定 model；显式模型不含 effort 时也删除旧 effort。权限未知、effort 未知/不支持时拒绝应用，只有明确 fallback=inherit 且宿主支持创建与角色继承才回退。调用方负责将参数合入独立角色、保留原规则、检查文件冲突和写权限，以及宿主加载/刷新；函数不解析 YAML、不写文件、不加载角色。

启动后用 `execution_record(runtime, role, preference, plan, reported)` 生成记录，再由调度员按活动 run 的权限保存。`reported` 接受 `actual_model`、`actual_effort`、`startup_status`、`error`；没有实际反馈填 unknown，绝不从请求值推定。该模块提供两端纯参数适配，未实现 Claude 角色文件生成与宿主加载，也未完成 Claude/Codex 两端真实模型切换验收；本地行为测试不能替代第二期整体验收。

## 能力校验与应用

1. 从当前运行时的工具 schema、模型列表或官方配置能力校验模型和 effort；不要用历史名单断言账号可用性。分别记录宿主支持、账号权限和实际启动结果，状态为“可用（预检）/不可用/无法验证/宿主不支持”。名单可见不是启动成功，官方介绍不是账号权限证据。未知或不支持时请用户选可用项，不保存成“已生效”。配置另一端且无法验证时可按用户指定保存，但明确“已保存，待该运行时验证”；首次使用必须验证。
2. 默认按需委派不变。仅配置某角色不创建它。当前 agent 直接开发时仍用主会话模型，报告“开发偏好已保存，仅委派时使用”。用户明确要求本次开发必须用指定模型时可委派一个 developer，不恢复整条五角色链。
3. **Codex**：创建子 agent 时将 model 与 effort 转为当前 spawn 工具支持的字段（例如 model、reasoning_effort）；当前工具若要求覆盖模型时不能完整 fork，使用 fork_turns=none 并传最小 base/run/角色差量。不能只把模型名写进 prompt 假装已切换。若工具不支持选模，说明限制，不自动改成侧栏任务或外部 API。
4. **Claude Code**：优先使用当前 Agent 调用支持的模型覆盖。没有该覆盖但支持自定义角色文件时，由智能体在项目 `.claude/agents/` 为所选角色生成专用 `pipeline-configured-<role>.md`，复制原角色规则并设置当前版本支持的 model/effort frontmatter，使用独立名称避免覆盖已有角色；已有非本工具维护文件不可覆盖。仅按运行时实际支持字段写入，不通过全局 effort 设置影响其他角色。加载需刷新时说明“下次会话生效”，不冒充已加载。继承配置需显式覆盖原角色的固定 model 或使用受支持的 inherit 角色。
5. Claude 项目配置明确要求模型/角色文件时，写入仍遵守项目禁区与运行时权限；没有权限或活动 run 阻止变更时保存待办，不放宽白名单或移除 .active。未配置角色保留旧默认，确保兼容。
6. 模型不存在、无权限或组合无效：只在该角色实际需要时阻止其启动，给可操作的选择；不得重复尝试多个昂贵模型。仅明确允许 fallback=inherit 时回退并说明。当前已运行的 agent 不因文件变更自动切换；下一次创建生效，不为配置变更中断工作。
7. 把 role、requested_model/effort、传入参数、runtime 报告的 actual_model/effort、fallback 写入本 run 的 context/models.json（仅委派时）；运行时不返回实际模型则标 unknown，不能把请求值当实际值。展示设置时区别“保存的偏好”与“本次实际使用”。

## 不可用时的最短处理

- 模型不存在/权限拒绝：“当前不能使用该模型。用当前模型继续，还是从可用列表重选？”不要擅自保存 fallback。
- 没有名单：“当前工具无法读取可用列表。可以继承当前模型；你也可以提供已经能用的型号。”不让用户编辑文件，不猜测候选。
- 宿主没有独立选模：“此环境不能按角色切换模型，可以用当前模型继续。”用户选择后才应用；不通过外部 API 绕过。
- 网络/限流：“本次启动失败，尚不能判断模型不可用。”按既有重试上限处理，不改永久偏好。
- 用户选择“先不设置”：保留原配置，结束设置；若原需求仍能用默认模型完成则继续，不启动失败的必需角色。

参考：[Codex 子 agent](https://learn.chatgpt.com/docs/agent-configuration/subagents)、[Claude Code 子 agent](https://code.claude.com/docs/en/sub-agents)。能力以当前宿主为准。
