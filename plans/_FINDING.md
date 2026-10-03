---
status: open # open → deferred | planned | resolved | discarded (the user decides)
module: <name from docs/modules.json>
found: YYYY-MM-DD
# plan: <initiative>/NNN   # required when status is planned or resolved
---

# <What is wrong or missing, in one line>

<!-- File: plans/findings/<module>-<short-slug>.md (kebab-case). Rules:
     docs/harness/conventions/plans.md → "Findings". Validated by `uv run just plans-lint`. -->

## Found while

<!-- The plan or task, e.g. "reviewing platform-api-foundation/002". -->

## What

<!-- The problem with file:line references. Facts only. -->

## Why it matters

<!-- The failure scenario or the cost of leaving it. -->

## Suggested next step

<!-- A plan, a fast-lane fix, or "accept and close" — the user decides. -->
