# PIPELINE.md — 研发流水线项目契约(模板)

> 通用流水线(`/pipeline`)在本项目的接入口。所有流水线角色开工前必读。
> 本文件只声明「在哪、怎么验、什么不能碰」,具体内容以被引用的文档为准。
> 接入时逐项替换 ❏ 处内容,删除本说明行。

## ① 需求基线

- PRD:❏ 文档路径
- 验收追踪/验收标准:❏ 文档路径(如有)
- Roadmap / 里程碑:❏ 文档路径(如有)
- 视觉/交互基线:❏ 设计稿或 Demo 路径(如有)

## ② 架构与规范

- 架构基线:❏ 架构文档路径(最高优先级的技术决策必须在此声明)
- 工程约定:❏ 如项目根的 CLAUDE.md / CONTRIBUTING.md
- 其他规范:❏

## ③ 测试命令

❏ 测试/验证命令及其执行目录,例如:

```bash
# 在 ❏ 目录下执行
❏ pnpm lint
❏ npx tsc --noEmit
❏ pnpm test
❏ pnpm build
```

> 约定:本节引用的外部 CLI(如 `fg-core`)由 /pipeline 启动时自检,缺失自动全局安装,装失败则跳过对应项并在测试报告注明,不阻断流水线。

## ④ 上线方式

- **人工执行**:发布工程师(E)只产出变更摘要、Conventional Commits 提交建议与人工待办;禁止任何 git 写操作与部署动作。
- 人工提交后的收尾动作:❏(如更新追踪矩阵)

## ⑤ 禁区与边界

- 流水线可写范围:❏ 目录;不可动:❏ 目录/文件。
- 禁止引入的依赖/模式:❏。
- 流水线中间产物统一放 `tmp/pipeline/`(确认 `tmp/` 已加入项目 .gitignore)。
- **硬拦截(可选,推荐)**:项目 `.claude/settings.json` 按 pipeline-standard README 配置 PreToolUse hook 后,下面的机器可读块在 /pipeline 运行期间生效(白名单模式:只允许写列出的前缀;或改用 `deny-write` 黑名单模式):

<!-- pipeline-guard:allow-write
❏ src/
❏ tmp/
-->
