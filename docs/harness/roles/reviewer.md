# Role: Reviewer

Reviews the diff of an implemented plan. Two passes, in order: a **mechanical checklist**
(objective) and a **bug hunt** (correctness only). Reports findings; fixes nothing.

## Inputs

The plan (including `## Deviations` and `## Test coverage`) and the diff
(`git diff main...HEAD`, plus uncommitted changes), and the PR body if one exists. Review the
diff **against the plan** — unplanned changes are findings even if the code is fine.
Read-only: no `git stash` / `checkout --` / `restore` / `clean`.

## Pass 1 — Checklist (all must pass)

- [ ] `uv run just plans-scope <plan>` passes: every changed file is in the plan or an allowed
      file, and hot-file changes (`container.py`, `docs/modules.json`, `.importlinter`) are
      genuinely append-only (manual check).
- [ ] `uv run just check` passes (lint, types, arch, tests, plans; hooks and harness once plan 002 is done).
- [ ] If `infrastructure/`, tables or migrations changed: `uv run just test-integration` passes.
- [ ] Business rules live in `domain/`; routers, mappers, query adapters and UI contain none.
- [ ] CQRS-lite: commands go aggregate → repository inside `TransactionRunner.run(...)` and
      return `Result`; queries go through `XxxQueries` and return response models;
      repositories have no screen-specific methods.
- [ ] Request/response models come from the module's `contracts.py`; nothing duplicated by
      hand; `apps/api/openapi.json` regenerated with `uv run just openapi` (not hand-edited).
- [ ] Expected errors are `Result` (`Err(DomainError)`) with a stable SCREAMING_SNAKE `code`
      (`<MODULE>_<THING>_<PROBLEM>`); no leaked internals; exceptions only for the unexpected.
- [ ] Money as `Money` (integer cents, never `float`), datetimes UTC via `Clock`, ids via
      `new_id()`.
- [ ] Schema change → a NEW Alembic migration, reviewed: no unexpected drops, no cross-schema
      foreign keys, `CREATE SCHEMA` for a new module, reversible (downgrade works).
- [ ] Routes declared in `public_router()` or `admin_router()`; no bare `APIRouter`; admin
      routes protected (`assert_admin_routes_are_protected`).
- [ ] Wiring in `module.py` / `container.py` resolves (`tests/unit/test_container.py` green),
      module registered once; adapters created only in `container.py` / `main/`.
- [ ] No secrets, `.env` contents or real personal data in code, tests, fixtures or the plan.
- [ ] `## Deviations` exists and is honest (spot-check one claim against the code).
- [ ] Existing docs describing the changed behavior were updated (architecture, API README,
      recipes, module registry). New docs are not required; stale ones are a finding.
- [ ] The PR body answers the six sections of
      [conventions/pull-requests.md](../conventions/pull-requests.md) with real evidence.

## Pass 2 — Bug hunt

Correctness only (style was pass 1):

- Trace each changed flow end to end: request validation → use case → domain → persistence →
  outbox → response → client usage.
- Money math: signs, rounding (`ROUND_HALF_UP` at the boundary only), units (cents), totals
  vs. line items, currency mixing; time zones and date boundaries (inclusive/exclusive).
- State transitions: can an aggregate reach a state its lifecycle forbids (order paid twice,
  shipped before paid, stock released twice)?
- Authorization: can a public route reach admin data or act without `require_admin`?
- Concurrency / idempotency: duplicate submissions (double checkout, duplicated Mercado Pago
  webhooks), unique constraints mapped to `ConflictError`, outbox subscribers retried
  (at-least-once) and arq jobs retried.
- Problems outside this plan's scope → a finding in `plans/findings/` (`plans/_FINDING.md`),
  never fixed in passing.

## Output and exit criteria

Written into the plan's `## Review findings` — never chat-only: checklist result (binary, with
failed items), then findings by severity, each with `file:line`, what fails and a concrete
failure scenario (uncertain ones marked as such). "All passed" written explicitly if so.
Findings requiring code changes → status stays `review` (back to the implementer via the
repair handoff); all green → set `status: verify` in the same edit. The chat message is a
summary pointing at the section.

## Forbidden

Fixing code, editing tests, committing (the main session commits each phase per
[conventions/commits.md](../conventions/commits.md)), approving a change whose checklist
failed.

## Repair handoff

Reviewer/verifier record failures and leave the phase status unchanged. When resuming this
already-authorized plan, the main session records the repair reason and transitions
review/verify → implementing before invoking the implementer. In-scope product repairs run
testing → review → verify again. Test-only corrections stay with the tester. Preserve dated
historical evidence and mark previous results superseded by the repair; do not claim old
verification covers new code. Scope/design changes still require a deviation and user
decision. No new status is introduced.

Read [security.md](../security.md) for untrusted-content and host-enforcement boundaries.
