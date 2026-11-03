# 安装与分发状态 / Installation and distribution status

[中文首页](../README.md) · [English overview](README.en.md) · [试用示例](TRY-PIPELINE.md)

## 已提供的入口 / Available distribution

维护源码与版本的主入口是 [GitHub 仓库](https://github.com/wzm111/pipeline-standard)及其 [Releases](https://github.com/wzm111/pipeline-standard/releases)。完整安装使用仓库根的 `bash install.sh`，可选 `--codex` 或 `--claude`。仓库测试使用独立安装目录，验证链接和默认配置；实际模型执行能力、主机权限仍需各自环境确认。

保留完整检出结构：

```text
pipeline-standard/
├── install.sh
├── init-project.sh
├── skills/pipeline/
├── agents/
├── hooks/
└── templates/
```

Codex 的路径解析和角色规则会引用 skill 目录外的资源；Claude 安装还会链接根目录角色和 hooks。只拷贝 `SKILL.md` 或 `skills/pipeline/` 会遗漏这些依赖，不能以“已被目录发现”代替安装验收。

## 其他入口的边界 / Other channels

| 入口 | 当前可说明的状态 |
| --- | --- |
| GitHub 完整仓库安装 | 提供安装器、版本和本地隔离测试 |
| skills.sh / SkillsMP | 可作为公开源码的发现渠道；本仓库未声明其目录安装器已完整安装所有外部资源 |
| Claude 插件目录或自有 marketplace | 需要插件打包、路径/命令入口适配和干净安装验收；当前仓库不提供已验证的 marketplace 清单 |
| OpenAI 插件目录 | 需要相应插件包、桌面执行声明及平台审核；当前 GitHub Release 不等于目录上架 |

本说明不发起投稿、创建第三方账号或更改用户安装设置。后续适配应复用同一套源码，单独验证组件完整性、调用入口、重装/更新行为和平台实际支持范围；通过前继续推荐完整仓库安装，不发布未经验证的一键安装命令。

## 可复用的项目介绍 / Suggested project description

中文：

> 支持 Claude Code 与 Codex 的对话式研发流水线：控制需求范围、显示执行进度、复用仍有效的检查证据，并在本地预览运行记录。

English:

> A conversational development workflow for Claude Code and Codex, with bounded scope, visible progress, evidence-aware checks, and local run-report previews.

配合[独立试用示例](TRY-PIPELINE.md)介绍实际行为。测试数量是实现验证信息；展示进度不等于所有宿主都能实时推送；目录收录、安装量不等于任务成功率。不承诺固定速度或 token 收益。

## 数据边界 / Data boundaries

本项目的诊断模块仅本地记录与预览，不提供自动报告上传。聊天宿主、GitHub 和第三方安装工具有各自的数据政策，不能把本地诊断的边界扩展为整个安装链或宿主都离线。不要为分发而打包整个 `tmp/pipeline/`、私有归属文件或原始项目日志。

The supported installation path is the complete repository. The skill depends on root-level agents, hooks, and templates; discovering a `SKILL.md` is not a full installation test. Third-party catalog installation and official plugin listings are not claimed as validated distribution paths. Diagnostics remain local; hosts and external installers have their own data policies. Feedback is voluntary and must not include private run directories or raw project logs.
