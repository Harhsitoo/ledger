#!/usr/bin/env bash
# Introduce a known regression, for demonstrating Mender.
#
# It replaces divmod with integer division in allocate_equal. Every positive
# case still passes; only negative totals — credits — break, because int()
# truncates toward zero instead of flooring. That is the shape of bug that
# actually reaches production.
#
# Undo with:  git reset --hard origin/main
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

python3 - <<'PY'
from pathlib import Path

path = Path("ledger/money.py")
source = path.read_text()
before = "    share, remainder = divmod(total.amount, len(recipients))"
after = (
    "    share = int(total.amount / len(recipients))\n"
    "    remainder = total.amount - share * len(recipients)"
)

if before not in source:
    raise SystemExit("already broken, or money.py has changed — run: git reset --hard origin/main")

path.write_text(source.replace(before, after, 1))
print("broke allocate_equal in ledger/money.py")
PY

git add ledger/money.py
git -c user.name="A Developer" -c user.email="dev@example.com" \
    commit --quiet --message "Simplify the allocation share calculation"

echo "committed $(git rev-parse --short HEAD)"
