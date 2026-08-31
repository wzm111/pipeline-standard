#!/usr/bin/env bash
# pipeline-guard — /pipeline 运行期间的禁区写拦截 hook(PreToolUse: Write|Edit|NotebookEdit)
#
# 工作机制:
#   1. 项目存在 tmp/pipeline/.active 标记(/pipeline 流程开始时创建、结束时删除)才启用;
#      无标记 = 普通会话,直接放行。
#   2. 启用时读项目根 PIPELINE.md 里的机器可读块:
#        <!-- pipeline-guard:allow-write          ← 白名单模式:只允许这些前缀
#        src/
#        tmp/
#        -->
#      或
#        <!-- pipeline-guard:deny-write           ← 黑名单模式:拦截这些前缀
#        config/
#        -->
#   3. 命中拦截:exit 2,stderr 说明原因(agent 能看到并自我纠正)。
#
# 依赖:python3(macOS 自带);无 python3 时退化为放行并告警。
set -u

if ! command -v python3 >/dev/null 2>&1; then
  echo "[pipeline-guard] 警告:未找到 python3,禁区拦截失效,本次放行" >&2
  exit 0
fi

INPUT=$(cat)

# --- 提取 cwd、tool_name 与目标路径(路径可能含空格,分开提取) ---
CWD=$(printf '%s' "$INPUT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("cwd",""))' 2>/dev/null)
TOOL_NAME=$(printf '%s' "$INPUT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("tool_name",""))' 2>/dev/null)
FP=$(printf '%s' "$INPUT" | python3 -c 'import sys,json; d=json.load(sys.stdin); t=d.get("tool_input") or {}; print(t.get("file_path") or t.get("notebook_path") or "")' 2>/dev/null)

if [ -z "${CWD:-}" ]; then CWD="$(pwd)"; fi

# --- 无激活标记 = 非流水线会话,放行 ---
MARKER="$CWD/tmp/pipeline/.active"
if [ ! -f "$MARKER" ]; then exit 0; fi

# --- Bash 命令:流水线期间禁止写/删/改操作 ---
if [ "$TOOL_NAME" = "Bash" ]; then
  CMD=$(printf '%s' "$INPUT" | python3 -c 'import sys,json; t=json.load(sys.stdin).get("tool_input") or {}; print(t.get("command") or "")' 2>/dev/null)
  if [ -n "$CMD" ]; then
    BLOCK_REASON=$(printf '%s' "$CMD" | python3 -c '
import sys, re, shlex
cmd = sys.stdin.read().strip()

# 1. git 写子命令
git_write = re.search(r"\bgit(?:\s+-[a-zA-Z]+(?:\s+\S+)?)*\s+(add|commit|push|tag|reset|merge|checkout\s+-b|restore|revert|cherry-pick|rebase|pull|stash|rm|mv|branch)\b", cmd, re.I)
if git_write:
    print("git-" + git_write.group(1).lower())
    sys.exit(0)

# 2. 常见文件破坏命令(允许在 tmp/ 内操作)
dangerous = re.search(r"\b(rm|cp|mv|touch)\s+(-\S+\s+)?(\S+)", cmd, re.I)
if dangerous:
    target = dangerous.group(3) or ""
    safe_prefixes = ("tmp/", "/tmp/", "/dev/null")
    if not any(target.startswith(p) for p in safe_prefixes):
        print("cmd-" + dangerous.group(1).lower())
        sys.exit(0)

# 3. 重定向写(> / >> / 1> 等),放行 tmp/ 与 /dev 安全目标
try:
    tokens = shlex.split(cmd)
except ValueError:
    tokens = cmd.split()
for i, token in enumerate(tokens):
    if token in (">", ">>", "1>", "1>>", "2>", "2>>"):
        target = tokens[i+1] if i+1 < len(tokens) else ""
        safe_prefixes = ("tmp/", "/tmp/", "/dev/null", "/dev/stdout", "/dev/stderr")
        if not any(target.startswith(p) for p in safe_prefixes):
            print("redirect-" + token)
            sys.exit(0)

sys.exit(1)
' 2>/dev/null)
    if [ -n "$BLOCK_REASON" ]; then
      echo "[pipeline-guard] 拦截:流水线运行期间禁止通过 Bash 执行写/删操作($BLOCK_REASON)。请在闸口 2 由人类手工执行,或把建议命令写进 release-notes.md。" >&2
      exit 2
    fi
  fi
  # Bash 只读/安全命令:放行
  exit 0
fi

if [ -z "${FP:-}" ]; then exit 0; fi

# --- 项目外路径:流水线期间一律拦截 ---
case "$FP" in
  "$CWD"/*) REL="${FP#$CWD/}" ;;
  /*)
    echo "[pipeline-guard] 拦截:流水线运行期间禁止写项目外路径 $FP" >&2
    exit 2 ;;
  *) REL="$FP" ;;
esac

PIPELINE_FILE="$CWD/PIPELINE.md"
if [ ! -f "$PIPELINE_FILE" ]; then exit 0; fi

# --- 解析 guard 块 ---
GUARD=$(python3 - "$PIPELINE_FILE" <<'PYEOF' 2>/dev/null
import sys, re
text = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"<!--\s*pipeline-guard:(allow-write|deny-write)\s*\n(.*?)-->", text, re.S)
if not m:
    sys.exit(0)
mode = m.group(1)
patterns = [l.strip() for l in m.group(2).splitlines() if l.strip() and not l.strip().startswith("#")]
print(mode)
for p in patterns:
    print(p)
PYEOF
)

if [ -z "$GUARD" ]; then exit 0; fi

MODE=$(printf '%s\n' "$GUARD" | head -1)
PATTERNS=$(printf '%s\n' "$GUARD" | tail -n +2)

match() {
  local rel="$1"
  while IFS= read -r p; do
    [ -z "$p" ] && continue
    # 规范化目录边界:普通前缀不以 / 结尾时自动补 /,避免 src 误匹配 src2/
    if [[ "$p" != */ && "$p" != *\\* ]]; then
      p="${p}/"
    fi
    case "$rel" in "$p"*) return 0 ;; esac
  done <<< "$PATTERNS"
  return 1
}

if [ "$MODE" = "allow-write" ]; then
  if match "$REL"; then exit 0; fi
  echo "[pipeline-guard] 拦截:流水线期间只允许写白名单目录,目标 $REL 不在其中。" >&2
  echo "[pipeline-guard] 白名单见 PIPELINE.md ⑤ 的 pipeline-guard:allow-write 块;确需写其他位置,请先向调度员/用户申请。" >&2
  exit 2
else
  if match "$REL"; then
    echo "[pipeline-guard] 拦截:$REL 命中 PIPELINE.md ⑤ 声明的禁区。" >&2
    exit 2
  fi
  exit 0
fi
