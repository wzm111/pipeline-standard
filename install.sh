#!/usr/bin/env bash
# pipeline-standard 安装/同步:把通用角色与 /pipeline skill 软链到 ~/.claude/
# 新电脑:clone 本目录后执行 bash install.sh 即可
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.claude/agents" "$HOME/.claude/skills" "$HOME/.claude/hooks"

for f in "$ROOT"/agents/*.md; do
  ln -sf "$f" "$HOME/.claude/agents/$(basename "$f")"
  echo "链接 agents/$(basename "$f")"
done

ln -sfn "$ROOT/skills/pipeline" "$HOME/.claude/skills/pipeline"
echo "链接 skills/pipeline"

chmod +x "$ROOT"/hooks/*.sh
for f in "$ROOT"/hooks/*.sh; do
  ln -sf "$f" "$HOME/.claude/hooks/$(basename "$f")"
  echo "链接 hooks/$(basename "$f")"
done

cat <<'NOTE'
完成。Claude Code 新会话生效,触发方式:/pipeline <任务描述>

禁区硬拦截(可选,推荐):在项目的 .claude/settings.json 加入:
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Write|Edit|MultiEdit|NotebookEdit",
        "hooks": [{ "type": "command", "command": "bash ~/.claude/hooks/pipeline-guard.sh" }] }
    ]
  }
}
并在项目 PIPELINE.md ⑤ 中维护 pipeline-guard 块(见 templates/PIPELINE.md)。
NOTE
