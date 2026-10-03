# Plan conventions

Plans are the orchestration backbone: state lives in their frontmatter, phases hand off through
them, and the evidence of every phase accumulates in them. One canonical location: **`plans/`**.

## Structure: initiatives grouped by module

```
plans/
├── _TEMPLATE.md                      plan template
├── _INITIATIVE.md                    initiative README template
├── _FINDING.md                       finding template
├── catalog-perfumes/                 ← initiative = <module>-<topic>
│   ├── README.md                     ← index: goal, order, decisions, delivered, discarded
│   ├── 001-perfume-aggregate-and-sizes.md
│   ├── 002-admin-endpoints-and-contract.md
│   └── 003-fix-findings-review.md
├── orders-checkout/
│   └── …
└── findings/                         ← discovered, not (yet) planned
    └── payments-duplicated-webhook-retries.md
```

- **Initiative** = a coherent series of plans toward one goal. Directory name
  `<module>-<topic>` (or exactly `<module>` for a foundational series): `<module>` is a name
  from [docs/modules.json](../../modules.json), `<topic>` an English kebab-case ASCII slug
  (`catalog-perfumes`, `orders-checkout`, `platform-harness`). An initiative belongs to ONE
  module; cross-module work is its own initiative under the module that owns the change.
  Existing directories created before this rule (`platform-infraestructura`) keep their names.
- **Numbering** `NNN` is zero-padded and sequential **within its initiative**; every
  initiative starts at 001, so parallel branches never collide. **Never renumber** a plan
  once it exists.
- **Plan file names**: `NNN-<slug>.md`, slug in English kebab-case ASCII, describing the
  deliverable (`002-made-to-order-availability.md`), not the phase.
- **Fix-up plans** for review/verification findings too big for the plan itself are new plans
  in the same initiative: `NNN-fix-findings-<topic>.md`, with `depends_on` on the plan they fix.
- No loose plans in `plans/` root. `_`-prefixed files are templates, ignored by the tooling.

## The initiative README (`README.md`, from `_INITIATIVE.md`)

The initiative's index and memory; any session reads it before touching the series.

1. **Goal** — the business outcome, in one paragraph.
2. **Plans** — table in execution order: plan, title, depends on, one-line purpose. **No
   status column**: status lives only in each plan's frontmatter;
   `uv run just plans-status <initiative>` computes the view (a hand-kept status column always
   drifts).
3. **Dependency notes** — why an order exists when it isn't obvious.
4. **Decisions with the user** — dated, numbered. Business rules, scope calls, "not in this
   delivery". The architect records every decision the user made while brainstorming here;
   later plans cite them ("per decision 3 of the README").
5. **Delivered** — filled when the series closes: what shipped, where it's documented.
6. **Considered and discarded** — ideas evaluated and rejected, with the reason, so nobody
   re-proposes them blind.

## Frontmatter (required on every plan)

```yaml
---
status: draft # draft | approved | implementing | testing | review | verify | blocked | done | superseded
module: catalog # must match the initiative's <module> prefix
min_implementer: mid # small | mid | high  (rules in workflow.md)
depends_on: [] # "002" (same initiative) or "catalog-perfumes/002" (another one)
# superseded_by: catalog-perfumes/004   # only with status: superseded
---
```

Semantics of each status:

| Status         | Meaning                                                       | Who moves it here |
| -------------- | ------------------------------------------------------------- | ----------------- |
| `draft`        | Written, not yet approved. Nobody implements a draft.         | Architect         |
| `approved`     | User accepted the plan as the spec.                           | **User only**     |
| `implementing` | Implementer started (resume point: `## Deviations`).          | Implementer       |
| `testing`      | Code done, `uv run just check` green, Deviations filled.      | Implementer       |
| `review`       | Tests written, Test coverage filled.                          | Tester            |
| `verify`       | Review passed, findings resolved.                             | Reviewer          |
| `done`         | Verified in the running app and accepted.                     | **User only**     |
| `blocked`      | Unmet `depends_on` or an escalated deviation; reason in plan. | Any role          |
| `superseded`   | Abandoned or replaced (`superseded_by`). Terminal.            | **User only**     |

- `depends_on` is the anti-invention contract: steps may not assume code from a non-`done`
  plan unless it is listed here; the implementer refuses to start while any dependency isn't
  `done`.
- Whoever finishes a phase updates `status` in the same edit as their last change.

## Required sections (in this order)

Plans scale down (a 15-line mini-plan is valid). Non-negotiable even then: exact files,
Out of scope, acceptance criteria.

1. **Context** — what exists today with `file:line` citations; chosen approach and why; which
   files are being imitated; which README decisions apply.
2. **Out of scope** — explicit fence for the implementer. Must have content unless the plan is
   `draft` or `superseded`.
3. **Dependencies** — for each `depends_on`, the interface it promises (or "None").
4. **Steps** — numbered; each with a `Files:` line listing backticked paths followed by
   `(create)`, `(modify)` or `(delete)`, what to do, and the observable result.
   `uv run just plans-scope` reads the `Files:` lines (backticked tokens containing `/` or `.`
   and no space, inside `## Steps`); a path missing there is out of scope. A path ending in
   `/` declares a whole directory.
5. **Acceptance criteria** — checkbox statements the verifier checks in the running app.
6. **Test layers required** — the table from the template.
7. **Deviations** — empty until the implementer fills it ("None" if none).
8. **Test coverage** — empty until the tester fills it (matrix from `testing.md`).
9. **Review findings** — empty until the reviewer fills it.
10. **Verification** — empty until the verifier fills it.

A section counts as empty when it holds only whitespace and HTML comments.

### Evidence required by status

| From status on | Section that must have content |
| -------------- | ------------------------------ |
| `testing`      | `## Deviations`                |
| `review`       | `## Test coverage`             |
| `verify`       | `## Review findings`           |
| `done`         | `## Verification`              |

## Findings (`plans/findings/<module>-<slug>.md`, from `_FINDING.md`)

Something discovered while working that is **out of the current plan's scope**: a bug in
another module, a data inconsistency, a risky assumption. Never fixed "while passing by" —
documented here with evidence (`file:line`, queries, numbers), then triaged by the user.

```yaml
---
status: open # open | deferred | planned | resolved | discarded
module: payments
found: 2026-10-02 # date discovered
plan: payments-mercado-pago/004 # required when planned/resolved: the plan that handles it
---
```

`uv run just plans-status` lists open findings so they don't get lost.

## What `uv run just plans-lint` enforces

Exit 1 with one `where: message` line per problem.

- Initiative directory names are kebab-case, their `<module>` prefix is in the registry, and
  every initiative has a `README.md`. No loose plans, one level deep, `NNN-slug.md`, numbers
  unique within the initiative.
- Frontmatter present and valid YAML; `status` is one of the nine statuses; `module` is in the
  registry and equals the initiative's owner module; `min_implementer` is `small|mid|high`;
  `depends_on` is a list; `superseded_by` only with `status: superseded`.
- `depends_on` targets exist, no self-dependency, no cycles (checked even in `draft`); from
  `implementing` on, every dependency is `done`. Approved plans may wait for unfinished
  dependencies but cannot start implementing against them.
- All ten required sections present, in order; Out of scope has content unless
  `draft`/`superseded`; evidence matches status (table above).
- Findings: kebab-case slug, valid YAML, `status` in the five values, `module`, `found` date,
  and a resolvable `plan` when `planned`/`resolved`.

## What `uv run just plans-scope <plan> [--base main]` checks

Changed set = committed diff since `merge-base <base> HEAD` (both paths of renames) ∪ staged ∪
worktree ∪ untracked. Allowed = declared paths (exact or under a declared `dir/`), the plan
file, its initiative README, `plans/findings/*`, the hot files (`HARNESS.md`), new
`apps/api/migrations/versions/*` when an `infrastructure/tables.py` is declared,
`apps/api/openapi.json` when a `contracts.py` or `http/router.py` is declared, and `uv.lock`
when a `pyproject.toml` is declared. It prints counts, hot files to review as append-only,
declared-but-unchanged paths, and out-of-scope files (exit 1). Git or usage errors exit 2 —
never an empty success. Hot-file content remains a manual append-only review.

## Language

Plans, READMEs and findings are written in English (repo rule). Customer-facing Spanish copy
(UI texts, email subjects) is quoted verbatim where a plan specifies it.
