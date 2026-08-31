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

merge_settings_json() {
  local path="$1" cmd="$2"

  # 优先用 python3, 其次用 jq, 都没有则降级为手动提示
  if command -v python3 >/dev/null 2>&1; then
    if python3 - "$path" "$cmd" <<'PYEOF'
import json, sys, os

path, cmd = sys.argv[1], sys.argv[2]
cfg = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)

hooks = cfg.setdefault("hooks", {})
pre = hooks.setdefault("PreToolUse", [])

entries = [
    {"matcher": "Write|Edit|MultiEdit|NotebookEdit",
     "hooks": [{"type": "command", "command": cmd}]},
    {"matcher": "Bash",
     "hooks": [{"type": "command", "command": cmd}]}
]

existing_matchers = {group.get("matcher") for group in pre}
changed = False
for entry in entries:
    if entry["matcher"] in existing_matchers:
        print(f"跳过: matcher {entry['matcher']} 已配置")
        continue
    pre.append(entry)
    existing_matchers.add(entry["matcher"])
    changed = True

if changed:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"已写入 hook: {path}")
PYEOF
    then
      return 0
    fi
  fi

  if command -v jq >/dev/null 2>&1; then
    local tmp="${path}.tmp"
    local jq_ok=0
    if [ -f "$path" ]; then
      jq --arg cmd "$cmd" '
        .hooks.PreToolUse //= [] |
        if (.hooks.PreToolUse | map(select(.matcher == "Write|Edit|MultiEdit|NotebookEdit")) | length) == 0 then
          .hooks.PreToolUse += [{"matcher":"Write|Edit|MultiEdit|NotebookEdit","hooks":[{"type":"command","command":$cmd}]}]
        else . end |
        if (.hooks.PreToolUse | map(select(.matcher == "Bash")) | length) == 0 then
          .hooks.PreToolUse += [{"matcher":"Bash","hooks":[{"type":"command","command":$cmd}]}]
        else . end
      ' "$path" > "$tmp" && jq_ok=1
    else
      echo '{}' | jq --arg cmd "$cmd" '
        .hooks.PreToolUse //= [] |
        if (.hooks.PreToolUse | map(select(.matcher == "Write|Edit|MultiEdit|NotebookEdit")) | length) == 0 then
          .hooks.PreToolUse += [{"matcher":"Write|Edit|MultiEdit|NotebookEdit","hooks":[{"type":"command","command":$cmd}]}]
        else . end |
        if (.hooks.PreToolUse | map(select(.matcher == "Bash")) | length) == 0 then
          .hooks.PreToolUse += [{"matcher":"Bash","hooks":[{"type":"command","command":$cmd}]}]
        else . end
      ' > "$tmp" && jq_ok=1
    fi
    if [ "$jq_ok" -eq 1 ]; then
      mv "$tmp" "$path"
      echo "已写入 hook: $path (via jq)"
      return 0
    fi
    rm -f "$tmp"
    return 1
  fi

  return 1
}

if ! merge_settings_json "$SETTINGS" "$HOOK_CMD"; then
  echo "跳过: 未找到 python3 或 jq, 无法自动合并 .claude/settings.json" >&2
  echo "请手动在项目根创建/编辑 $SETTINGS, 添加以下 PreToolUse 配置:" >&2
  cat <<EOF
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Write|Edit|MultiEdit|NotebookEdit",
        "hooks": [{ "type": "command", "command": "$HOOK_CMD" }] },
      { "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "$HOOK_CMD" }] }
    ]
  }
}
EOF
fi

# ③ .gitignore 含 pipeline 相关临时产物
GITIGNORE="$TARGET/.gitignore"
NEEDLE='^# pipeline-standard guard'
if [ -f "$GITIGNORE" ] && grep -qE "$NEEDLE" "$GITIGNORE"; then
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
