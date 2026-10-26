# 运行时验证记录

日期：2026-10-26。本表区分本地配置验证、宿主预检、实际调用与实际模型确认。

| 环境 | 观察 | 能证明什么 | 尚不能证明什么 |
| --- | --- | --- | --- |
| 当前 Codex 会话 | spawn 工具接受 planner 请求 gpt-6-astra / medium，返回任务句柄，任务完成 | 指定参数的子 agent 请求能运行并完成只读规划 | 工具未返回服务端实际模型 ID，actual_model=unknown |
| 当前 Codex 会话 | developer 请求 gpt-6.1-sol / medium，返回任务句柄并完成实现与本地行为测试 | 请求被接受且任务可执行 | 工具未返回实际模型，actual_model=unknown |
| 当前 Codex 会话 | guardian 请求 gpt-6-astra / high，完成独立只读审查 | 指定参数的审查请求可完成 | 实际模型与effort未由宿主反馈，均为unknown |
| Codex CLI | `codex --version` 返回 0.164.0-alpha.11.1 | 可运行该 CLI 的版本查询 | 与桌面会话完全相同的 agent 能力、账号配额 |
| Claude CLI | `claude --version` 返回 2.1.269；auth status 返回 loggedIn=true | 有版本与登录信息 | 模型能够启动 |
| Claude 最小探测 | `claude -p --safe-mode --tools '' --no-session-persistence --output-format json --model sonnet` 在 /tmp 退出 1，提示 native binary not installed | 此次执行失败，不能宣布两端真实调用验收通过 | 不能据此断言 sonnet 不存在或账号无权限 |

Claude 探测只请求回复固定字符串，不给文件/命令工具，不修改项目或全局模型设置。没有自动重装全局 CLI；需先修复现有启动环境再验收。这里的模型名是探测请求，不是用户的 Claude 持久偏好。

主会话没有程序化换模，项目 pipeline.models.json 仅控制实际委派的角色。保存项目偏好不修改个人配置、其他项目或 Claude 设置。
