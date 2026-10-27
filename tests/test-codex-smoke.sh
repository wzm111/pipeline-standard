#!/usr/bin/env bash
# 验证 Codex 安装、角色规则定位和实体目录冲突保护。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEST_HOME_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_HOME_DIR"' EXIT

PIPELINE_INSTALL_HOME="$TEST_HOME_DIR" bash "$REPO_ROOT/install.sh" --codex >/dev/null
CODEX_SKILL_LINK="$TEST_HOME_DIR/.codex/skills/pipeline"

test -L "$CODEX_SKILL_LINK"
test "$(realpath "$CODEX_SKILL_LINK")" = "$(realpath "$REPO_ROOT/skills/pipeline")"
test -f "$(realpath "$CODEX_SKILL_LINK")/../../agents/pipeline-planner.md"
for ref in fast standard thorough qa-loop dependencies operations codex interaction models model-routing evidence; do
  test -f "$(realpath "$CODEX_SKILL_LINK")/references/$ref.md"
done
test -f "$CODEX_SKILL_LINK/defaults/models.json"
# 从安装软链接调用解析器，项目和个人均无配置时仍能找到skill默认文件。
python3 "$CODEX_SKILL_LINK/scripts/resolve-models.py" --runtime codex --project "$TEST_HOME_DIR" --personal "$TEST_HOME_DIR/no-personal.json" |
  python3 -c 'import json,sys; d=json.load(sys.stdin); assert len(d["roles"]) == 5; assert all(r["source"] == "skill_default" for r in d["roles"].values())'

rm "$CODEX_SKILL_LINK"
mkdir "$CODEX_SKILL_LINK"
if PIPELINE_INSTALL_HOME="$TEST_HOME_DIR" bash "$REPO_ROOT/install.sh" --codex >/dev/null 2>&1; then
  echo "expected install to reject an existing directory" >&2
  exit 1
fi
test ! -e "$CODEX_SKILL_LINK/pipeline"

printf '%s\n' 'Codex smoke test: OK'
