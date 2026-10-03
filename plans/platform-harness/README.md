# platform-harness — The full AI harness from web-rh, ported to this repo

## Goal

Work in this repo the way it works in `~/codes/web-rh`: every non-trivial change flows through a
plan with a status-driven pipeline (plan → implement → tests → review → verify), executed by
role-specific agents in Claude Code and Codex, with the evidence of each phase written in the
plan, and with mechanical guardrails (plan lint/scope, destructive-action hooks, generated
adapters checked for drift). Also undo two divergences from web-rh introduced in phase 1.

## Plans

| Plan | Title                                                            | Depends on | Purpose                                                                  |
| ---- | ---------------------------------------------------------------- | ---------- | ------------------------------------------------------------------------ |
| 001  | Process: one env file, harness docs, full plan format, plan tooling | —          | Undo the env/test-db divergences; `docs/harness/`; plans-lint/status/scope |
| 002  | Enforcement: guard hooks, permissions, generated agents and skills  | 001        | Block destructive actions; roles → Claude/Codex adapters with drift check |

## Dependency notes

002 generates adapters from the role docs written in 001, and its skills call 001's tooling.

## Decisions with the user

1. (2026-10-02) Align with web-rh: a single env file per app (`apps/api/.env`), compose with
   inline defaults and no root `.env`; the test database created by a fixed-name `.sql` script.
2. (2026-10-02) Bring the whole harness: pipeline statuses, roles (architect, implementer,
   tester, reviewer, verifier), plan tooling, guard hooks, generated adapters. The earlier
   "lighter harness" was an assumption of the assistant, never the user's decision.
3. (2026-10-02) Adapters for **Claude Code and Codex**, like web-rh.
4. (2026-10-02) Skill names in **English**: `plan`, `implement`, `write-tests`, `review`,
   `verify`, `fix`, plus recipe skills `new-module`, `new-use-case`, `db-change`.
5. (2026-10-02) Everything is written in English (docs, plans, commit subjects), per the repo
   rule; tooling is Python (stdlib for hooks, so they start fast and need no venv).
6. (2026-10-02) Work reaches `main` through pull requests (the user chose PRs for phase 2).
7. (2026-10-02) Group related work into as few plans as possible (two plans, not five).
8. (2026-10-02) New pull request format: what changed, why, how, how it was verified, what's
   missing, risks and rollout (added to plan 001).

## Delivered

- **001** (2026-10-02): single env file (`apps/api/.env`) and fixed-name test database;
  `docs/harness/` (vision, workflow, security, roles, conventions incl. pull requests); full
  plan format and templates; `plans-lint` / `plans-status` / `plans-scope`.
- **002** (2026-10-03): guard hooks + `.claude/settings.json` permissions (three review rounds,
  best-effort denylist); generated Claude subagents, skills and Codex profiles with
  `harness-sync` / `harness-check`; recipe skills. Finding
  `plans/findings/platform-guard-bash-round3-bypasses.md` resolved by a fast-lane fix.
- PR #3.

## Considered and discarded

- **A lighter harness** (plans + ADRs only): discarded, see decision 2.
- **Node for hooks/tooling, copied verbatim from web-rh**: the repo has no Node until phase 3;
  Python ports keep one toolchain.
