---
status: draft # draft → approved → implementing → done (or: abandoned)
module: <name from docs/modules.json>
depends_on: [] # e.g. [catalog-products/001]
---

# NNN — <Title>

<!-- Written after brainstorming with the user; there is no separate spec. English only.
     Every claim about existing code carries a file:line reference read while writing it. -->

## Context

<!-- What exists today (with references), what we need, the chosen approach and why (one
     short paragraph), and which existing files are imitated. -->

## Out of scope

<!-- Explicit fence: refactors, adjacent bugs, UI polish not requested, other modules… -->

## Dependencies

<!-- For each depends_on: the plan and the interface it promises. "None" if empty. -->

None

## Steps

<!-- Numbered. Each step: a `Files:` line with backticked paths + (create)/(modify)/(delete),
     what to do, and the observable result. Files not listed here are not touched. -->

1. **<Step title>**
   - Files: `path/to/file.py` (create)
   - Do: …
   - Observable result: …

## Acceptance criteria

<!-- Verifiable in the RUNNING system: commands, endpoints, status codes, JSON shapes, rows. -->

- [ ] …

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes/no  |       |
| application | yes/no  |       |
| http        | yes/no  |       |
| integration | yes/no  |       |
| e2e         | yes/no  |       |

## Deviations

<!-- Left empty when planning. During implementation: what the plan said / what reality was /
     what was done. Write "None" explicitly if none. -->

## Verification

<!-- Left empty when planning. At the end: commands run (decisive output lines), acceptance
     criteria checked one by one, unhappy paths, and anything NOT verified, flagged. -->
