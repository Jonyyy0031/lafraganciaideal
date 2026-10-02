# Contributing

Read [AGENTS.md](AGENTS.md) first: it defines the plan-driven workflow and what is forbidden.

## Branches

`feat/<module>-<topic>`, `fix/<module>-<topic>`, `chore/<module>-<topic>` (e.g.
`chore/platform-infrastructure`). `main` is always green.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/), in English:

```
type(scope): what changed, in business terms

Optional body: why, and anything a reviewer must know.
```

- **type**: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`,
  `chore`, `revert`. Append `!` for breaking changes (`feat(orders)!: …`).
- **scope** (required): a module from [docs/modules.json](docs/modules.json) or a cross-cutting
  scope: `api`, `web`, `infra`, `ci`, `deps`, `docs`, `repo`. A new module is registered in
  `docs/modules.json` by the plan that creates it.
- **subject**: says what changed — never `changes`, `wip`, `fix`, `update`…; header ≤ 100 chars.
- **No AI attribution**: no `Co-Authored-By` trailers, no "Generated with …" lines.
- One logical change per commit; stage files explicitly (no `git add -A` / `git add .`).

The `commit-msg` hook (installed by `just bootstrap`) and the CI `commits` job run
`scripts/commits.py`. Examples:

```
chore(infra): add development services with docker compose      ✔
feat(orders): cancel unpaid orders after 48 hours                ✔
fix(payments): ignore duplicated mercado pago webhooks           ✔
changes                                                          ✘ not conventional
feat: add catalog                                                ✘ scope required
fix(catalog): wip                                                ✘ generic subject
```

## Pull requests

Use the template in `.github/pull_request_template.md`: link the plan, list the changes, and
state how it was verified. CI (`quality`, `commits`, `infra`) must pass. See
[.github/workflows/README.md](.github/workflows/README.md).

## Before pushing

```bash
just check
```

## Security

- Never commit real credentials. `.env` is ignored; only `.env.example` (development values)
  is versioned. The `detect-private-key` hook blocks private keys.
- Local services listen on `127.0.0.1` only. Do not change that to reach them from the LAN
  without a plan.
