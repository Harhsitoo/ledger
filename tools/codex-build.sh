#!/usr/bin/env bash
# Drive Codex to do a unit of work, then commit the result with Codex as the
# git author.
#
# Author and committer are different people in git for exactly this reason:
# the author wrote the change, the committer applied it. Codex writes the code
# here, so Codex is the author. The commit message records the prompt it was
# given, so the history shows what was asked as well as what came back.
#
#   tools/codex-build.sh "Implement money.py with allocation" "Add money module"
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROMPT="${1:?usage: codex-build.sh <prompt> <commit-subject> [effort]}"
SUBJECT="${2:?usage: codex-build.sh <prompt> <commit-subject> [effort]}"
EFFORT="${3:-medium}"

PYTHON="$REPO/.venv/bin/python"
SUMMARY="$(mktemp)"
trap 'rm -f "$SUMMARY"' EXIT

echo "── codex: $SUBJECT ($EFFORT effort)"

# --ignore-user-config keeps runs reproducible: a personal ~/.codex/config.toml
# can enable plugins and high reasoning effort that make timings meaningless.
codex exec \
  -C "$REPO" \
  -s workspace-write \
  --ignore-user-config \
  -c approval_policy=never \
  -c "model_reasoning_effort=$EFFORT" \
  -o "$SUMMARY" \
  "$PROMPT

If this task involves Python code, write real tests for it and run the suite
with \`$PYTHON -m pytest -q\` before you finish — it must pass. If the task is
design or documentation only, there is nothing to run." >/dev/null

if [[ -z "$(git -C "$REPO" status --porcelain)" ]]; then
  echo "   codex made no changes — nothing to commit" >&2
  exit 1
fi

git -C "$REPO" add -A
git -C "$REPO" commit --quiet \
  --author="Codex <codex@openai.com>" \
  --message "$SUBJECT" \
  --message "Task given to Codex:

$PROMPT" \
  --message "Codex reported:

$(cat "$SUMMARY")"

echo "   $(git -C "$REPO" log -1 --pretty='%h %an: %s')"
