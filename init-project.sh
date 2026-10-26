#!/usr/bin/env bash
# init-project.sh — 把当前目录(或指定目录)接入 pipeline-standard
# 用法: bash init-project.sh [--all|--claude|--codex] [项目根目录]     (默认当前目录)
# 动作: ① 拷贝 PIPELINE.md 契约模板 ② 配置 Claude Code 的 .claude/settings.json 禁区 hook ③ 确认 .gitignore 含 /tmp/
# Codex 读取同一份 PIPELINE.md；其禁区校验由 pipeline skill 的流程执行，不写 Claude 配置。
# 幂等:已存在的文件不会被覆盖。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
MODE="--all"
if [[ "${1:-}" == --* ]]; then
  MODE="$1"
  shift
fi
case "$MODE" in
  --all|--claude|--codex) ;;
  *)
    echo "用法: bash init-project.sh [--all|--claude|--codex] [项目根目录]" >&2
    exit 2
    ;;
esac
if [ "$#" -gt 1 ]; then
  echo "用法: bash init-project.sh [--all|--claude|--codex] [项目根目录]" >&2
  exit 2
fi
TARGET="$(cd "${1:-.}" && pwd)"

# 不跟随项目控制文件的符号链接写入其他目录。
CONTROL_PATHS=("$TARGET/.gitignore" "$TARGET/PIPELINE.md")
if [ "$MODE" != "--codex" ]; then
  CONTROL_PATHS+=("$TARGET/.claude" "$TARGET/.claude/settings.json")
fi
for control in "${CONTROL_PATHS[@]}"; do
  if [ -L "$control" ]; then
    echo "接入未完成:控制路径是符号链接，请由智能体核对目标后处理: $control" >&2
    exit 1
  fi
done

echo "接入项目: $TARGET"

# ① 契约模板
if [ -f "$TARGET/PIPELINE.md" ]; then
  echo "跳过: PIPELINE.md 已存在(不覆盖)"
else
  cp "$ROOT/templates/PIPELINE.md" "$TARGET/PIPELINE.md"
  echo "已准备: PIPELINE.md 模板；下一次 pipeline 对话会自动识别项目并补全。"
fi

# ② Claude Code 的 PreToolUse hook(禁区硬拦截)
if [ "$MODE" = "--all" ] || [ "$MODE" = "--claude" ]; then
HOOK_CMD='bash ~/.claude/hooks/pipeline-guard.sh'
mkdir -p "$TARGET/.claude"
SETTINGS="$TARGET/.claude/settings.json"

merge_settings_json() {
  local path="$1" cmd="$2"

  # 优先用 python3, 其次用 jq, 都没有则降级为手动提示
  if command -v python3 >/dev/null 2>&1; then
    if python3 - "$path" "$cmd" <<'PYEOF'
import json, sys, os, tempfile

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

changed = False
for entry in entries:
    if any(group.get("matcher") == entry["matcher"] and
           any(h.get("type") == "command" and h.get("command") == cmd for h in group.get("hooks", []))
           for group in pre):
        print(f"跳过: matcher {entry['matcher']} 已含 pipeline hook")
        continue
    pre.append(entry)
    changed = True

if changed:
    staged = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(path), prefix=".pipeline-settings-", delete=False) as f:
            staged = f.name
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            os.chmod(staged, os.stat(path).st_mode & 0o777)
        os.replace(staged, path)
        staged = None
    finally:
        if staged is not None:
            os.unlink(staged)
    print(f"已写入 hook: {path}")
PYEOF
    then
      return 0
    fi
    return 1
  fi

  if command -v jq >/dev/null 2>&1; then
    local tmp
    tmp="$(mktemp "${path}.pipeline.XXXXXX")" || return 1
    local jq_ok=0
    if [ -f "$path" ]; then
      jq --arg cmd "$cmd" '
        .hooks.PreToolUse //= [] |
        if (.hooks.PreToolUse | map(select(.matcher == "Write|Edit|MultiEdit|NotebookEdit" and any(.hooks[]?; .type == "command" and .command == $cmd))) | length) == 0 then
          .hooks.PreToolUse += [{"matcher":"Write|Edit|MultiEdit|NotebookEdit","hooks":[{"type":"command","command":$cmd}]}]
        else . end |
        if (.hooks.PreToolUse | map(select(.matcher == "Bash" and any(.hooks[]?; .type == "command" and .command == $cmd))) | length) == 0 then
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
  echo "接入未完成:无法合并 hook（配置无效、写入失败或缺少 python3/jq）。原设置保留，请让智能体根据上述错误修复后继续。" >&2
  exit 1
fi
else
  echo "Codex 模式:跳过 .claude/settings.json hook 配置；禁区校验由 pipeline skill 执行。"
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
echo "接入文件已准备。对智能体说“帮我初始化”或直接描述需求，它会检查并补全项目设置。"
echo "Claude Code 使用 /pipeline <任务描述>；Codex 安装 skill 后使用 \$pipeline <任务描述>。"
