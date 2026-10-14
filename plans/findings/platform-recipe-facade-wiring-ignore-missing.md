---
status: resolved
plan: inventory-stock/001
module: platform
found: 2026-10-13
---

# The new-module recipe omits the second `ignore_imports` line a facade crossing needs

## Found while

Reviewing inventory-stock/001 (the first real cross-module crossing).

## What

`docs/recipes/new-module.md:46-52` (§5) says the consumer adapter calls the other module facade,
"and `orders/module.py` wires it", then says to allow "exactly that import" (the adapter) in
`.importlinter`. But `module.py` must also import the facade type to fetch it from the service
registry (`services.get(PresentationDirectory)`), which the `modules-independent` contract
forbids unless a second `ignore_imports` line allows it. inventory-stock/001 needed both lines
(`apps/api/.importlinter`, last three lines), and `docs/architecture.md:50-54` now documents the
pair, but the recipe still says one line. It also does not say that the provider module must
come before the consumer in `MODULES` (`container.py`).

## Why it matters

The next module that follows the recipe (orders → catalog prices; catalog → inventory in
inventory-stock/002 and 005) hits a red `uv run just arch` and has to guess whether a second
ignore line is allowed, or registers in the wrong order and fails at startup.

## Suggested next step

A fast-lane docs fix to `docs/recipes/new-module.md` §5: list both `ignore_imports` lines and the
`MODULES` ordering requirement, mirroring `docs/architecture.md:50-54`.

## Resolution (2026-10-13, repair round 1 of inventory-stock/001)

`docs/recipes/new-module.md` §5 now lists both `ignore_imports` lines (the adapter and the
consumer's `module.py`) and the rule that the providing module comes before the consumer in
`MODULES`, mirroring `docs/architecture.md:50-54`.
