---
status: resolved
module: platform
found: 2026-10-03
plan: platform-harness/002
---

# guard_bash still allows `set -k` env injection and git plumbing that discards work

## Found while

Re-reviewing platform-harness/002, round 3 (after the second repair). The main session
decided that round was the last repair round for the guard denylist (`docs/harness/security.md`
says it is best-effort), so new bypasses are recorded here instead of reopening the plan. None of
them is a regression from the repairs: the round-2 repair diff (`9aeef1e..341e6fb`) only adds
blocks and touches none of these paths.

## What

All exit 0 from the live `.claude/hooks/guard_bash.py` (payload on stdin,
`CLAUDE_PROJECT_DIR` = repo):

1. **`set -k` makes later `NAME=value` arguments environment variables** (bash `keyword`
   option). `set` is not in `ENV_SETTERS` (`guard_bash.py:109,751`), and assignment-shaped
   arguments after the program are not inspected.
   `set -k; git x GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.x GIT_CONFIG_VALUE_0=stash`
   runs `git stash`. Confirmed in a scratch repo with `sh -c` (bash) and the harmless value
   `status`: `git x` printed `git status`. Same route works for `core.hooksPath` before
   `git commit` (skipping hooks) and for any other `GIT_*` / `PG*` variable. (The Bash tool on
   this machine runs fish, where `set -k` means something else; through `sh -c` it is bash.)
2. **Git plumbing that discards worktree changes** is not covered by `_check_git`
   (`guard_bash.py:471`): `git checkout-index -f -a` and `git read-tree -u --reset HEAD`
   both overwrote a modified tracked file in a scratch repo (confirmed). Equivalent to the
   blocked `git checkout -- .` / `git reset --hard`.
3. **Other git forms not in the denylist** (not executed): `git rm -f <file>` (deletes a tracked
   file with uncommitted changes), `git worktree remove --force <dir>`,
   `git update-ref -d refs/heads/<b>` (branch delete without `-D`),
   `git reflog expire --expire=now --all` (drops the recovery net for lost commits).
4. Documented residual still open: `docker compose … exec postgres dropdb x` (arbitrary programs
   inside the container are not analyzed).

## Why it matters

Item 1 re-opens every git rule (stash, reset --hard, no-verify) with one extra word, the same
class as the round-2 `export` finding. Item 2 discards uncommitted work that may belong to
another agent sharing the worktree. Items 3–4 are lower impact.

## Resolution (2026-10-03, fast lane, user's decision)

Fixed in `.claude/hooks/guard_bash.py`: `set` with any argument is rejected (covers `set -k`
and `set -o keyword`); `_check_git_plumbing` rejects `checkout-index -f`, `read-tree -u|--reset`,
`rm -f`, `worktree remove --force`, `update-ref -d` and `reflog expire|delete` (abbreviations and
clusters included); `dropdb`, `dropuser` and `pg_resetwal` are rejected on the host and inside
`docker … exec`. Regression tests: `ROUND3_FINDING_BLOCKS` and the safe-form allows in
`scripts/harness/test_hooks.py` (all 13 reproduced bypasses passed the guard before the fix).
Verified live: the session's hook refused `set -e`; `uv run just check` green (harness 682).
The guard stays a best-effort denylist (`docs/harness/security.md`).

## Suggested next step

A fast-lane fix in guard_bash: reject `set` with any option argument other than read-only
forms (or reject `set` entirely), and treat `checkout-index`, `read-tree -u`, `rm -f`,
`worktree remove --force`, `update-ref -d` and `reflog expire|delete` as blocked git
subcommands; or accept and close given the best-effort stance. The user decides.
