# identity-access — Back-office accounts, login and sessions

<!-- Initiative index. Rules: docs/harness/conventions/plans.md → "The initiative README".
     Status is NOT tracked here: run `uv run just plans-status identity-access`. -->

## Goal

Only known people can use the back office. Each person (the shop owner and the staff they
invite) signs in with email and password and gets a revocable session. Every admin request then
runs as a real user whose role (owner or staff) decides what they may do. The development-only
static token is removed: development signs in the same way production does. Customers keep buying as
guests; customer accounts are a later initiative.

## Plans

| Plan | Title                                              | Depends on | Purpose                                                                                       |
| ---- | -------------------------------------------------- | ---------- | --------------------------------------------------------------------------------------------- |
| 001  | Identity module: users, login, sessions and roles  | —          | `User` + roles, argon2id, opaque sessions in Postgres, login throttle, my account endpoints, `just create-owner`, ADR 0009 |
| 002  | Account management by email (TBD)                  | 001        | Owner invites staff, password reset by email link, deactivate a user; emails through the worker |

## Dependency notes

002 needs 001's `User`, sessions and roles: inviting, resetting and deactivating all act on
them, and only the owner may invite or deactivate. Until 002, every account is created with
`just create-owner` on the server.

## Decisions with the user

1. (2026-10-03) Identity comes before the perfume catalog, so the catalog's admin endpoints are
   protected by real accounts from the start.
2. (2026-10-03) Only back-office users sign in for now. Customers check out as guests; customer
   accounts are a later initiative.
3. (2026-10-03) Two roles: **owner** (everything, including managing users and settings) and
   **staff** (catalog, stock and orders, but not users). Permissions are atomic strings inside,
   so roles can be split further later without rewriting enforcement.
4. (2026-10-03) The first owner is created from the terminal (`just create-owner`). After that
   the owner invites staff by email (activation link, sent by the worker; Mailpit in
   development) — plan 002.
5. (2026-10-03) Besides login/logout, this initiative includes: password reset by email,
   change password while signed in, list and close my own sessions, and deactivating a user.
6. (2026-10-03) Two plans, grouped as small as reasonable (001: module, login, sessions,
   roles, create-owner, change password, my sessions; 002: invitations, reset, deactivation).
7. (2026-10-03) The development token `ADMIN_DEV_TOKEN` is **removed in plan 001**: one
   authentication path (the session cookie), no back door if `APP_ENV` is ever misconfigured,
   no actions recorded under a non-user, no special case for permissions in plan 002.
8. (2026-10-03) Review of plan 001, finding L2: wrong current passwords in
   `PUT /admin/auth/password` are throttled inside plan 001, reusing the login throttle with
   key `password:<user_id>` and the per-email limit; above it → 429
   `IDENTITY_TOO_MANY_ATTEMPTS`. M1, L1, L3 and L4 are repaired in plan 001 too.

## Delivered

- **001** (2026-10-04): `identity` module with owner and staff users (argon2id, hashing off the
  event loop); opaque sessions in `identity.sessions` sent as an httpOnly `SameSite=Strict`
  cookie; login and password-change throttle in PostgreSQL; `POST /auth/login` and
  `/admin/auth/{logout,me,password,sessions}`; `just create-owner`; `ADMIN_DEV_TOKEN` removed;
  ADR 0009. Two review rounds; verified 20/21 (Scalar in the browser not verified).

## Considered and discarded

- **JWT access + refresh tokens**: a stolen or deactivated user's token stays valid until it
  expires, and fixing that needs a server-side denylist — a session store anyway. One monolith
  with one database gains nothing from stateless tokens (same conclusion as web-rh).
- **Sessions in Valkey**: volatile and not auditable; listing my devices wants durable rows.
- **Customer accounts now**: more plans before the catalog, and guest checkout covers the first
  release (decision 2).
- **A single admin role**: the user chose owner + staff (decision 3).
