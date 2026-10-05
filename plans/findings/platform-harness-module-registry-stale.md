---
status: open # open → deferred | planned | resolved | discarded (the user decides)
module: platform
found: 2026-10-03
---

# HARNESS.md's module registry summary still lists identity as planned

## Found while

Implementing identity-access/001 (step 1 sets `identity` to `active` in `docs/modules.json`).

## What

`docs/harness/HARNESS.md` → "Module registry" has a hand-written summary: "**Active**:
`platform`, `web`, `catalog`" and "**Planned**: `inventory`, `orders`, `payments`, `identity`,
…". `docs/modules.json` is the source of truth, and it now says `identity` is `active`. Plan 001
lists only HARNESS.md line 106 among its files, so the summary was left alone.

## Why it matters

An agent reading HARNESS.md first may think `identity` does not exist yet and refuse to
depend on it, or plan work that duplicates it. The summary goes stale every time a module
becomes active.

## Suggested next step

A docs-only fast-lane change: either add `identity` to Active, or drop the per-status lists and
point to `docs/modules.json` (so the summary cannot drift again).
