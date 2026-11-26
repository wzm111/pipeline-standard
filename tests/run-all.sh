#!/usr/bin/env bash
# 本地/CI隔离回归；不调用模型或改写用户配置。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# unittest discover 忽略含连字符的模块名；显式执行避免“零测试”假通过。
for test_file in tests/test-*.py; do
  PYTHONDONTWRITEBYTECODE=1 python3 "$test_file"
done
bash tests/test-codex-smoke.sh
for script in install.sh init-project.sh hooks/pipeline-guard.sh hooks/pipeline-progress-session.sh; do
  bash -n "$script"
done
git diff --check
