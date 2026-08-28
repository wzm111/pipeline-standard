# pipeline-standard — 通用研发流水线标准

端到端「需求 → 开发 → 测试 → 上线」多角色流水线,方法论通用,项目差异由项目根的 `PIPELINE.md` 契约注入。

## 组成

```text
agents/                     5 个通用角色(软链到 ~/.claude/agents/)
  pipeline-scope-guardian   A 范围守门员(只读):审计划、验收,对照需求基线与规范
  pipeline-planner          B 任务拆解员:需求 → 带验收标准的任务清单
  pipeline-developer        C 开发工程师:只按计划做,遵守契约禁区
  pipeline-tester           D 测试工程师:实际跑测试命令,只报不改
  pipeline-releaser         E 发布工程师:变更摘要 + 提交建议,不动 git/不部署
skills/pipeline/SKILL.md    /pipeline 斜杠命令,固化编排流程与两道人工闸口
install.sh                  安装/同步脚本
```

## 使用

1. 每个项目根放一个 `PIPELINE.md`(契约,五节:需求基线 / 架构规范 / 测试命令 / 上线方式 / 禁区边界),随项目 git 管理。模板见 `templates/PIPELINE.md`,复制后逐项替换 ❏ 即可。
2. 在项目的 Claude Code 会话里:`/pipeline <任务描述>`。
3. 中间产物在项目的 `tmp/pipeline/`(plan.md / qa-report.md / release-notes.md)。同一项目同一时刻只跑一条 `/pipeline`(artifact 是单例,并行会互踩)。

## 移植到新电脑

```bash
# 方式一:git(推荐,先推到私有远端)
git clone <本目录的远端地址> && cd pipeline-standard && bash install.sh

# 方式二:直接拷贝整个目录后跑 install.sh
```

`install.sh` 只做软链,不动其他配置。API key、`settings.json` 等敏感/机器相关配置不属于本体系,无需迁移。

## 接入新项目

复制 `templates/PIPELINE.md` 到项目根,逐项替换 ❏,10 分钟完成接入。
