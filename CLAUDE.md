@AGENTS.md

## Claude Code specifics

- **Pipeline skills** (generated from `docs/harness/`; never edited by hand):
  - `plan` — architect: writes a plan in `plans/` (does not touch code).
  - `implement` — implementer, inline, for an approved plan.
  - `write-tests` — tester: layered tests under "nothing invented".
  - `review` — reviewer inline (for a clean context prefer the `reviewer` subagent).
  - `verify` — verifier: QA against the running app.
  - `fix` — fast lane for small diagnosed bugs.
- **Recipe skills** (hand-written): `new-module`, `new-use-case`, `db-change`.
- **Subagents** (`.claude/agents/`, generated): `implementer` (model by `min_implementer`:
  small→haiku, mid→sonnet, high→opus), `tester` (sonnet), `reviewer` (opus), `verifier`
  (sonnet). Ask before dispatching when the user is present (see AGENTS.md → "Subagent
  dispatch").
- **Active hooks** (`.claude/settings.json`): `guard_bash`, `guard_files` and `guard_read` block
  destructive actions and secret reads; when one blocks you, read the reason and use the
  suggested alternative — never try to get around it. `format_file` runs ruff after each edit
  of a `.py` file.
- To change a subagent or pipeline skill: edit `docs/harness/roles/*.md` or
  `scripts/harness/adapters.py` and run `uv run just harness-sync`.
- Personal preferences that must not be versioned: `CLAUDE.local.md` or
  `.claude/settings.local.json` (both git-ignored).
