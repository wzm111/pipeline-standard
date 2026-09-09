#!/usr/bin/env bash
# pipeline-standard 安装/同步:同时支持 Claude Code 与 Codex。
# 新电脑:clone 本目录后执行 bash install.sh 即可；可用 --claude 或 --codex 限定目标。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:---all}"
INSTALL_HOME="${PIPELINE_INSTALL_HOME:-$HOME}"

case "$TARGET" in
  --all|--claude|--codex) ;;
  *)
    echo "用法: bash install.sh [--all|--claude|--codex]" >&2
    exit 2
    ;;
esac

install_claude() {
  mkdir -p "$INSTALL_HOME/.claude/agents" "$INSTALL_HOME/.claude/skills" "$INSTALL_HOME/.claude/hooks"

  for f in "$ROOT"/agents/*.md; do
    ln -sf "$f" "$INSTALL_HOME/.claude/agents/$(basename "$f")"
    echo "Claude Code: 链接 agents/$(basename "$f")"
  done

  link_skill "$ROOT/skills/pipeline" "$INSTALL_HOME/.claude/skills/pipeline"
  echo "Claude Code: 链接 skills/pipeline"

  chmod +x "$ROOT"/hooks/*.sh
  for f in "$ROOT"/hooks/*.sh; do
    ln -sf "$f" "$INSTALL_HOME/.claude/hooks/$(basename "$f")"
    echo "Claude Code: 链接 hooks/$(basename "$f")"
  done
}

link_skill() {
  local source_skill="$1" destination_skill="$2"

  if [ -e "$destination_skill" ] && [ ! -L "$destination_skill" ]; then
    echo "拒绝安装: $destination_skill 是实体目录或文件。请先迁移、删除或改名该现有 skill，再重试。" >&2
    return 1
  fi
  ln -sfn "$source_skill" "$destination_skill"
}

install_codex() {
  mkdir -p "$INSTALL_HOME/.codex/skills"
  link_skill "$ROOT/skills/pipeline" "$INSTALL_HOME/.codex/skills/pipeline"
  echo "Codex: 链接 skills/pipeline"
}

if [ "$TARGET" = "--all" ] || [ "$TARGET" = "--claude" ]; then install_claude; fi
if [ "$TARGET" = "--all" ] || [ "$TARGET" = "--codex" ]; then install_codex; fi

cat <<'NOTE'
完成。请新开会话以加载 skill：
- Claude Code：/pipeline <任务描述>
- Codex：$pipeline <任务描述>

接入新项目(拷契约模板 + 配 hook + gitignore):
  bash init-project.sh /path/to/项目

说明：下面的 PreToolUse 配置仅适用于 Claude Code。Codex 通过 skill 内的
PIPELINE.md 预检和子 agent 编排执行同一流程，但没有该 hook 级硬拦截接口。

禁区硬拦截的手动配置方式(一般不需要,init-project.sh 已代办):
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Write|Edit|MultiEdit|NotebookEdit",
        "hooks": [{ "type": "command", "command": "bash ~/.claude/hooks/pipeline-guard.sh" }] },
      { "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "bash ~/.claude/hooks/pipeline-guard.sh" }] }
    ]
  }
}
并在项目 PIPELINE.md ⑤ 中维护 pipeline-guard 块(见 templates/PIPELINE.md)。
NOTE
