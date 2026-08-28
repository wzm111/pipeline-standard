# pipeline-standard — 通用研发流水线标准

端到端「需求 → 开发 → 测试 → 上线」多角色流水线,方法论通用,项目差异由项目根的 `PIPELINE.md` 契约注入。

## 组成

```text
agents/                     5 个通用角色(软链到 ~/.claude/agents/)
  pipeline-scope-guardian   A 范围守门员(只读):审计划、验收,对照需求基线与规范
  pipeline-planner          B 任务拆解员:先澄清歧义,再拆成带验收标准的任务清单
  pipeline-developer        C 开发工程师:只按计划做,checkbox 追踪进度
  pipeline-tester           D 测试工程师:实测 + 对抗式找茬,gate 三态结论,只报不改
  pipeline-releaser         E 发布工程师:变更摘要 + 提交建议,不动 git/不部署
skills/pipeline/SKILL.md    /pipeline 斜杠命令:编排流程、两道人工闸口、规模分流、team 直聊
hooks/pipeline-guard.sh     禁区硬拦截 hook(PreToolUse),流水线运行期间生效
templates/PIPELINE.md       项目契约模板(❏ 占位符)
install.sh                  安装/同步脚本(软链 agents/skills/hooks 到 ~/.claude/)
init-project.sh             项目接入脚本(拷契约模板 + 配 hook + gitignore,幂等)
```

## 流程特性

- **澄清前置**:planner 发现实质性歧义先提问,不带着猜测拆计划
- **规模分流**:小任务(≤2 文件、无新依赖)走快速通道,跳过范围评审
- **批次级循环**:任务 >8 个时计划按批次组织(每批 ≤10 任务,plan.md 索引 + plan-<批次>.md 详情),每批 dev 交付即测;qa 测上一批与 dev 开下一批**并行重叠**,fail-fast 不攒到最后,单批打回 ≤3 轮
- **批次进度简报**:每批 gate 向用户一行简报(进度 N/M + gate + 下一步);阶段转换(过半/全批交付/异常停止)再发**表格式快照**(批次状态+实测节奏+修正 ETA),长跑进度始终可见
- **闸口 1 ETA**:计划头部带任务数/批次数/预估时长量级;里程碑级计划附「拆分建议」,人工决定整体跑还是切片跑
- **team 直聊**:开发⇄测试打回循环由两个 teammate 直接 SendMessage,调度员只监控轮次与裁决,不经手中转(省上下文;异常时回退中转模式)
- **上下文瘦身**:plan 按批拆文件、demo/大文档按需切片检索,控制各角色冷启动读入量
- **QA gate 三态**:`PASS / CONCERNS / FAIL`;CONCERNS 的非阻断问题交人工裁决豁免与否
- **禁区硬拦截**:hook 在 `/pipeline` 运行期间(存在 `tmp/pipeline/.active` 标记)按 PIPELINE.md ⑤ 的 `pipeline-guard` 块拦截越界 Write/Edit,不依赖 prompt 自觉
- **打回硬上限**:范围评审 ≤2 轮,测试 ≤3 轮,到顶停报

## 使用

1. 安装(每台机器一次):`git clone` 本仓库后 `bash install.sh`(只做软链 agents/skills/hooks 到 `~/.claude/`,不动其他配置)。
2. 项目接入:`bash init-project.sh /path/to/项目`(拷契约模板 + 配 hook + gitignore,详见「接入新项目」一节),然后编辑项目根的 `PIPELINE.md` 逐项替换 ❏。契约随项目 git 管理。
3. 在项目的 Claude Code 会话里:`/pipeline <任务描述>`。
4. 中间产物在项目的 `tmp/pipeline/`(plan.md + plan-<批次>.md / qa-report.md / release-notes.md)。同一项目同一时刻只跑一条 `/pipeline`(artifact 是单例,并行会互踩)。
5. 流水线异常中断后若普通编辑被 hook 误拦,删除 `tmp/pipeline/.active` 即可。

## 角色工具箱(推荐)

通用层不硬编码任何工具——由项目契约 ③ 声明,`/pipeline` 启动时自检、缺失自动安装(全局 CLI 型)。求精不求多,每角色 1–2 个;`templates/PIPELINE.md` ③ 节有同份注释清单(含条件启用项)可直接启用:

| 面/角色 | 工具 | 安装方式 | 建议场景 |
| ------ | ---- | -------- | -------- |
| 守门员 | dependency-cruiser(模块边界/依赖方向机器校验) | 项目 devDep+规则文件 | 收口 |
| 拆解/开发 | ast-grep(结构搜索/codemod,定位准省 token) | 全局 `sg` | 日常 |
| 开发·设计辅助 | ui-ux-pro-max(设计 skill:风格/调色板/字体/UX 准则) | Claude skill;契约 ② 声明后 developer 可调,缺失时自检 clone(附 git 来源)或降级 | UI 任务,基线未覆盖的细节 |
| 测试·静态页/UI | Playwright(E2E/多断点截图) | 项目 devDep | 日常批次 gate |
| 测试·静态页/UI | @axe-core(a11y 审计) | 项目 devDep | 日常/收口 |
| 测试·静态页/UI | Lighthouse CI(LCP/CLS 硬指标) | 全局 `lhci` | 里程碑收口 |
| 测试·API | curl / vitest+fetch(冒烟) | 系统自带 | 日常 |
| 测试·API | zod/ajv(响应契约校验) | 项目 devDep | 日常 |
| 测试·API | Bruno(集合回归,文本存仓库) | 全局 `bru` | 日常/收口 |
| 测试·API | Schemathesis(OpenAPI 对抗 fuzz) | pip/brew | 收口,契合 tester 对抗式找茬 |
| 测试·性能 | k6(压测) | 系统包 | 里程碑级 |
| 发布 | gitleaks(提交前密钥泄露扫描) | 系统包 brew | 闸口 2 前,建议默认开 |

条件启用(只进模板注释,不进主表):knip(死代码扫描,项目体量大后)、size-limit(bundle 体积门禁,有体积验收条时)、npm audit(依赖漏洞,零安装)。

原则:日常批次 gate 只跑快反馈项(lint/typecheck/单测/E2E 冒烟),重工具(Lighthouse/fuzz/压测)声明为里程碑收口用,避免每次 run 都烧全量。

## 接入新项目

```bash
bash init-project.sh /path/to/项目     # 或在项目目录里直接 bash <本目录>/init-project.sh
```

一条命令完成:拷贝 `templates/PIPELINE.md` 到项目根 + 合并写入 `.claude/settings.json` 禁区 hook + 确认 `.gitignore` 含 `/tmp/`。幂等,重复执行不覆盖已有文件。之后编辑 PIPELINE.md 逐项替换 ❏,即可 `/pipeline <任务描述>`。
