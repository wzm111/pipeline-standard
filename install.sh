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

if ! command -v python3 >/dev/null 2>&1; then
  echo "安装未完成:需要 python3 初始化并校验本机模型配置。请让智能体协助处理。" >&2
  exit 1
fi
python3 "$ROOT/skills/pipeline/scripts/initialize-models.py" --target "$INSTALL_HOME/.config/pipeline-standard/models.json"

if [ "$TARGET" = "--all" ] || [ "$TARGET" = "--claude" ]; then install_claude; fi
if [ "$TARGET" = "--all" ] || [ "$TARGET" = "--codex" ]; then install_codex; fi

cat <<'NOTE'
完成。可在新会话或已有会话中调用 skill：
- Claude Code：/pipeline <任务描述>
- Codex：$pipeline <任务描述>

在项目会话直接描述需求，首次会自动接入，无需手填配置。
已准备本机默认角色配置；项目配置优先，升级保留你的选择。
新安装先继承当前会话模型，说“配置模型”即可选择不同角色型号。
也可以说：帮我初始化、配置模型、查看设置、继续上次。
Claude Code 用 /pipeline，Codex 用 $pipeline。
已有会话请让智能体重新读取本机最新版 SKILL.md 及本次所需引用，沿用当前任务和有效证据。
若宿主仍未发现更新，再重启应用或新开会话。
Claude hook 与 Codex 写前校验的能力差异见 README。
NOTE
