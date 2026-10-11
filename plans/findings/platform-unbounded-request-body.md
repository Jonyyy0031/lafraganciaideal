---
status: open
module: platform
found: 2026-10-11
---

# The API reads request bodies of any size, before authentication

## Found while

Reviewing catalog-perfumes/006 (perfume photos), whose upload route is the first multipart one.

## What

- No middleware, server option or proxy config in the repo limits the request body size
  (`apps/api/src/fragancia_api/main/http.py` adds only `RequestContextMiddleware` and CORS;
  nothing under `infra/` or `docs/` sets a limit).
- FastAPI reads the whole body before it solves dependencies: `fastapi/routing.py:438-450`
  (`await request.form()` / `await request.body()`) runs before `solve_dependencies` at line 499,
  which is where `require_admin` / `require_permission` run. JSON bodies are read into memory;
  multipart file parts are spooled to a temporary file with no size cap (Starlette 1.7
  `MultiPartParser.max_part_size` applies only to non-file fields).

## Why it matters

An anonymous client can send a multi-GB JSON body to any POST/PUT/PATCH route and hold that much
memory per request, or a multi-GB multipart body to `POST /api/v1/admin/perfumes/{id}/photos` and
fill the temporary disk, before the 401. Plan 006 only bounds what its router reads from the
already-spooled file (finding H1 in its review covers that route).

## Suggested next step

A small platform plan: an ASGI middleware that answers 413 when `Content-Length` exceeds a
configured maximum and counts streamed bytes for chunked bodies (a larger maximum for the photo
route), plus the matching `client_max_body_size` in the production reverse proxy when it exists.
