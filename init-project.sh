#!/usr/bin/env bash
# init-project.sh — 把当前目录(或指定目录)接入 pipeline-standard
# 用法: bash init-project.sh [项目根目录]     (默认当前目录)
# 动作: ① 拷贝 PIPELINE.md 契约模板 ② 配置 .claude/settings.json 禁区 hook ③ 确认 .gitignore 含 /tmp/
# 幂等:已存在的文件不会被覆盖。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
TARGET="$(cd "${1:-.}" && pwd)"

echo "接入项目: $TARGET"

# ① 契约模板
if [ -f "$TARGET/PIPELINE.md" ]; then
  echo "跳过: PIPELINE.md 已存在(不覆盖)"
else
  cp "$ROOT/templates/PIPELINE.md" "$TARGET/PIPELINE.md"
  echo "已拷贝: PIPELINE.md(请逐项替换 ❏)"
fi

# ② PreToolUse hook(禁区硬拦截)
HOOK_CMD='bash ~/.claude/hooks/pipeline-guard.sh'
mkdir -p "$TARGET/.claude"
SETTINGS="$TARGET/.claude/settings.json"
python3 - "$SETTINGS" "$HOOK_CMD" <<'PYEOF'
import json, sys, os

path, cmd = sys.argv[1], sys.argv[2]
cfg = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)

hooks = cfg.setdefault("hooks", {})
pre = hooks.setdefault("PreToolUse", [])
entry = {"matcher": "Write|Edit|MultiEdit|NotebookEdit",
         "hooks": [{"type": "command", "command": cmd}]}

for group in pre:
    if any(h.get("command") == cmd for h in group.get("hooks", [])):
        print("跳过: hook 已配置")
        break
else:
    pre.append(entry)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"已写入 hook: {path}")
PYEOF

# ③ .gitignore 含 pipeline 相关临时产物
GITIGNORE="$TARGET/.gitignore"
NEEDLE='^# pipeline-standard guard'
if [ -f "$GITIGNORE" ] && grep -qE "$NEEDLE" "$GITIGNORE" ]; then
  echo "跳过: .gitignore 已含 pipeline-standard guard 块"
else
  cat >> "$GITIGNORE" <<EOF

# pipeline-standard guard
/tmp/
/.frontend-guardian/
/tmp-*/
*.log
# .claude/settings.json 通常需入库,不要整目录忽略;如产生临时文件请单独追加
EOF
  echo "已追加: .gitignore ← pipeline-standard guard 块"
fi

echo ""
echo "接入完成。下一步:编辑 $TARGET/PIPELINE.md 替换 ❏,然后在该项目的 Claude Code 会话里运行 /pipeline <任务描述>"
