# Ledger

A subscription billing engine — plans, billing cycles, proration, invoicing, and
dunning.

Billing is one of the few domains where a small arithmetic mistake is
immediately, visibly expensive: an off-by-one in a billing period double-charges
a customer, a rounding error loses money on every invoice, and a naive datetime
renews a subscription an hour early twice a year. That makes it a good place to
find out whether an agent can write code that is *correct*, not merely code that
runs.

## How this was built

The application code in `ledger/` and its tests were written by **OpenAI Codex**,
one task at a time, via `tools/codex-build.sh`. Each commit records the prompt
Codex was given and what it reported back, and is authored by Codex in git.

Scaffolding — this README, the build harness, the virtualenv — was set up by
hand with Claude Code. The distinction is visible in the commit history: check
`git log --format='%an %s'`.

## Its other job

Ledger is also the repository that [Mender](https://github.com/Harhsitoo/mender)
watches. When a change breaks a test here, Mender hands the failure back to
Codex, verifies the fix independently, and opens a pull request. So Codex writes
the code, and Codex repairs it — with Mender deciding whether the repair is real.

## Running it

```bash
uv venv --python 3.12 && uv pip install pytest
.venv/bin/python -m pytest -q
```
