# API image. Build from the repository root:
#   docker build -f apps/api/Dockerfile --target runtime  -t fragancia-api .
#   docker build -f apps/api/Dockerfile --target migrator -t fragancia-migrator .
# The worker is the runtime image with another command:
#   arq fragancia_api.main.worker.WorkerSettings
# Base images are pinned by digest (see README → "Updating infrastructure images").

# Source verified on 2026-10-02: python:3.14-slim (3.14.8)
FROM python@sha256:0741d101873c12ab927e6f8653feb8862b9bd58771177acb1b885b95141f91b4 AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

FROM base AS builder
# Source verified on 2026-10-02: ghcr.io/astral-sh/uv:0.12.9
COPY --from=ghcr.io/astral-sh/uv@sha256:8b940d3a9d65bed080436972241af2e21c84b5e8c9193f7014ed71479ee795ff /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_NO_CACHE=1
WORKDIR /src
# Dependencies first (cached layer), then the API code.
COPY pyproject.toml uv.lock .python-version ./
COPY apps/api/pyproject.toml apps/api/pyproject.toml
RUN uv sync --frozen --no-dev --package fragancia-api --no-install-workspace
COPY apps/api/src apps/api/src
COPY apps/api/README.md apps/api/README.md
RUN uv sync --frozen --no-dev --package fragancia-api --no-editable

FROM base AS runtime
RUN useradd --system --uid 10001 --no-create-home --home-dir /app app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    APP_ENV=production
USER app
EXPOSE 8100
CMD ["uvicorn", "fragancia_api.main.http:create_app", "--factory", "--host", "0.0.0.0", "--port", "8100", "--proxy-headers"]

FROM runtime AS migrator
COPY --chown=app:app apps/api/alembic.ini /app/alembic.ini
COPY --chown=app:app apps/api/migrations /app/migrations
CMD ["alembic", "upgrade", "head"]
