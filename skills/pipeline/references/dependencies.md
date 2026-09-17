# 依赖与 skill 自检

仅在当前测试层确实引用缺失 CLI，或当前批次确实需要契约声明但缺失的 skill 时读取。

## CLI

- 逐个 `command -v` 检测当前层实际使用的 CLI；未使用的工具不检测。
- 项目 devDependency 型工具不全局安装，缺失时跳过对应检查并在 qa/final.md 说明。
- Claude Code 可按 [工具速查表](../../../tools/quick-install.md) 在契约范围内安装已知全局 CLI；未知 CLI 不猜包名，转人工。
- Codex 展示工具、命令、目标和影响，获得明确授权后才安装。未授权或失败只跳过该项，不阻断其他测试。

## Skill

- Claude Code 检查用户/项目 Claude skill 目录；Codex 检查用户/项目 Codex skill 目录。
- 缺失且契约提供 git 来源时，Codex clone 前先请求授权；Claude 按契约规则处理。
- 安装前扫描 SKILL.md 和脚本中的管道执行、编码载荷、非声明外联、越界读写及敏感目录访问。命中即拒装并转人工。
- 根有 SKILL.md 时安装整体；否则只接受仓库内对应 `.claude/skills/<name>` 或 `.codex/skills/<name>`。无匹配则降级按基线执行。
