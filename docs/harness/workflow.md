# Workflow — the 5-phase pipeline

The pipeline is change-type agnostic: features, fixes and refactors all flow through it (the
commit type reflects which). Small diagnosed fixes may take the fast lane. Everything else is
driven by a plan file `plans/<module>-<topic>/NNN-<slug>.md` inside an initiative (format:
[conventions/plans.md](conventions/plans.md), template: `plans/_TEMPLATE.md`). The plan's
frontmatter `status:` is the single source of truth for orchestration.

## Entry points (the pipeline is a menu, not a train)

> **A plan exists to control changes to product behavior.** Activities that don't change
> behavior — writing tests, reviewing, verifying, recon, documenting — are standalone: enter
> directly, anytime, no plan needed.

| Request                                           | Entry                                    | Plan?                                   |
| ------------------------------------------------- | ---------------------------------------- | --------------------------------------- |
| New feature / new module                          | Architect → full pipeline                | Yes                                     |
| Small **diagnosed** fix                           | Fast lane (skill `fix`)                  | No (regression test + verify mandatory) |
| Undiagnosed fix / >3 files / sensitive zone       | Architect → short plan                   | Yes                                     |
| Only write tests for existing code                | Tester (skill `write-tests`)             | No                                      |
| Only review a diff / branch / module              | Reviewer (skill `review` / subagent `reviewer`) | No                               |
| Only verify something works                       | Verifier (skill `verify`)                | No                                      |
| Refactor keeping behavior                         | Architect — characterization tests first | Yes, always                             |
| Redo **changing** behavior                        | Architect — it's a feature in disguise   | Yes                                     |
| Trivial adjustment, no new behavior (copy, style) | Fast lane                                | No                                      |
| Micro-feature (tiny new behavior)                 | Architect → mini-plan                    | Yes, short                              |

Two clarifications that make "quick but ordered" possible:

1. **Order lives in the conventions, not in the plan.** Architecture rules, nothing invented,
   scope discipline and the destructive-action rules apply ALWAYS, plan or no plan. The plan
   only adds coordination across phases, sessions and models.
2. **Plans scale down.** For a micro-feature every section may be one line; non-negotiable
   are exact files, Out of scope and acceptance criteria.

## Status lifecycle

```
draft → approved → implementing → testing → review → verify → done
                        ↘ blocked     (unmet depends_on or escalated deviation)
                        ↘ superseded  (terminal: abandoned or replaced)
```

| Status         | Who moves it here |
| -------------- | ----------------- |
| `draft`        | Architect         |
| `approved`     | **User only**     |
| `implementing` | Implementer (or the main session when starting a repair) |
| `testing`      | Implementer       |
| `review`       | Tester            |
| `verify`       | Reviewer          |
| `blocked`      | Any role          |
| `done`         | **User only**     |
| `superseded`   | **User only** (with `superseded_by:` when a replacement exists) |

Agents move the rest and update the frontmatter **in the same edit** as their last change of
the phase. Semantics of each status: [conventions/plans.md](conventions/plans.md).

Review comes **before** verify: findings go back to the implementer before spending a verify
run on code that will change anyway.

### Routing (how any session orchestrates)

When asked "continue plan NNN", "what's next?", or handed a plan without instructions: read
its `status` and apply this table. For the cross-plan overview never maintain a status doc
by hand — compute it: `uv run just plans-status` (`--all` includes done/superseded).

| `status`       | Next action                                        | Claude Code                                 | Codex profile                       |
| -------------- | -------------------------------------------------- | ------------------------------------------- | ----------------------------------- |
| `draft`        | User reviews/approves. Summarize the plan and ask. | (skill `plan` wrote it)                     | —                                   |
| `approved`     | Implement                                          | skill `implement` or subagent `implementer` | per `min_implementer` (table below) |
| `implementing` | Resume (read `## Deviations` for where it stopped) | same as above                               | same as above                       |
| `testing`      | Layered tests                                      | skill `write-tests` or subagent `tester`    | `tester` / `tester-high`            |
| `review`       | Checklist + bug hunt → `## Review findings`        | skill `review` or subagent `reviewer`       | `reviewer` / `reviewer-medium`      |
| `verify`       | Drive the running app → `## Verification`          | skill `verify` or subagent `verifier`       | `verifier`                          |
| `blocked`      | Report WHY to the user; do not work around it      | —                                           | —                                   |
| `done`         | Nothing. Changes = new plan or fast-lane fix.      | —                                           | —                                   |
| `superseded`   | Nothing. Follow `superseded_by`.                   | —                                           | —                                   |

The skill `plan` runs the architect role in the main chat; the skill `fix` runs the fast
lane. Skills and subagents are generated adapters (`HARNESS.md`).

### Dispatch policy

- **Attended (default — the user is at the keyboard): ASK before dispatching a subagent**,
  with the question tool, e.g. "plan 004 is in `testing`: dispatch `tester`, or run it
  inline?". Never spawn silently.
- **Unattended**: dispatch without asking only when the user explicitly handed over a batch
  ("continue the plans without asking me", a loop, an overnight queue).
- Stays in the main chat regardless of mode: bug diagnosis, anything needing conversation
  context (screenshots, pasted errors), and plan writing (the architect converses).

**Role purity applies inline too**: whoever writes tests does not fix product code (bugs come
back as documented gaps); a fix that grows past the fast-lane criteria stops and goes through
a plan, even if the change looks obvious.

## The pipeline

| #   | Phase     | Role                   | Model tier                 | Output                     | Gate to advance                                         |
| --- | --------- | ---------------------- | -------------------------- | -------------------------- | ------------------------------------------------------- |
| 1   | Plan      | `roles/architect.md`   | High (main chat)           | Plan file, `status: draft` | **User approves** → `approved`                          |
| 2   | Implement | `roles/implementer.md` | Per plan `min_implementer` | Code + `## Deviations`     | `uv run just check` green → `testing`                   |
| 3   | Tests     | `roles/tester.md`      | Mid                        | Tests + `## Test coverage` | `uv run just check` green (gaps as strict `xfail`) → `review` |
| 4   | Review    | `roles/reviewer.md`    | High                       | `## Review findings`       | Checklist passes, findings resolved → `verify`          |
| 5   | Verify    | `roles/verifier.md`    | Mid                        | `## Verification`          | **User accepts** → `done` (+ commit)                    |

### Evidence lives in the plan, not in chat

Every phase's deliverable persists to disk; a chat report evaporates with the session (and
survives subagent relay even worse). The architect writes the plan, the implementer fills
`## Deviations`, the tester fills `## Test coverage`, the reviewer fills `## Review findings`,
the verifier fills `## Verification`. A role's return message is a summary pointing at the
section, never the only copy. `uv run just plans-lint` refuses a status whose evidence section
is still empty.

## Fast lane (small fixes without a plan file)

Allowed **only if ALL of these hold**:

- It's a bug fix or trivial adjustment with a clear reproduction (not new behavior).
- Touches ≤ 3 files, in modules the user named.
- No schema/migration change, no new or changed endpoint or contract (`contracts.py`,
  `openapi.json`), no auth/permission change (declared access, `require_admin`), no money
  math (prices, totals, discounts, shipping costs), nothing in payments (Mercado Pago,
  webhooks), stock reservation or the order state machine.

The fast lane skips the _plan_, not the discipline: conventions apply, scope stays locked to
the fix, a regression test is added at the lowest layer that would have caught the bug,
`uv run just check` is green, and the fix is verified in the running app. Work order:
reproduce → regression test that fails → fix → `uv run just check` → verify live → commit
`fix(<scope>): <symptom fixed>` **only if the user authorized commits** (body: root cause +
regression test). If any criterion fails, or in doubt → it's a plan. A fix that grows
mid-flight stops and goes to the architect.

## Refactors (always a plan, never fast lane)

1. **Safety net first**: if the code lacks coverage, the plan's first steps are
   characterization tests that pass on the OLD code; they stay untouched and green throughout.
2. **Acceptance criterion = observable behavior unchanged**: same HTTP responses, same
   numbers, same `openapi.json`, same suites green. If the plan can't state this, it's a
   feature wearing a refactor label — split it.

## Cross-cutting rules

### Deviation protocol (what makes cheap implementers viable)

The implementer never resolves ambiguity creatively. If the plan contradicts reality (a file,
function, column, method, route that doesn't exist or differs):

1. **Stop** that step.
2. Log it in `## Deviations`: what the plan said / what reality is / what was done.
3. Cosmetic (typo in a path, renamed variable) → fix forward and record. Design or scope
   impact → `status: blocked` and escalate.

### Dependencies (`depends_on`)

Code is never planned or written against code that doesn't exist yet.

- **Architect**: may only assume code verified in the repo (cited `file:line`) or from plans
  with `status: done`. Anything else goes in `depends_on`, and dependent steps are written
  against the interface the dependency promises, clearly marked.
- **Implementer**: before starting, every `depends_on` must be `done`; otherwise
  `status: blocked` and stop.

### Commits (one per phase)

Each phase closes with one atomic commit that includes the plan file with its new status and
evidence ([conventions/commits.md](conventions/commits.md)): `docs(x): plan 003 for … (draft)`
→ `feat(x): …` → `test(x): … (plan 003)` → `fix(x): review findings (plan 003)` →
`docs(x): verification of plan 003 (PASS)` → `docs(x): plan 003 done`. Only the main session
commits, only when the user asked for or authorized commits, staging explicit paths. No AI
attribution trailers. Work reaches `main` through a pull request
([conventions/pull-requests.md](conventions/pull-requests.md)).

### Closing an initiative

When the last plan of a series is `done`, the main session fills the README's "Delivered"
section (what shipped, where it's documented) and moves any leftover ideas to "Considered and
discarded" or to findings. Commit: `docs(<scope>): close the <initiative> series`.

### Scope lock

No role touches files outside the plan's listed files plus append-only additions to shared
hot files (`HARNESS.md`). "I fixed something nearby while I was there" is a violation, not a
favor — the nearby problem becomes a finding in `plans/findings/`. `uv run just plans-scope
<plan>` makes it checkable (it always allows the plan, its initiative README, new findings,
the hot files, and derived files: new migrations when a `tables.py` is declared,
`apps/api/openapi.json` when a `contracts.py` or `http/router.py` is declared, `uv.lock` when a
`pyproject.toml` is declared).

### Dependencies on packages

New Python packages only through `uv add <pkg>` (`--dev` for tooling) inside a plan that lists
`pyproject.toml` (and therefore `uv.lock`); never `pip install`, never hand-edit `uv.lock`.
Read the installed version's documentation before using an API you are not sure about.

### Choosing `min_implementer`

- `small`: only mechanical plans — a CRUD that copies the reference module step by step,
  renames, copy/style changes. Every step names exact files and exact results.
- `mid`: default for everything else.
- `high`: the exception — implementation itself demands judgment even with a good plan
  (concurrency, outbox/transaction subtleties, cross-module flows, tricky migrations). If you
  reach for `high` because the steps are vague, the fix is a better plan, not a bigger model.
- **Always `mid`+** when the plan touches: money math (prices, totals, discounts, shipping),
  auth/permissions, payments (Mercado Pago, webhooks, idempotency), stock reservation or order
  state transitions, migrations that rewrite data. Errors there are silent (plausible-looking
  numbers), not exceptions.

#### Model dispatch (cost-aware — lowest option that satisfies the tier)

| Tier    | Claude Code (`Agent` → `model`) | Codex profile (`.codex/agents/`)           |
| ------- | ------------------------------- | ------------------------------------------ |
| `small` | `haiku`                         | `implementer-small` (`gpt-5.4-mini`, low)  |
| `mid`   | `sonnet`                        | `implementer` (`gpt-5.6-terra`, medium)    |
| `high`  | `opus`                          | `implementer-high` (`gpt-5.6-terra`, high) |

Fixed profiles for the other phases: tester → `sonnet` / Codex `tester` (`gpt-5.6-terra`,
medium), `tester-high` only when recon genuinely needs it; reviewer → `opus` / Codex
`reviewer` (`gpt-5.5`, high) or `reviewer-medium` (`gpt-5.5`, medium) for small diffs;
verifier → `sonnet` / Codex `verifier` (`gpt-5.6-terra`, medium). The architect runs in the
main chat with a high-tier model. Codex model names are deployment-specific and live only in
`scripts/harness/adapters.py`; change them there and run `uv run just harness-sync`. If a
model named in a profile is unavailable, report it — never substitute silently
([security.md](security.md)).

The orchestrating chat may be a stronger model than the dispatched tier. That does not raise a
plan's `min_implementer`: it preserves the plan's tier and dispatches the cost-appropriate model.

## Commands reference

```bash
uv run just plans-status                    # what needs attention, computed from frontmatter
uv run just plans-lint                      # plan format + status/evidence coherence (in just check)
uv run just plans-scope plans/x-y/001-z.md  # diff vs. the plan's file list (--base main by default)
uv run just check                           # lint, types, arch, unit+http+tooling tests, plans (+ hooks, harness: plan 002)
uv run just test-integration                # SQL adapters against fragancia_test (needs just up; migrates it)
uv run just openapi                         # regenerate apps/api/openapi.json after a contract change
uv run just harness-sync                    # regenerate agent/skill adapters (plan 002)
```

## Repair handoff

Reviewer/verifier record failures and leave the phase status unchanged. When resuming this
already-authorized plan, the main session records the repair reason and transitions
review/verify → implementing before invoking the implementer. In-scope product repairs run
testing → review → verify again. Test-only corrections stay with the tester. Preserve dated
historical evidence and mark previous results superseded by the repair; do not claim old
verification covers new code. Scope/design changes still require a deviation and user
decision. No new status is introduced.
