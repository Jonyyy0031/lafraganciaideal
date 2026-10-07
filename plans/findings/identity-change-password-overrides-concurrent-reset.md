---
status: deferred # open → deferred | planned | resolved | discarded (the user decides)
module: identity
found: 2026-10-06
---

# A password change in flight can override a password reset that commits meanwhile

## Found while

Reviewing identity-access/002, round 3 (after repair round 2 touched `ChangePassword`).

## What

`ChangePassword`
(`apps/api/src/fragancia_api/modules/identity/application/commands/change_password.py`)
verifies the current password against the user loaded in its first transaction (line 72),
hashes the new one outside any transaction (line 75), and in its final transaction re-reads the
user with `get_for_update` (line 81). It refuses only a missing or inactive user. It does not
compare the locked row's `password_hash` with the one it verified, and it does not check that
the caller's session is still open. `LogIn` does compare the hash in its final transaction
(`log_in.py`, `open_session`).

## Why it matters

Someone who knows the current password and holds a session (for example an attacker with a
stolen password) sends `PUT /admin/auth/password`. While it is hashing (hundreds of ms), the
real user confirms an emailed reset (`POST /auth/password-reset/confirm`): the new password is
saved and every session is revoked, including the attacker's. The attacker's `work()` then
locks the row, sees an active user, and writes its own hash. The reset is silently undone and
the attacker knows the password again. The window is narrow and needs timing; no data is
corrupted. This predates plan 002 (plan 001 saved the stale object), and plan 002's repair 2
was scoped by the user to the inactive-user check only.

## Suggested next step

A fast-lane fix: in `ChangePassword.work()`, also refuse when
`locked.password_hash != user.password_hash` (the same check `LogIn` makes), plus a regression
test. Or accept and close.

## Decision

2026-10-06: the user wants this planned after identity-access/002 closes. It stays `deferred`
until that plan exists (`planned` requires an existing plan).
