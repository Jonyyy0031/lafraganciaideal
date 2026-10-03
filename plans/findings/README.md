# Findings

Problems or ideas discovered while working on something else. They are **written down here,
not fixed in passing**: fixing them belongs to its own plan (or a fast-lane fix).

One file per finding, `plans/findings/<module>-<short-slug>.md`, copied from
[`plans/_FINDING.md`](../_FINDING.md). Its `status` (`open`, `deferred`, `planned`,
`resolved`, `discarded`) is decided by the user; `planned`/`resolved` name the plan that handles
it. `uv run just plans-lint` validates them and `uv run just plans-status` lists the open ones.

Rules: [docs/harness/conventions/plans.md](../../docs/harness/conventions/plans.md) → "Findings".
