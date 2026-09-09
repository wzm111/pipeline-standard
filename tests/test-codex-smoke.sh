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

rm "$CODEX_SKILL_LINK"
mkdir "$CODEX_SKILL_LINK"
if PIPELINE_INSTALL_HOME="$TEST_HOME_DIR" bash "$REPO_ROOT/install.sh" --codex >/dev/null 2>&1; then
  echo "expected install to reject an existing directory" >&2
  exit 1
fi
test ! -e "$CODEX_SKILL_LINK/pipeline"

printf '%s\n' 'Codex smoke test: OK'
