# PIPELINE.md — 研发流水线项目契约(模板)

> 通用流水线(`/pipeline`)在本项目的接入口。所有流水线角色开工前必读。
> 本文件只声明「在哪、怎么验、什么不能碰」,具体内容以被引用的文档为准。
> 接入时逐项替换 ❏ 处内容,删除本说明行。
>
> 🛠 本契约依赖流水线工具:**[pipeline-standard](https://github.com/wzm111/pipeline-standard)**
> 新成员安装:`git clone` 该仓库后 `bash install.sh`(软链角色/命令/hook 到 ~/.claude/),本项目接入已由 `init-project.sh` 完成。

## ① 需求基线

- PRD:❏ 文档路径
- 验收追踪/验收标准:❏ 文档路径(如有)
- Roadmap / 里程碑:❏ 文档路径(如有)
- 视觉/交互基线:❏ 设计稿或 Demo 路径(如有)

## ② 架构与规范

- 架构基线:❏ 架构文档路径(最高优先级的技术决策必须在此声明)
- 工程约定:❏ 如项目根的 CLAUDE.md / CONTRIBUTING.md
- UI 设计辅助(可选):❏ 声明 developer 可调用的设计类 skill(如 ui-ux-pro-max,来源 `https://github.com/nextlevelbuilder/ui-ux-pro-max-skill`),处理视觉基线未覆盖的细节;不声明则 developer 不调任何 skill。附 git 来源后缺失时 /pipeline 自动安装到 ~/.claude/skills/(兼容仓库根或 `.claude/skills/<name>/` 子目录两种布局);无来源则缺失时降级不阻断
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

❏ 可选:推荐测试工具箱(按项目的测试面选用,取消注释并补执行目录;全局 CLI 型缺失时 /pipeline 自动安装,devDep 型需项目自行 `npm i -D`):

```bash
# ── 静态页 / UI ──
# pnpm test:e2e          Playwright E2E + 多断点截图/横向溢出实测(devDep 型,日常批次 gate)
# npx axe                @axe-core a11y 自动审计(devDep 型,日常/收口)
# lhci autorun           Lighthouse CI 性能硬指标 LCP/CLS(全局 CLI @lhci/cli,里程碑收口用)
#
# ── API ──
# curl -sf ...           接口冒烟:状态码/关键字段(系统自带,零成本,日常)
# pnpm test:contract     响应体 schema 契约校验(zod/ajv,devDep 型,日常)
# bru run                Bruno 接口集合回归,文本 collection 存仓库(全局 CLI @usebruno/cli)
# schemathesis run <spec-url>   OpenAPI 驱动的对抗式 fuzz 边界/异常用例(收口;需后端暴露 spec)
#
# ── 性能 ──
# k6 run <script>        压测(系统包,里程碑级)
#
# ── 角色工具 ──
# sg scan                ast-grep 结构搜索/codemod:B 拆解定位涉及文件、C 批量改写(全局 CLI @ast-grep/cli,日常)
# depcruise --validate   模块边界/依赖方向校验:A 验收「无违规」机器化(devDep 型 dependency-cruiser + 规则文件存仓库,收口)
# gitleaks detect        提交前密钥泄露扫描:E 闸口 2 前执行(系统包 brew,建议默认开)
#
# ── 条件启用(项目需要时再开) ──
# npx knip               死代码/未用导出/未用依赖扫描(项目体量大后,收口,零安装)
# npx size-limit         bundle 体积预算硬门禁(有体积验收条时,devDep 型)
# npm audit              依赖漏洞扫描(收口,零安装)
```

## ④ 上线方式

- **人工执行**:发布工程师(E)只产出变更摘要、Conventional Commits 提交建议与人工待办;禁止任何 git 写操作与部署动作。
- 人工提交后的收尾动作:❏(如更新追踪矩阵)
- 通知(可选):❏ 飞书/Slack webhook——只声明渠道,URL 走环境变量(如 `FEISHU_WEBHOOK_URL`),不写进本文件;声明后 /pipeline 在闸口等待/待裁决/完成三时机推送,未设环境变量静默跳过

## ⑤ 禁区与边界

- 流水线可写范围:❏ 目录;不可动:❏ 目录/文件。
- 禁止引入的依赖/模式:❏。
- 流水线中间产物统一放 `tmp/pipeline/`(确认 `tmp/` 已加入项目 .gitignore)。
- **硬拦截(可选,推荐)**:项目 `.claude/settings.json` 按 pipeline-standard README 配置 PreToolUse hook 后,下面的机器可读块在 /pipeline 运行期间生效(白名单模式:只允许写列出的前缀;或改用 `deny-write` 黑名单模式):

<!-- pipeline-guard:allow-write
❏ src/
❏ tmp/
-->
