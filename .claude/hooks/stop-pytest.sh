#!/usr/bin/env bash
# Stop hook: Claude may not end its turn while the test suite is red.
# Exit 2 blocks the stop and feeds stderr back to Claude. One forced retry only,
# so a stuck agent hands control back to the human instead of looping.

input=$(cat)
cd "$CLAUDE_PROJECT_DIR" || exit 0
[ -d tests ] && [ -x .venv/bin/python ] || exit 0

out=$(.venv/bin/python -m pytest -q -x -m 'not integration' 2>&1); status=$?
# 0 = green; 5 = no tests collected yet (a fresh repo must not block the first turns).
if [ $status -eq 0 ] || [ $status -eq 5 ]; then
  exit 0
fi

if [ "$(printf '%s' "$input" | jq -r '.stop_hook_active // false')" = "true" ]; then
  echo "stop-pytest: tests still failing after one fix attempt; handing back to the user." >&2
  exit 0
fi

echo "Tests are failing. Fix them (or explain why) before finishing:" >&2
printf '%s\n' "$out" | tail -40 >&2
exit 2
