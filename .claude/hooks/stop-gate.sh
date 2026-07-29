#!/bin/sh
# stop-gate.sh — per-repo Stop hook: when the turn ends with modified Python
# source files, run ruff check + pytest on them. Exit 2 blocks the stop and
# feeds the failures back so the agent fixes them before finishing.
# Loop-safe: exits 0 when stop_hook_active is set (we already ran this turn).

INPUT=$(cat)
case "$INPUT" in
  *'"stop_hook_active": true'*|*'"stop_hook_active":true'*) exit 0 ;;
esac

# Only inside a git repo, only when Python source changed (skip migrations -
# generated code, not hand-authored).
git rev-parse --git-dir >/dev/null 2>&1 || exit 0
CHANGED=$(git diff --name-only HEAD 2>/dev/null | grep -E '\.py$' | grep -v -E '(^|/)migrations/' || true)
[ -z "$CHANGED" ] && exit 0

RUFF=.venv/bin/ruff
PYTEST=.venv/bin/pytest
[ -x "$RUFF" ] || RUFF=ruff
[ -x "$PYTEST" ] || PYTEST=pytest

FAIL=0
OUT=""

# shellcheck disable=SC2086
RUFF_OUT=$($RUFF check $CHANGED 2>&1) || { FAIL=1; OUT="$OUT
--- ruff check ---
$(echo "$RUFF_OUT" | head -20)"; }

TEST_OUT=$($PYTEST -q 2>&1) || { FAIL=1; OUT="$OUT
--- pytest ---
$(echo "$TEST_OUT" | tail -25)"; }

if [ "$FAIL" -eq 1 ]; then
  echo "stop-gate: quality gate failed for changed files - fix before finishing:$OUT" >&2
  exit 2
fi
exit 0
