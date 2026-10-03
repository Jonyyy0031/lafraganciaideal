---
status: draft # draft → approved → implementing → testing → review → verify → done (blocked, superseded)
module: <name from docs/modules.json; must own the initiative>
min_implementer: mid # small | mid | high — see docs/harness/workflow.md → Model tiers
depends_on: [] # e.g. ["002"] (same initiative) or ["catalog-perfumes/001"]
---

# NNN — <Title>

<!-- Written by the architect role (docs/harness/roles/architect.md) after brainstorming with
     the user; there is no separate spec. English only. Format rules:
     docs/harness/conventions/plans.md. Validate with `uv run just plans-lint`.
     Every claim about existing code carries a file:line reference read while writing it. -->

## Context

<!-- What exists today (with references), what we need, the chosen approach and why (one short
     paragraph), and which existing files are imitated, e.g. "command shape follows
     apps/api/src/fragancia_api/modules/catalog/application/commands/create_brand.py:20-50". -->

## Out of scope

<!-- Explicit fence: refactors, adjacent bugs, UI polish not requested, other modules… -->

## Dependencies

<!-- For each depends_on: the plan and the interface it promises. "None" if empty. -->

None

## Steps

<!-- Numbered. Each step: a `Files:` line with backticked paths + (create)/(modify)/(delete),
     what to do, and the observable result. `uv run just plans-scope` reads the Files: lines;
     files not listed there are not touched. -->

1. **<Step title>**
   - Files: `apps/api/src/fragancia_api/modules/<module>/contracts.py` (create)
   - Do: …
   - Observable result: …

## Acceptance criteria

<!-- Verifiable in the RUNNING system: endpoints, status codes, JSON shapes, error codes, rows. -->

- [ ] …

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes/no  |       |
| application | yes/no  |       |
| http        | yes/no  |       |
| integration | yes/no  |       |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

<!-- LEFT EMPTY by the architect. Implementer: what the plan said / what reality is / what was
     done. Write "None" explicitly if none (required from status testing). -->

## Test coverage

<!-- LEFT EMPTY by the architect. Tester: coverage matrix from
     docs/harness/conventions/testing.md (Behavior | Source | Layer | Test | State). -->

## Review findings

<!-- LEFT EMPTY by the architect. Reviewer: checklist result + findings by severity with
     file:line and failure scenario. "All passed" explicitly if so. -->

## Verification

<!-- LEFT EMPTY by the architect. Verifier: suites run (decisive lines), flows driven vs.
     acceptance criteria, unhappy path, anything NOT VERIFIED flagged. -->
