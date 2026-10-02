# CI

`ci.yml` runs on every pull request, on pushes to `main` and on demand. It only reads the
repository (`permissions: contents: read`), never publishes images and never deploys.
Every third-party action is pinned to a commit SHA with its version in a comment.

| Job       | What it proves                                                                  | Reproduce locally                                   |
| --------- | ------------------------------------------------------------------------------- | --------------------------------------------------- |
| `quality` | Lint, formatting, pre-commit hooks, mypy, import-linter and all unit tests      | `uv run just check`                                 |
| `commits` | Every commit in the PR follows the convention (PRs only)                        | `uv run scripts/commits.py --range origin/main..HEAD` |
| `api-integration` | Migrations apply, match the table definitions and reverse; integration tests pass against ephemeral PostgreSQL + Valkey | `uv run just test-integration` |
| `docker`  | The `runtime` and `migrator` images build (never pushed)                        | `docker build -f apps/api/Dockerfile --target runtime .` |
| `infra`   | `just bootstrap` works on a clean machine, is idempotent, and services respond  | `uv run just bootstrap` twice, then the smoke commands |

## Updating pinned actions

Resolve the tag to its commit and replace both the SHA and the version comment:

```bash
git ls-remote https://github.com/actions/checkout 'refs/tags/v7.0.1^{}' 'refs/tags/v7.0.1'
```

Never pin to a branch or a moving tag.
