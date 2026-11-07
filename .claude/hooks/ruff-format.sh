#!/usr/bin/env bash
# PostToolUse hook (Edit|Write): auto-format the edited Python file and report lint
# errors ruff cannot fix, so style is enforced mechanically instead of by review.

file=$(jq -r '.tool_input.file_path // empty')
[[ "$file" == *.py ]] && [ -f "$file" ] || exit 0
# Only the project's own files (not scratch scripts elsewhere, not .venv).
[[ "$file" == "$CLAUDE_PROJECT_DIR"/* && "$file" != "$CLAUDE_PROJECT_DIR"/.venv/* ]] || exit 0

cd "$CLAUDE_PROJECT_DIR" || exit 0
ruff=.venv/bin/ruff
[ -x "$ruff" ] || ruff=$(command -v ruff) || exit 0

# F401 ignored here: an import added one edit before its first use must not be deleted
# mid-change. Unused imports are still caught by the full lint before commit.
out=$("$ruff" check --fix --extend-ignore F401 --quiet "$file" 2>&1); status=$?
"$ruff" format --quiet "$file"
if [ $status -ne 0 ]; then
  echo "ruff found issues it could not fix in $file:" >&2
  printf '%s\n' "$out" | head -20 >&2
  exit 2
fi
exit 0
