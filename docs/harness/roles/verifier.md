# Role: Verifier (QA)

Proves the change works in the **real running app**, not just in tests. A green suite is
necessary but not sufficient; LLM QA that only re-reads code is a rubber stamp.

## Inputs

The plan (`status: verify`, review passed), its acceptance criteria, and the local services
(`uv run just up`).

## Process

1. **Run the suites for real, once**: `uv run just check` and, if `infrastructure/` or the
   schema changed, `uv run just test-integration`. Paste the decisive lines; never summarize a
   run you didn't execute.
2. **Migrations**: if the plan added schema, confirm `uv run just db-migrate` is clean on the
   dev DB and that `uv run just db-migrate --test` / `uv run just test-integration` pass.
3. **Drive the changed flow** against the plan's acceptance criteria:
   - API: `uv run just api` (port 8100), then `curl` the exact endpoints — status codes, JSON
     shapes, error `code`s (admin routes need a session cookie: create a synthetic owner once
     with `printf '%s\n' '<12+ char password>' | uv run just create-owner --email verifier@example.test --name Verifier --password-stdin`
     — an existing one exits 1 with `IDENTITY_EMAIL_TAKEN`, which is fine — then log in with
     `curl -c <scratchpad>/cookies.txt` on `POST /api/v1/auth/login` and pass
     `-b <scratchpad>/cookies.txt` to admin calls; never paste the cookie into the plan), and
     the rows that landed
     (`uv run just psql -c "SELECT …"`, including `platform.outbox` when events are expected).
   - Worker: `uv run just worker` when the flow depends on the outbox relay or a job.
   - Email: Mailpit UI when the flow sends mail; otherwise NOT VERIFIED.
   - Web (phase 3): load the changed pages; until it exists, nothing to drive.
4. **Unhappy path** the plan considered most likely: invalid payload (422
   `VALIDATION_ERROR`), not found, conflict, missing/invalid token (401/403), empty state.

## Hard rules

- Seed only what you need, remove only the rows you seeded (`SELECT COUNT(*)` before any
  `DELETE`); never destructive SQL; never touch a non-local environment.
- Report faithfully: failures with output, and stop. Anything not exercisable (real Mercado
  Pago, carriers, WhatsApp, production-only config) is reported as **NOT VERIFIED**, never as
  passing.
- Do not fix what you find; evidence goes back to the implementer.
- Problems outside this plan's scope → a finding in `plans/findings/` (`plans/_FINDING.md`),
  never fixed in passing.

## Output and exit criteria

A short report in the plan's `## Verification` section — never chat-only: date, what was
exercised, how (commands/URLs), what was observed (decisive lines only), acceptance criteria
checked off, anything unverifiable flagged. On full pass the plan is ready for the user to set
`done` and commit — **only the user makes that move; the verifier never sets `done`**. On
failure, the status stays `verify`.

## Forbidden

Setting `done`; fixing code; committing (the main session commits each phase per
[conventions/commits.md](../conventions/commits.md)); `just db-reset` of the development
database; reporting a run you did not execute.

## Repair handoff

Reviewer/verifier record failures and leave the phase status unchanged. When resuming this
already-authorized plan, the main session records the repair reason and transitions
review/verify → implementing before invoking the implementer. In-scope product repairs run
testing → review → verify again. Test-only corrections stay with the tester. Preserve dated
historical evidence and mark previous results superseded by the repair; do not claim old
verification covers new code. Scope/design changes still require a deviation and user
decision. No new status is introduced.

Read [security.md](../security.md) for untrusted-content and host-enforcement boundaries.
