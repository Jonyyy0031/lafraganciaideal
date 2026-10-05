# 0009 — Opaque back-office sessions in PostgreSQL, carried by a cookie

- **Status**: Accepted
- **Date**: 2026-10-03

## Context

The back office needs real accounts (owner and staff) instead of the development stand-in
that compared a static `ADMIN_DEV_TOKEN` against `Authorization: Bearer <token>`. A session
must end at once when its user signs out, changes their password, closes it from another
device or is deactivated (planned). The admin panel is an Angular SSR app on the same origin
(phase 3), so the browser can carry the session itself. Decisions:
[initiative README](../../plans/identity-access/README.md) #1–3, #7;
plan [001](../../plans/identity-access/001-users-login-sessions-and-roles.md).

## Decision

- **Opaque tokens.** `POST /api/v1/auth/login` creates a random token
  (`secrets.token_urlsafe(32)`). Only its SHA-256 hex digest is stored, in
  `identity.sessions`; every admin request looks the session up by that digest
  (`ResolveSessionActor`, the `ActorResolver` behind `require_admin`). Revoking is setting
  `revoked_at`.
- **The cookie is the only transport.** The token travels in the `fragancia_session` cookie:
  httpOnly, `SameSite=Strict`, `Path=/api/v1`, `Secure` in production. Login never returns the
  token in the body, and the `Authorization` header is not read. A 401 carries no
  `WWW-Authenticate` header. Scalar works because the browser stores the cookie on login.
- **The development token is removed** (`DevTokenActorResolver`, `ADMIN_DEV_TOKEN`): there is
  one authentication path in every environment, no back door if `APP_ENV` is misconfigured,
  and no action is recorded under a non-user. The first account comes from
  `uv run just create-owner`.
- **Lifetimes.** A session expires after `SESSION_IDLE_MINUTES` without use (default 120) and
  at the latest `SESSION_MAX_HOURS` after login (default 12). `last_seen_at` is written at most
  once a minute.
- **Throttle before verification.** Each login attempt is counted per email and per IP in
  `identity.login_throttle` with one atomic `INSERT … ON CONFLICT DO UPDATE … RETURNING`
  (fixed window, `LOGIN_WINDOW_MINUTES`), *before* the password is checked, so parallel
  bursts cannot slip through. Above `LOGIN_EMAIL_MAX_ATTEMPTS` or `LOGIN_IP_MAX_ATTEMPTS` the
  answer is 429. A success clears the email counter and gives the IP its attempt back.
  Changing the password while signed in throttles the current-password check the same way,
  under `password:<user_id>` with the per-email limit; a correct password clears it.
- Passwords are hashed with argon2id (library defaults), in a worker thread so the event loop
  is never blocked. A login for an unknown email still verifies a dummy hash, so timing does
  not reveal accounts.

## Alternatives considered

- **JWT access + refresh tokens**: a stolen token, or one of a deactivated user, stays valid
  until it expires; fixing that needs a server-side denylist, which is a session store anyway.
  One monolith with one database gains nothing from stateless tokens.
- **Sessions in Valkey**: volatile and not auditable; "list my sessions" wants durable rows,
  and Valkey has no shared client in the API yet.
- **Returning a bearer token in the login body**: JavaScript would hold the token (XSS can
  steal it) and the web app would need to store and attach it; the httpOnly cookie avoids
  both.
- **Keeping a development token**: two authentication paths, a back door if production is
  misconfigured, and a special case for permissions; development signs in like production.

## Consequences

- Every admin request costs one indexed lookup (plus a write at most once a minute per
  session). Fine for a back office; revisit with a cache only if it shows in profiles.
- Signing out, changing a password and closing sessions take effect on the next request.
- Cross-site calls cannot carry the session (`SameSite=Strict`); a separate admin origin or a
  mobile client would need a new decision (CORS with credentials, or a token transport).
- Expired sessions and old throttle rows accumulate until a purge job exists; they are
  ignored meanwhile.
- Behind a reverse proxy the client IP must come from the proxy (`--proxy-headers`), or every
  login shares one IP counter; the deployment plan configures it.
