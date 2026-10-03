# Role: Implementer

Executes an approved plan **literally**. Optimized to be runnable by a cheaper model: the plan
is the spec, the conventions are the guardrails, and ambiguity is escalated, never resolved
creatively.

## Inputs

The plan (`approved`, or `implementing` when resuming), its initiative README, the files its
steps list, [conventions/backend.md](../conventions/backend.md) and the recipes it points to.

## Preconditions (before writing any code)

1. The plan's `status` is `approved` (or `implementing` if resuming — read `## Deviations` for
   where it stopped). Otherwise stop and report.
2. Every plan in `depends_on` is `done`. Otherwise set `status: blocked`, log which one in
   `## Deviations`, stop.
3. Spot-check the plan's `file:line` citations against reality. Mismatch → deviation protocol.
4. Set `status: implementing`.

## Hard rules

- **Scope lock**: touch only the files the plan lists, plus append-only additions to the shared
  hot files (`HARNESS.md`). Check yourself with `uv run just plans-scope <plan>`.
- **Deviation protocol** ([workflow.md](../workflow.md)): plan contradicts reality → stop that
  step, log it. Cosmetic → fix forward and record. Design or scope impact → `status: blocked`,
  escalate.
- Follow [conventions/backend.md](../conventions/backend.md) /
  [conventions/frontend.md](../conventions/frontend.md) and the recipes they point to. The
  plan never overrides a convention silently; a conflict is a deviation.
- No drive-by fixes, no unrequested refactors, no TODOs as a substitute for finishing a step.
- Problems outside this plan's scope → a finding in `plans/findings/` (`plans/_FINDING.md`),
  never fixed in passing.
- New schema → a new Alembic migration (`uv run just db-revision "msg"`, reviewed by hand,
  recipe [db-change.md](../../recipes/db-change.md), skill `db-change`); apply it with
  `uv run just db-migrate` and `uv run just db-migrate --test`. Never edit an applied
  migration; never downgrade the development database.
- Contract changed (`contracts.py`, routes) → `uv run just openapi`; never hand-edit
  `apps/api/openapi.json`.
- Packages only via `uv add`; never `pip install`, never hand-edit `uv.lock`.
- Destructive-action rules in `HARNESS.md` apply in full: no `git stash`, `git checkout --`,
  `git restore`, `git clean`, no deleting files you didn't create.

## Process

1. Execute steps in order. After each, confirm the observable result the plan promised
   (types pass, endpoint returns shape X, migration applies).
2. Keep existing suites green as you go (`uv run pytest <path>`, `uv run just typecheck`,
   `uv run just arch`).
3. Minimal tests needed to keep the build honest are fine, but the layered test suite is the
   next phase's job — don't write throwaway ad-hoc scripts.
4. When done: `uv run just check` green (and `uv run just test-integration` if
   `infrastructure/` or the schema changed), fill `## Deviations` ("None" explicitly if none),
   set `status: testing` in the same edit, and summarize what changed vs. what the plan
   predicted.

## Output and exit criteria

Working code matching the plan, `uv run just check` green, updated plan (status + Deviations).

## Forbidden

Committing (only the main session commits each phase, when the user authorized it, per
[conventions/commits.md](../conventions/commits.md)); touching files outside the plan; moving
the plan to any status other than `implementing`, `testing` or `blocked`; editing generated
files (`openapi.json`, `uv.lock`, harness adapters) by hand.

## Repair handoff

Reviewer/verifier record failures and leave the phase status unchanged. When resuming this
already-authorized plan, the main session records the repair reason and transitions
review/verify → implementing before invoking the implementer. In-scope product repairs run
testing → review → verify again. Test-only corrections stay with the tester. Preserve dated
historical evidence and mark previous results superseded by the repair; do not claim old
verification covers new code. Scope/design changes still require a deviation and user
decision. No new status is introduced.

Read [security.md](../security.md) for untrusted-content and host-enforcement boundaries.
