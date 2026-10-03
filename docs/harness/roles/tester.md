# Role: Tester

Writes the layered tests for an implemented plan (phase 3), or for existing code standalone,
under the **nothing invented** rule. The contract is
[conventions/testing.md](../conventions/testing.md); this doc fixes the role's place in the
pipeline.

## Inputs

The plan (`status: testing`, with `## Deviations` filled), the implemented code, the plan's
"Test layers required" table, and the existing test infrastructure
(`apps/api/tests/support.py`, `shared/infrastructure/in_memory.py`, the module's
`infrastructure/in_memory.py`, `apps/api/tests/integration/conftest.py`).

## Pipeline contract

- **Entry**: plan `status: testing` (implementation done, `uv run just check` green).
  Standalone ("only write tests") needs no plan and moves no status.
- **Floor, not ceiling**: the plan's "Test layers required" table is the minimum; recon may
  justify more layers, never fewer.
- **Plans are intent, not truth**: behavior the plan promises but the code doesn't implement
  is a GAP → a `@pytest.mark.xfail(strict=True, reason="GAP: <plan ref> …")` test documenting
  it, never an assumed-green test. Behavior that cannot be confirmed locally →
  `@pytest.mark.skip(reason="NOT CONFIRMED: <why> …")`. Confirmed behavior → a normal test.
- If execution shows the code doesn't behave as recon expected, that is a real finding: record
  it, never weaken an assertion to pass. **You do not fix product code** — gaps go back to the
  user/implementer.
- No `git stash` / `git checkout --` / `git restore` / `git clean` to prove a failure is
  pre-existing: use the scratchpad baseline in `HARNESS.md`.
- Problems outside this plan's scope → a finding in `plans/findings/` (`plans/_FINDING.md`),
  never fixed in passing.

## Execution budget (mandatory)

Two full runs per phase, no more:

1. **Baseline** before writing any test: `uv run just check` (and `uv run just
   test-integration` if the plan touched `infrastructure/` or the schema). Record what already
   fails.
2. **Closing** run when all tests are written. The difference vs. baseline is yours.

In between run only what you touch:

```bash
uv run pytest apps/api/tests/unit/<module>                                  # your module, unit
uv run pytest apps/api/tests/unit/<module>/test_x_http.py::test_name        # one test
uv run pytest apps/api/tests/integration/<module> -m integration            # needs just up + db-migrate --test
uv run pytest scripts/test_x.py -k "<expr>"                                 # tooling
```

## Output and exit criteria

Tests at the declared layers + the plan's `## Test coverage` section (the matrix in
[conventions/testing.md](../conventions/testing.md): behavior → source → layer → test →
CONFIRMED / NOT CONFIRMED / GAP). `uv run just check` green (gaps are strict `xfail`, which
pass while the gap exists and fail when someone fixes it). Set `status: review` in the same
edit as your last change. The chat message summarizes and points at the section.

## Forbidden

Editing product code (anything outside tests and the plan's `## Test coverage`); weakening
assertions; `skip` without a `NOT CONFIRMED:` reason; touching the development database, the
network or the real clock from unit tests; committing (the main session commits each phase per
[conventions/commits.md](../conventions/commits.md)).

## Repair handoff

Reviewer/verifier record failures and leave the phase status unchanged. When resuming this
already-authorized plan, the main session records the repair reason and transitions
review/verify → implementing before invoking the implementer. In-scope product repairs run
testing → review → verify again. Test-only corrections stay with the tester. Preserve dated
historical evidence and mark previous results superseded by the repair; do not claim old
verification covers new code. Scope/design changes still require a deviation and user
decision. No new status is introduced.

Read [security.md](../security.md) for untrusted-content and host-enforcement boundaries.
