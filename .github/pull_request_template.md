<!-- Title: commit convention, e.g. `feat(catalog): admin can create and list brands`.
     Each section, with good and bad examples: docs/harness/conventions/pull-requests.md.
     Facts and real command output only. No AI attribution. -->

## What changed

<!-- The outcome in 2–3 lines, readable without opening the code. -->

## Why

<!-- The problem or need. Plan: plans/<initiative>/NNN-<slug>.md (or "fast lane: <reason>")
     and the README decisions it applies. -->

## How

<!-- Approach, key design decisions, alternatives discarded, files worth reviewing first. -->

## How it was verified

<!-- Commands with their real results, live checks, evidence (or a link to the plan's
     ## Verification). Only what was actually run. -->

## What's missing

<!-- NOT VERIFIED items, known limitations, follow-ups, open findings. "Nothing" only if true. -->

## Risks and rollout

<!-- Migrations, configuration/env changes, manual steps after merge — or "None". -->

---

- [ ] `uv run just check` passes
- [ ] `uv run just test-integration` passes (when persistence changed)
- [ ] `uv run just plans-scope <plan>` passes (when there is a plan)
- [ ] `apps/api/openapi.json` regenerated with `uv run just openapi` (when a contract changed)
- [ ] Docs updated, in English
