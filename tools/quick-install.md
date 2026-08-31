# pipeline-standard 工具速查表

本文件是 `/pipeline` 工具自检时可能自动安装的全局 CLI 单一来源。新增或修改 CLI 时只需改这里，README.md 与 `skills/pipeline/SKILL.md` 均引用本文件。

## 已知全局 CLI

| CLI | 安装命令 | 适用角色/场景 |
| --- | -------- | ------------- |
| `fg-core` | `npm install -g frontend-guardian-core` | 守门员·模块/依赖边界校验 |
| `lhci` | `npm install -g @lhci/cli` | 测试·里程碑收口性能指标 |
| `bru` | `npm install -g @usebruno/cli` | 测试·API 集合回归 |
| `schemathesis` | `pip install schemathesis`（或 `brew install schemathesis`） | 测试·API OpenAPI 对抗 fuzz |
| `k6` | `brew install k6`（或对应系统包） | 测试·里程碑级压测 |
| `sg` | `npm install -g @ast-grep/cli` | 拆解/开发·结构搜索与 codemod |
| `depcruise` | `npm install -g dependency-cruiser` | 守门员·模块依赖方向机器校验 |
| `gitleaks` | `brew install gitleaks` | 发布·闸口 2 前密钥泄露扫描 |

## 项目 devDep 型工具

以下工具不由 `/pipeline` 全局安装，需在项目内自行 `npm i -D` 并接入 npm scripts：

| 工具 | 安装命令 | 适用角色/场景 |
| --- | -------- | ------------- |
| `playwright` | `npm i -D playwright` | 测试·静态页/UI E2E/多断点截图 |
| `@axe-core/playwright` | `npm i -D @axe-core/playwright` | 测试·可访问性审计 |
| `knip` | `npm i -D knip` | 测试·死代码扫描（项目体量大后条件启用） |
| `size-limit` | `npm i -D size-limit` | 测试·bundle 体积门禁（有体积验收条时启用） |

## 设计类 skill

| skill | 安装方式 | 适用角色/场景 |
| --- | -------- | ------------- |
| `ui-ux-pro-max` | Claude skill；契约 ② 声明后 developer 可调，缺失时自检 clone（附 git 来源）或降级 | 开发·设计辅助，UI 任务基线未覆盖的细节 |

## 使用原则

- 日常批次 gate 只跑快反馈项（lint / typecheck / 单测 / E2E 冒烟）。
- 重工具（Lighthouse / fuzz / 压测）声明为里程碑收口用，避免每次 run 都烧全量。
- 速查表没有的 CLI → `/pipeline` 尝试 `npm install -g <同名包>`，失败按降级处理。
