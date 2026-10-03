# Contributing

Read [AGENTS.md](AGENTS.md) first: it defines the plan-driven workflow and what is forbidden.
The rules live in one place each; this page points to them.

| Topic                         | Canonical source                                                                   |
| ----------------------------- | ---------------------------------------------------------------------------------- |
| Pipeline, statuses, fast lane | [docs/harness/workflow.md](docs/harness/workflow.md)                               |
| Plans and findings            | [docs/harness/conventions/plans.md](docs/harness/conventions/plans.md)             |
| Commits and branches          | [docs/harness/conventions/commits.md](docs/harness/conventions/commits.md)         |
| Pull requests                 | [docs/harness/conventions/pull-requests.md](docs/harness/conventions/pull-requests.md) |
| Backend / testing             | [backend.md](docs/harness/conventions/backend.md), [testing.md](docs/harness/conventions/testing.md) |
| Security of the harness       | [docs/harness/security.md](docs/harness/security.md)                               |

## In short

- **Branches** `feat/<initiative>`, `fix/<module>-<slug>`, `chore/<slug>`; work reaches `main`
  only through a pull request. `main` is always green.
- **Commits**: Conventional Commits in English, `type(scope): subject`, scope from
  [docs/modules.json](docs/modules.json) or `api`, `web`, `infra`, `ci`, `deps`, `docs`,
  `repo`, `harness`; one commit per phase including the plan file; explicit staging; **no AI
  attribution**. The `commit-msg` hook (installed by `uv run just bootstrap`) and the CI
  `commits` job run `scripts/commits.py`.
- **Pull requests** use `.github/pull_request_template.md` and answer: what changed, why, how,
  how it was verified, what's missing, risks and rollout. CI must pass
  ([.github/workflows/README.md](.github/workflows/README.md)).
- **Before pushing**: `uv run just check` (plus `uv run just test-integration` when persistence
  changed).

## Security

- Never commit real credentials. `.env` files are ignored; only `.env.example` (development
  values) is versioned. The `detect-private-key` hook blocks private keys.
- Local services listen on `127.0.0.1` only. Do not change that to reach them from the LAN
  without a plan.
