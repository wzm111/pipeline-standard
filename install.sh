#!/usr/bin/env bash
# pipeline-standard 安装/同步:把通用角色与 /pipeline skill 软链到 ~/.claude/
# 新电脑:clone 本目录后执行 bash install.sh 即可
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.claude/agents" "$HOME/.claude/skills"

for f in "$ROOT"/agents/*.md; do
  ln -sf "$f" "$HOME/.claude/agents/$(basename "$f")"
  echo "链接 agents/$(basename "$f")"
done

ln -sfn "$ROOT/skills/pipeline" "$HOME/.claude/skills/pipeline"
echo "链接 skills/pipeline"
echo "完成。Claude Code 新会话生效,触发方式:/pipeline <任务描述>"
