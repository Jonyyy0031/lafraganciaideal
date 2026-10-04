---
status: done
module: identity
min_implementer: high
depends_on: []
---

# 001 — Identity module: users, login, sessions and roles

## Context

**Today.** Admin access is a development stand-in:

- `require_admin` reads `Authorization: Bearer <token>` (`apps/api/src/fragancia_api/shared/http/access.py:17-33`)
  and asks the `ActorResolver` port (`shared/application/actor.py:13-16`). That port returns an
  `Actor(id, is_admin)` (`actor.py:5-10`).
- The only adapter is `DevTokenActorResolver`. It compares the token against `ADMIN_DEV_TOKEN`
  (`shared/infrastructure/dev_token_actor_resolver.py:6-19`).
- Settings refuse that variable in production (`config.py:56-62`).
- The container builds it into `Platform.actors` and registers it as the `ActorResolver` service
  (`container.py:51-63`, `shared/module.py:17-25`).
- Modules receive only `Platform` (`shared/module.py:16-35`). No module gets settings.
- `docs/modules.json` lists `identity` as `planned`: "Admin authentication and sessions;
  customer accounts later".
- `docs/architecture.md:59` says "Admin login with opaque sessions; customers check out as
  guests in the first release".

**What this plan builds** (README decisions 1–3, 6, 7):

- A new `identity` module with `User` (owner or staff) and server-side opaque sessions.
- A login throttle, the "my account" endpoints, and `just create-owner` for the first account.
- A real `ActorResolver`: the session token arrives in an httpOnly cookie, and the resolver
  turns it into the user.
- The development token is removed (decision 7), along with every place that uses it:
  - `DevTokenActorResolver`, `Settings.admin_dev_token` and its production check.
  - `Platform.actors`, the Bearer scheme and the Scalar auth preset
    (`shared/http/reference.py:20-21`).
  - The test helpers `ADMIN_TOKEN` / `ADMIN_HEADERS` (`tests/support.py:12-24`).
  - The docs that tell agents to use it: `docs/harness/roles/verifier.md:20-22`,
    `docs/harness/HARNESS.md:106`, `docs/harness/conventions/testing.md:51` and
    `apps/api/README.md:14-15,67-69`.

**Approach and why.**

- **Sessions.** Option A: opaque random token, only its SHA-256 stored in
  `identity.sessions`, looked up on every admin request. Option B: JWT. Option A is chosen
  (README "Considered and discarded"; ADR 0009 written in step 10). It allows immediate
  revocation, which closing a session, changing a password and deactivating a user (plan 002)
  all need.
- **Transport.** An httpOnly, `SameSite=Strict` cookie, with `Secure` in production. This is
  what the Angular SSR admin panel (phase 3) needs: JavaScript never sees the token, and
  `Strict` blocks cross-site requests from carrying it. The cookie path is `/api/v1`, so it
  also reaches `/api/v1/docs` (Scalar) on the same origin.
- **Cookie is the only transport.** The Bearer header is no longer accepted, and login never
  returns the token in the body. Scalar works without configuration: calling
  `POST /auth/login` from it stores the cookie, and the browser sends it with later
  same-origin requests.
- A 401 no longer carries `WWW-Authenticate: Bearer` (`shared/http/errors.py:98-101`). A
  cookie session has no registered HTTP auth scheme, and advertising Bearer would be false.
- **Throttle in PostgreSQL**, as one atomic `INSERT … ON CONFLICT DO UPDATE … RETURNING` per
  key (fixed window). It counts the attempt *before* the password is checked. web-rh's first
  version counted after the check, and parallel bursts slipped through (its decision 11). This
  needs no new client wiring: Valkey has no shared client today, only `ValkeyHealth`
  (`shared/infrastructure/valkey.py:4-14`).
- **Expected auth failures are values.** Two new kernel categories, `UnauthenticatedError`
  (401) and `RateLimitedError` (429), added next to the existing four
  (`shared/kernel/errors.py:23-40`, mapped in `shared/http/errors.py:50-64`).

**Defaults chosen by the architect** (not business rules; every one is an env var so it can
change without code, and the user confirms them by approving this plan):

| Setting | Default |
| --- | --- |
| Session idle timeout | 120 minutes |
| Session absolute lifetime | 12 hours |
| Login throttle per email | 5 attempts per 15 minutes |
| Login throttle per IP | 50 attempts per 15 minutes; successful logins give the IP its attempt back |
| Password length | 12–128 characters |

**Imitated files.** Copy them by name:

| New | Copies |
| --- | --- |
| Value objects and aggregate | `modules/catalog/domain/brand.py:30-70` |
| Errors | `modules/catalog/domain/errors.py:6-15` |
| Repository port | `modules/catalog/domain/repositories.py:8-15` |
| Commands | `modules/catalog/application/commands/create_brand.py:12-44` |
| Queries port | `modules/catalog/application/ports.py:6-15` |
| Tables, SQL adapters, unique-violation mapping | `modules/catalog/infrastructure/tables.py`, `sql_brand_repository.py:24-47`, `sql_brand_queries.py:10-37` |
| In-memory fakes | `modules/catalog/infrastructure/in_memory.py` |
| Router | `modules/catalog/http/router.py:19-55` |
| Wiring | `modules/catalog/module.py:15-30` |
| Migration | `apps/api/migrations/versions/0002_catalog_brands.py` |

## Out of scope

- Invitations, password reset by email, deactivating users, and listing or editing other users.
  These are plan 002. Until then the only way to create a user is `just create-owner`.
- **Enforcing permissions on routes.** A `require_permission` dependency is not part of this
  plan. Both roles may call every existing admin route. `User` carries its permissions and
  `GET /admin/auth/me` shows them; plan 002 adds the first permission-gated routes (users).
- Customer accounts and storefront login (README decision 2).
- **Cleaning `ADMIN_DEV_TOKEN` out of developers' existing `apps/api/.env` files.** Settings
  ignore unknown keys (`config.py:22`, `extra="ignore"`), so a leftover line is harmless.
  Bootstrap never edits existing values; the PR tells developers they may delete the line.
- Purging expired sessions and old throttle rows (a later cron). Expired rows are simply
  ignored.
- Reverse-proxy client IPs (`X-Forwarded-For`, uvicorn `--proxy-headers`). This plan uses
  `request.client.host`; the deployment plan configures the proxy.
- CORS with credentials, the Angular dev proxy and login screens (phase 3).
- Rehashing passwords when argon2 parameters change; 2FA; "remember me".
- Re-checking a login that is in flight while its user is deactivated (web-rh's plan 006
  problem). This becomes relevant only when plan 002 adds deactivation.

## Dependencies

None

## Steps

All module paths below are under `apps/api/src/fragancia_api/modules/identity/`. Every
`Files:` line still names the full path.

1. **Register the module, dependency and settings**
   - Files: `docs/modules.json` (modify), `apps/api/pyproject.toml` (modify), `uv.lock` (modify),
     `apps/api/src/fragancia_api/config.py` (modify), `apps/api/.env.example` (modify)
   - Do:
     - **Module registry:** set `identity` → `"status": "active"` in `docs/modules.json`.
       Keep the description and use an append-only edit.
     - **Dependency:** run `uv add --package fragancia-api argon2-cffi` from the repo root.
       Never edit `uv.lock` by hand.
     - **Settings:** add these fields to `Settings` (`config.py:51-56`), each with
       `Field(ge=1)`:

       | Field | Default |
       | --- | --- |
       | `session_idle_minutes` | 120 |
       | `session_max_hours` | 12 |
       | `login_email_max_attempts` | 5 |
       | `login_ip_max_attempts` | 50 |
       | `login_window_minutes` | 15 |

     - **`.env.example`:** add the same five variables in uppercase with these defaults, under
       a comment `# Back-office sessions and login throttle (development values; revisit
       before production).`
     - **Remove the dev token from settings:**
       - In `Settings`, delete `admin_dev_token` and the `_check_production` validator
         (`config.py:56-62`), plus the `SecretStr` import if it becomes unused.
       - In `.env.example`, delete the `ADMIN_DEV_TOKEN` block (lines 16-18).
   - Observable result:
     - `uv run just plans-lint` accepts `module: identity`.
     - `Settings()` loads with the defaults.
     - `make_settings(session_idle_minutes=0)` raises `ValidationError`.
     - `make_settings(app_env="production")` is valid.

2. **Kernel categories for 401 and 429, the session in the actor, cookie-only access**
   - Files: `apps/api/src/fragancia_api/shared/kernel/errors.py` (modify),
     `apps/api/src/fragancia_api/shared/kernel/__init__.py` (modify),
     `apps/api/src/fragancia_api/shared/http/errors.py` (modify),
     `apps/api/src/fragancia_api/shared/application/actor.py` (modify),
     `apps/api/src/fragancia_api/shared/http/access.py` (modify),
     `apps/api/src/fragancia_api/shared/http/__init__.py` (modify),
     `apps/api/src/fragancia_api/shared/http/reference.py` (modify),
     `apps/api/src/fragancia_api/shared/infrastructure/dev_token_actor_resolver.py` (delete)
   - Do:
     - **Kernel errors.** Add two categories after `BusinessRuleViolationError`, with
       docstrings in the same style, and export both from `shared/kernel/__init__.py`:
       - `UnauthenticatedError`: "The caller could not be identified, e.g. wrong credentials
         (HTTP 401)."
       - `RateLimitedError`: "Too many attempts; try again later (HTTP 429)."
     - **HTTP mapping.**
       - In `_STATUS_BY_CATEGORY` (`errors.py:50-55`) add `(UnauthenticatedError, 401)` and
         `(RateLimitedError, 429)`.
       - Update the module docstring line 3.
       - In `authentication_required` (`errors.py:98-101`), return the error response
         without the `WWW-Authenticate` header.
     - **Actor.** Add `session_id: UUID | None = None` to `Actor` (`actor.py:5-10`). It stays
       frozen. The identity resolver always sets it; `None` exists only for test resolvers.
     - **Access.** In `access.py`:
       - Replace `HTTPBearer` with `SESSION_COOKIE = "fragancia_session"` and
         `_cookie = APIKeyCookie(name=SESSION_COOKIE, auto_error=False, description="Session cookie set by POST /api/v1/auth/login")`.
       - `require_admin` takes `token: Annotated[str | None, Depends(_cookie)]`. If the token
         is `None` it raises `AuthenticationRequired`. The rest is unchanged
         (`access.py:20-33`).
       - Update the module docstring (lines 1-6): admin routes need the session cookie.
     - **Exports.** Export `SESSION_COOKIE` from `shared/http/__init__.py`.
     - **Scalar.** In `reference.py`, remove the `authentication` and `persist_auth`
       arguments (lines 20-21). The browser carries the cookie.
     - **Dev resolver.** Delete `dev_token_actor_resolver.py`.
   - Observable result:
     - `uv run just typecheck` lists only the now-broken imports of `DevTokenActorResolver`
       and `admin_dev_token`. Steps 3 and 11 fix them.
     - Admin operations in OpenAPI declare only `APIKeyCookie`.

3. **Platform carries the settings**
   - Files: `apps/api/src/fragancia_api/shared/module.py` (modify),
     `apps/api/src/fragancia_api/container.py` (modify)
   - Do:
     - **`Platform`.** In the `Platform` dataclass (`shared/module.py:17-25`), replace the
       field `actors: ActorResolver` with `settings: Settings` (import from
       `fragancia_api.config`) and document it. Drop the now-unused `ActorResolver` import.
     - **`build_container`.**
       - Pass `settings=settings` when building `Platform`.
       - Delete the dev-token lines: the import (`container.py:19`), `admin_token`
         (`container.py:51`), `actors=` (`container.py:57`), and
         `services.add(ActorResolver, platform.actors)` (`container.py:63`).
       - The identity module now registers the `ActorResolver` (step 9).
     - **Missing resolver.** After the module loop, raise
       `RuntimeError("No module registered the ActorResolver")` if `ActorResolver not in services`.
   - Observable result: `uv run just arch` stays green; shared modules may import `config`,
     and only `container`/`main` are forbidden (`.importlinter` composition-root contract).

4. **Domain**
   - Files: `apps/api/src/fragancia_api/modules/identity/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/identity/domain/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/identity/domain/errors.py` (create),
     `apps/api/src/fragancia_api/modules/identity/domain/user.py` (create),
     `apps/api/src/fragancia_api/modules/identity/domain/session.py` (create),
     `apps/api/src/fragancia_api/modules/identity/domain/repositories.py` (create)
   - Do:
     - **`__init__.py`** docstring: "Identity: back-office users, roles and sessions. Public
       API for other modules: nothing yet."
     - **`errors.py`** — frozen dataclasses with `code` and `message` defaults, like
       catalog's:

       | Error | Category | Code | Message |
       | --- | --- | --- | --- |
       | `EmailInvalid` | Invalid value | `IDENTITY_EMAIL_INVALID` | "Enter a valid email address" |
       | `NameInvalid` | Invalid value | `IDENTITY_NAME_INVALID` | "A name needs 2 to 80 characters" |
       | `PasswordTooWeak` | Invalid value | `IDENTITY_PASSWORD_TOO_WEAK` | "A password needs 12 to 128 characters" |
       | `CurrentPasswordWrong` | Invalid value | `IDENTITY_CURRENT_PASSWORD_WRONG` | "The current password is not correct" |
       | `EmailTaken` | Conflict | `IDENTITY_EMAIL_TAKEN` | "A user with this email already exists" |
       | `InvalidCredentials` | Unauthenticated | `IDENTITY_INVALID_CREDENTIALS` | "Email or password is incorrect" |
       | `TooManyAttempts` | Rate limited | `IDENTITY_TOO_MANY_ATTEMPTS` | "Too many sign-in attempts; try again later" |
       | `SessionNotFound` | Not found | `IDENTITY_SESSION_NOT_FOUND` | "Session not found" |

     - **`user.py`:**
       - **`Email`** value object with `create(raw)`. Strip and lowercase. Valid when it is
         at most 254 characters, has no whitespace, has exactly one `@`, a non-empty local
         part, and a domain that contains a `.` that is neither its first nor its last
         character. Otherwise `Err(EmailInvalid)`.
       - **`DisplayName`**: trim, collapse inner whitespace, 2–80 characters, else
         `Err(NameInvalid(details={"min": 2, "max": 80}))`.
       - **`PlainPassword`**: 12–128 characters counted with `len()` and kept as given (no
         trimming), else `Err(PasswordTooWeak(details={"min": 12, "max": 128}))`. Its
         `__repr__` returns `"PlainPassword(***)"`.
       - **`Role`** is a `StrEnum` with `OWNER = "owner"` and `STAFF = "staff"`.
       - **Permissions.** Constants `CATALOG_MANAGE = "catalog:manage"` and
         `USERS_MANAGE = "users:manage"`. `ROLE_PERMISSIONS` maps OWNER to both and STAFF to
         `{CATALOG_MANAGE}`.
       - **`User(AggregateRoot)`** has the fields `id`, `email: Email`, `name: DisplayName`,
         `role: Role`, `password_hash: str`, `is_active: bool`, `created_at`,
         `password_changed_at`. It has these members:
         - `User.create(email, name, role, password_hash, *, now)`: active, both timestamps
           `now`.
         - `permissions` property: `ROLE_PERMISSIONS[role]`.
         - `change_password(new_hash, *, now)`.
       - No domain events in this plan.
     - **`session.py`:**
       - `Session` (plain class) with the fields `id`, `user_id`, `token_hash`, `created_at`,
         `last_seen_at`, `expires_at`, `revoked_at: datetime | None`,
         `user_agent: str | None`, `ip: str | None`.
       - `Session.open(user_id, token_hash, *, now, max_age: timedelta, user_agent, ip)`
         sets `expires_at = now + max_age` and truncates `user_agent` to 255 characters.
       - `is_valid(now, idle: timedelta)`: not revoked, `now < expires_at`, and
         `now < last_seen_at + idle`.
       - `revoke(now)` is idempotent.
       - `touch(now)` sets `last_seen_at`.
     - **`repositories.py`:**
       - **`UserRepository`**: `get(user_id) -> User | None`,
         `get_by_email(email: Email) -> User | None`,
         `add(user) -> Result[None, EmailTaken]` (race-safe through the unique constraint),
         `save(user)` (updates `password_hash` and `password_changed_at`).
       - **`SessionRepository`**: `add(session)`, `get(session_id)`,
         `get_by_token_hash(token_hash)`, `save(session)` (updates `last_seen_at` and
         `revoked_at`), and `revoke_all_for_user(user_id, *, except_id: UUID | None, now)`.
         The last one sets `revoked_at` on every non-revoked session of the user except
         `except_id`.
   - Observable result: domain imports only the kernel and the standard library
     (`uv run just arch`).

5. **Contracts**
   - Files: `apps/api/src/fragancia_api/modules/identity/contracts.py` (create)
   - Do: Pydantic models. Bound sizes only; the rules live in the domain.

     | Model | Fields |
     | --- | --- |
     | `LoginRequest` | `email: str = Field(max_length=320)`, `password: str = Field(max_length=1024)` |
     | `ChangePasswordRequest` | `current_password: str = Field(max_length=1024)`, `new_password: str = Field(max_length=1024)` |
     | `AdminMe` | `id: UUID`, `email: str`, `name: str`, `role: Literal["owner", "staff"]`, `permissions: list[str]` (sorted) |
     | `LoginResponse` | `user: AdminMe`, `expires_at: datetime` |
     | `AdminSession` | `id`, `created_at`, `last_seen_at`, `expires_at`, `user_agent: str \| None`, `ip: str \| None`, `current: bool` |

   - Observable result: the schemas appear in OpenAPI after step 9.

6. **Application**
   - Files: `apps/api/src/fragancia_api/modules/identity/application/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/identity/application/ports.py` (create),
     `apps/api/src/fragancia_api/modules/identity/application/policy.py` (create),
     `apps/api/src/fragancia_api/modules/identity/application/commands/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/identity/application/commands/create_user.py` (create),
     `apps/api/src/fragancia_api/modules/identity/application/commands/log_in.py` (create),
     `apps/api/src/fragancia_api/modules/identity/application/commands/log_out.py` (create),
     `apps/api/src/fragancia_api/modules/identity/application/commands/change_password.py` (create),
     `apps/api/src/fragancia_api/modules/identity/application/commands/revoke_sessions.py` (create),
     `apps/api/src/fragancia_api/modules/identity/application/commands/resolve_session_actor.py` (create),
     `apps/api/src/fragancia_api/modules/identity/application/queries/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/identity/application/queries/my_account.py` (create)
   - Do:
     - **`policy.py`.** Frozen dataclass `AuthPolicy` with the fields `session_idle`,
       `session_max_age`, `throttle_window` (all `timedelta`), `email_max_attempts: int` and
       `ip_max_attempts: int`. Add `TOUCH_INTERVAL = timedelta(seconds=60)`.
     - **`ports.py`:**
       - **`PasswordHasher`**: `hash(password: str) -> str`,
         `verify(password_hash: str, password: str) -> bool` (never raises on mismatch), and
         `dummy_hash: str`. The dummy hash is checked when the user does not exist, so
         timing does not reveal accounts.
       - **`SessionTokens`**: `new() -> str` (URL-safe random) and
         `digest(token: str) -> str` (hex SHA-256).
       - **`LoginThrottle`**:
         - `hit(key, *, now, window) -> int`: atomically count one attempt in the key's
           fixed window (a new window starts when the previous one is over) and return the
           count.
         - `clear(key)`.
         - `give_back(key)`: decrement, never below 0.
       - **`AccountQueries`**:
         - `me(user_id) -> AdminMe | None`.
         - `sessions(user_id, *, current_session_id, now, idle) -> list[AdminSession]`:
           valid sessions only, newest `last_seen_at` first.
     - **Commands.** Each one follows `create_brand.py:12-44`: `execute` returns a `Result`,
       and writes happen inside `transactions.run`.
     - **`CreateUser.execute(email, name, password, role)`** returns
       `Result[UUID, DomainError]`. It validates the three value objects (first error wins in
       that order), returns `EmailTaken` if `get_by_email` finds the user, then hashes and
       `add`s. Hashing happens before `transactions.run` (argon2 is slow).
     - **`LogIn.execute(email, password, *, ip, user_agent)`** returns
       `Result[LoginResult, DomainError]`. `LoginResult` is a frozen dataclass with `token`,
       `expires_at` and `user_id`. In order:
       1. `normalized = email.strip().lower()`. The keys are `"email:" + normalized` and
          `"ip:" + (ip or "unknown")`.
       2. In one `run`: `hit` both keys and return `Ok((email_count, ip_count))`. The work
          always returns `Ok`, so the counts commit even when the attempt is rejected.
          After the `run`, return `Err(TooManyAttempts())` if the email count is above
          `email_max_attempts` or the IP count is above `ip_max_attempts`.
       3. In one `run`: `get_by_email`, using `Email.create`. A malformed email means no
          user.
       4. Outside any transaction: `verify` against the user's hash, or `dummy_hash` when
          there is no user.
       5. On a missing user, an inactive user or a wrong password: `Err(InvalidCredentials())`.
          The attempt stays counted.
       6. On success, in one `run`: `clear` the email key, `give_back` the IP key,
          `Session.open(…)` with the token's digest, and `add`. Return `Ok(LoginResult)`.
     - **`LogOut.execute(session_id)`**: load the session, `revoke(now)` and `save`.
       Always `Ok(None)`.
     - **`ChangePassword.execute(user_id, session_id, current, new)`**:
       - Validate `new` with `PlainPassword` first.
       - Load the user and `verify(current)`, else `Err(CurrentPasswordWrong())`.
       - Hash `new`, then in one `run`: `change_password` + `save`, and
         `revoke_all_for_user(user_id, except_id=session_id, now)`.
     - **`RevokeSession.execute(user_id, session_id)`**: `Err(SessionNotFound())` unless the
       session exists, belongs to `user_id` and is not revoked. Otherwise revoke and save.
     - **`RevokeOtherSessions.execute(user_id, current_session_id)`**:
       `revoke_all_for_user(except_id=current_session_id)`.
     - **`ResolveSessionActor`** implements the shared `ActorResolver` port.
       - Its constructor takes `sessions`, `users`, `tokens`, `transactions`, `clock` and
         `policy`.
       - `resolve(token)`:
         1. In one `run`, look up `get_by_token_hash(tokens.digest(token))`. Return `None`
            if there is no session or it is not `is_valid(now, policy.session_idle)`.
         2. Load the user and return `None` unless they are active.
         3. If `now - last_seen_at >= TOUCH_INTERVAL`, `touch` and `save` the session.
         4. Return `Actor(id=str(user.id), is_admin=True, session_id=session.id)`.
     - **Queries.** `GetMyAccount` and `ListMySessions` are thin wrappers over
       `AccountQueries`, like `list_brands.py:5-22`.
   - Observable result: application imports no FastAPI or SQLAlchemy (`uv run just arch`).

7. **Infrastructure and migration**
   - Files: `apps/api/src/fragancia_api/modules/identity/infrastructure/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/identity/infrastructure/tables.py` (create),
     `apps/api/src/fragancia_api/modules/identity/infrastructure/sql_user_repository.py` (create),
     `apps/api/src/fragancia_api/modules/identity/infrastructure/sql_session_repository.py` (create),
     `apps/api/src/fragancia_api/modules/identity/infrastructure/sql_login_throttle.py` (create),
     `apps/api/src/fragancia_api/modules/identity/infrastructure/sql_account_queries.py` (create),
     `apps/api/src/fragancia_api/modules/identity/infrastructure/argon2_password_hasher.py` (create),
     `apps/api/src/fragancia_api/modules/identity/infrastructure/secure_session_tokens.py` (create),
     `apps/api/src/fragancia_api/modules/identity/infrastructure/in_memory.py` (create),
     `apps/api/migrations/versions/0003_identity_users_and_sessions.py` (create)
   - Do:
     - **Tables** in schema `identity` (`tables.py`, like catalog's). Constant names:
       `USER_EMAIL_UNIQUE = "uq_users_email"`.

       | Table | Columns |
       | --- | --- |
       | `users` | `id` uuid pk, `email` String(254) unique, `name` String(80), `role` String(20), `password_hash` String(255), `is_active` bool, `created_at` timestamptz, `password_changed_at` timestamptz |
       | `sessions` | `id` uuid pk, `user_id` uuid FK → `identity.users.id`, `token_hash` String(64) unique, `created_at`, `last_seen_at`, `expires_at` timestamptz, `revoked_at` timestamptz null, `user_agent` String(255) null, `ip` String(45) null |
       | `login_throttle` | `key` String(330) pk, `attempts` integer, `window_started_at` timestamptz |

       - `sessions` also gets an index on `user_id`. The foreign key stays inside the
         `identity` schema, which the module-boundary rule allows.
     - **SQL repositories.**
       - Explicit mappers.
       - Inserts inside a savepoint, mapping `uq_users_email` to `Err(EmailTaken())` like
         `sql_brand_repository.py:32-47`.
       - All of them use `Database.session`.
     - **`SqlLoginThrottle.hit`** is a single statement: `postgresql.insert(...)
       .on_conflict_do_update(...)`. On conflict it resets `attempts` to 1 and
       `window_started_at` to `now` when `window_started_at <= now - window`; otherwise it
       increments `attempts`. It ends with `.returning(attempts)`. `clear` deletes the row.
       `give_back` sets `attempts = greatest(attempts - 1, 0)`.
     - **`SqlAccountQueries`** uses `Database.reader()`, like `sql_brand_queries.py`. `me`
       derives `permissions` from `ROLE_PERMISSIONS`, sorted. `sessions` filters by user,
       `revoked_at IS NULL`, `expires_at > now` and `last_seen_at > now - idle`, and sets
       `current = (id == current_session_id)`.
     - **`Argon2PasswordHasher`** uses `argon2.PasswordHasher()` with the library defaults
       (argon2id). `verify` returns `False` on `VerifyMismatchError`, `VerificationError` and
       `InvalidHashError`. `dummy_hash` is computed once in `__init__` from a random
       password.
     - **`SecureSessionTokens`**: `secrets.token_urlsafe(32)` and
       `hashlib.sha256(token.encode()).hexdigest()`.
     - **`in_memory.py`.** Fakes for both repositories, the throttle and the queries. Also
       `PlainTextPasswordHasher` (hash = `"plain:" + password`, for unit tests only) and
       `SequentialSessionTokens` (`"token-1"`, `"token-2"`, …; digest = `"digest:" + token`).
     - **Migration.** Generate it with `uv run just db-revision "identity users and sessions"`,
       which produces `0003_…`. Rename the file to the name above if needed. Edit it by hand
       to `CREATE SCHEMA IF NOT EXISTS "identity"` first and `DROP SCHEMA` last in the
       downgrade, as in `0002_catalog_brands.py:19-36`.
   - Observable result:
     - `uv run just db-migrate` creates the three tables.
     - `cd apps/api && uv run alembic check` reports no differences.
     - Downgrade -1 then upgrade head works on the test database.

8. **HTTP**
   - Files: `apps/api/src/fragancia_api/modules/identity/http/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/identity/http/cookies.py` (create),
     `apps/api/src/fragancia_api/modules/identity/http/router.py` (create)
   - Do:
     - **`cookies.py`.** Frozen dataclass `SessionCookie` with `secure: bool` and
       `max_age_seconds: int`.
       - `set(response, token)` calls `response.set_cookie(SESSION_COOKIE, token,
         max_age=…, path="/api/v1", httponly=True, samesite="strict", secure=…)`.
       - `clear(response)` calls `response.delete_cookie(SESSION_COOKIE, path="/api/v1",
         httponly=True, samesite="strict", secure=…)`.
     - **Routers.** `public = public_router(prefix="/auth", tags=["identity"])` and
       `admin = admin_router(prefix="/auth", tags=["identity · admin"])`. That makes the
       admin paths `/api/v1/admin/auth/...`.
     - **Session-only routes.** A dependency `current_session(request) -> UUID` reads
       `request.state.actor.session_id`, set by `require_admin` (`access.py:32`). It raises
       `Forbidden` when that is `None`, which is defensive: only a test resolver can produce
       it. Every admin route here uses it.
     - **Routes.** Routers hold no logic, use `unwrap`, and declare their error responses
       like `router.py:31-42`.

       | Route | Function | Behavior |
       | --- | --- | --- |
       | `POST /auth/login` | `log_in` | Takes `request: Request` and `response: Response`. Calls `LogIn.execute(body.email, body.password, ip=request.client.host if request.client else None, user_agent=request.headers.get("user-agent"))`. On success it sets the cookie and returns `LoginResponse(user=me, expires_at=…)`, with `me` from `GetMyAccount`. Errors: 401 `IDENTITY_INVALID_CREDENTIALS`, 429 `IDENTITY_TOO_MANY_ATTEMPTS`, 422. |
       | `POST /admin/auth/logout` | `log_out` | 204. Revokes the session and clears the cookie. |
       | `GET /admin/auth/me` | `get_my_account` | Returns `AdminMe`. |
       | `PUT /admin/auth/password` | `change_my_password` | 204. Errors: 422 `IDENTITY_PASSWORD_TOO_WEAK` and `IDENTITY_CURRENT_PASSWORD_WRONG`. |
       | `GET /admin/auth/sessions` | `list_my_sessions` | Returns `list[AdminSession]`. |
       | `DELETE /admin/auth/sessions/{session_id}` | `close_my_session` | 204. Error: 404 `IDENTITY_SESSION_NOT_FOUND`. |
       | `DELETE /admin/auth/sessions` | `close_my_other_sessions` | 204. Closes every session except the current one. |

     - **Docstrings** state the codes, like the catalog routes. Operation ids are the
       function names and must stay unique.
   - Observable result: `routers = (public, admin)`; no logic in route bodies.

9. **Wiring, CLI and OpenAPI**
   - Files: `apps/api/src/fragancia_api/modules/identity/module.py` (create),
     `apps/api/src/fragancia_api/container.py` (modify), `apps/api/.importlinter` (modify),
     `apps/api/src/fragancia_api/main/cli.py` (create), `justfile` (modify),
     `apps/api/openapi.json` (modify)
   - Do:
     - **`module.py`.**
       - `register(platform, services)` builds `AuthPolicy` from `platform.settings` (minutes
         and hours become `timedelta`), plus the SQL adapters, `Argon2PasswordHasher()` and
         `SecureSessionTokens()`.
       - It registers `CreateUser`, `LogIn`, `LogOut`, `ChangePassword`, `RevokeSession`,
         `RevokeOtherSessions`, `GetMyAccount` and `ListMySessions`.
       - It registers `SessionCookie(secure=platform.settings.is_production,
         max_age_seconds=session_max_hours * 3600)`.
       - It registers `ActorResolver` → `ResolveSessionActor(...)`.
       - `module = AppModule(name="identity", register=register, routers=routers)`.
     - **Container.** `container.py`: `MODULES = (identity, catalog)`, so identity registers
       first.
     - **Import-linter.** Add `fragancia_api.modules.identity` to the `containers` of the
       `module-layers` contract (`.importlinter`).
     - **`main/cli.py`.** An `argparse` program with the subcommand
       `create-owner --email E --name N [--password-stdin]`.
       - Without `--password-stdin` it asks twice with `getpass` and exits 1 if they differ.
       - It builds `Settings()` and `build_container`, runs
         `services.get(CreateUser).execute(email, name, password, Role.OWNER)`, then closes
         the container.
       - `Ok`: print `✔ owner <email> created` and exit 0. `Err`: print
         `✘ <code>: <message>` to stderr and exit 1.
       - It never prints the password.
     - **justfile.** Recipe `create-owner *args`:
       `cd apps/api && uv run python -m fragancia_api.main.cli create-owner "$@"`, with the
       comment `# Create a back-office owner account (asks for the password)`.
     - **OpenAPI.** Run `uv run just openapi`.
   - Observable result:
     - `uv run just create-owner --email owner@example.test --name "Dueña Prueba"` creates
       a row in `identity.users` with role `owner`.
     - Running it again exits 1 with `IDENTITY_EMAIL_TAKEN`.
     - `apps/api/openapi.json` shows the seven operations.

10. **ADR and documentation**
    - Files: `docs/adr/0009-opaque-sessions-in-postgres.md` (create), `docs/adr/README.md` (modify),
      `docs/architecture.md` (modify), `apps/api/README.md` (modify),
      `docs/harness/roles/verifier.md` (modify), `docs/harness/HARNESS.md` (modify),
      `docs/harness/conventions/testing.md` (modify)
    - Do:
      - **ADR 0009** (template `docs/adr/0000-template.md`) covers:
        - Opaque tokens, with only their hash in `identity.sessions`.
        - The httpOnly `SameSite=Strict` cookie as the only transport, and the removed dev
          token (README decision 7).
        - The idle and absolute lifetimes.
        - The throttle counted before verification.
        - The alternatives: JWT, Valkey sessions, a returned bearer token, keeping a dev
          token.
      - **ADR index:** add a row to `docs/adr/README.md`.
      - **`docs/architecture.md`:**
        - Mark the `identity` row ✔ with "Back-office users (owner, staff), opaque sessions
          in a cookie, login throttle".
        - Line 127 becomes "→ admin_router dependency require_admin: session cookie →
          ActorResolver → 401 / 403".
        - Line 158 says "No or unknown session".
      - **`apps/api/README.md`:**
        - Lines 14-15: create an owner with `uv run just create-owner`, then sign in with
          `POST /api/v1/auth/login`.
        - Lines 67-69: in Scalar, call `log_in` first; the browser stores the cookie and
          sends it with the admin calls.
        - Add the 401 / 429 categories to the "Errors" bullet under "Rules of thumb".
      - **`docs/harness/roles/verifier.md` lines 20-22.** Admin routes need a session cookie.
        Create a synthetic owner once with
        `printf '%s\n' '<12+ char password>' | uv run just create-owner --email verifier@example.test --name Verifier --password-stdin`
        (an existing one exits 1 with `IDENTITY_EMAIL_TAKEN`, which is fine). Then log in
        with `curl -c <scratchpad>/cookies.txt` and pass `-b <scratchpad>/cookies.txt`. Never
        paste the cookie into the plan.
      - **`docs/harness/HARNESS.md` line 106.** The example variable becomes
        `DATABASE_URL`, `S3_SECRET_KEY`.
      - **`docs/harness/conventions/testing.md` line 51.** The helpers become
        `make_settings`, `ADMIN_HEADERS` (a session cookie for `TestActorResolver`),
        `assert_admin_routes_are_protected`.
      - **Adapters.** Run `uv run just harness-check`. Roles are referenced by path, so no
        adapter should drift (`scripts/harness/adapters.py:273,433`). If one does, stop and
        report it as a deviation.
    - Observable result:
      - `grep -rn "ADMIN_DEV_TOKEN\|DevTokenActorResolver" apps/api/src apps/api/tests apps/api/.env.example apps/api/README.md docs AGENTS.md CLAUDE.md`
        finds only ADR 0009. Never grep `apps/api/.env`: it is a secrets file.
      - The docs name only files and commands that exist.

11. **Existing tests follow the removal (implementer)**
    - Files: `apps/api/tests/support.py` (modify), `apps/api/tests/unit/test_http_platform.py` (modify),
      `apps/api/tests/unit/catalog/test_brand_http.py` (modify), `apps/api/tests/unit/test_container.py` (modify),
      `apps/api/tests/unit/test_settings.py` (modify), `apps/api/tests/unit/test_openapi.py` (modify)
    - Do: the minimum to keep these tests meaningful without the dev token. No new behavior
      tests here; those are the tester's job (step 12).
      - **`support.py`.**
        - Replace `ADMIN_TOKEN` / `ADMIN_HEADERS` (lines 12-13) with
          `ADMIN_SESSION = "test-admin-session"` and
          `ADMIN_HEADERS = {"Cookie": f"{SESSION_COOKIE}={ADMIN_SESSION}"}`.
        - Add `TestActorResolver`. It returns
          `Actor(id="test-admin", is_admin=True, session_id=UUID(int=1))` for
          `ADMIN_SESSION` and `None` otherwise.
        - Drop `"admin_dev_token"` from `make_settings` (line 21).
        - `assert_admin_routes_are_protected` (line 53) checks `{"APIKeyCookie": []}`.
      - **`test_http_platform.py`.**
        - `make_app` uses `TestActorResolver()`.
        - The unknown-credential test sends `Cookie: fragancia_session=wrong`.
        - The whoami test expects `{"id": "test-admin"}`.
        - The 401 test (around line 175) no longer asserts `WWW-Authenticate`.
        - `CustomerResolver` is called through a cookie.
        - `make_settings(app_env="production")` no longer passes `admin_dev_token`.
      - **`catalog/test_brand_http.py`.** Register `TestActorResolver()` instead of the dev
        resolver (lines 17, 23, 37). `ADMIN_HEADERS` keeps its name.
      - **`test_container.py`.** Delete `test_dev_token_resolver`. Assert that the registered
        `ActorResolver` is a `ResolveSessionActor`.
      - **`test_settings.py`.** Delete the two dev-token tests (lines 14-20). Add
        `test_production_settings_are_valid`
        (`make_settings(app_env="production").is_production`).
      - **`test_openapi.py`** (lines 63-65). The description mentions the session cookie,
        and `securitySchemes` has `APIKeyCookie` with `"in": "cookie"` and
        `"name": "fragancia_session"`.
    - Observable result: `uv run just check` is green, with the same number of tests minus the
      deleted dev-token ones.

12. **Tests (tester)**
    - Files: `apps/api/tests/unit/identity/` (create), `apps/api/tests/integration/identity/` (create),
      `apps/api/tests/support.py` (modify)
    - Do: derived by the tester from the layers below. `support.py` may gain helpers, such
      as building identity services with the in-memory fakes.
    - Observable result: `uv run just check` and `uv run just test-integration` are green.

## Acceptance criteria

Run against `uv run just api` with the development database migrated. The owner is created
with `uv run just create-owner --email owner@example.test --name "Dueña Prueba"`, using a
12+ character password.

- [ ] `POST /api/v1/auth/login` with the right credentials → 200 with
      `{user: {id, email: "owner@example.test", name, role: "owner", permissions:
      ["catalog:manage", "users:manage"]}, expires_at}`.
- [ ] The same response has a `Set-Cookie: fragancia_session=…` with `HttpOnly`,
      `SameSite=Strict` and `Path=/api/v1`, and no `Secure` in development. The body
      contains no token.
- [ ] `identity.sessions` holds a 64-hex `token_hash` that differs from the cookie value.
- [ ] With that cookie, `GET /api/v1/admin/auth/me` → 200 `AdminMe`.
      `GET /api/v1/admin/brands` → 200, so existing admin routes accept the session.
- [ ] Without a cookie, `GET /api/v1/admin/auth/me` and `GET /api/v1/admin/brands` → 401
      `AUTHENTICATION_REQUIRED`, with no `WWW-Authenticate` header.
- [ ] `Authorization: Bearer dev-admin-token` (the old dev token) → 401.
- [ ] A wrong password → 401 `IDENTITY_INVALID_CREDENTIALS`.
- [ ] An unknown email → the same 401 and the same body.
- [ ] A malformed email (`"no-at"`) → the same 401.
- [ ] After 5 failed logins for one email within 15 minutes, the 6th attempt → 429
      `IDENTITY_TOO_MANY_ATTEMPTS`, even with the right password.
- [ ] After that 429, another email from the same IP still gets 401/200, not 429.
- [ ] Two logins (two cookies): `GET /api/v1/admin/auth/sessions` lists 2, exactly one with
      `current: true`.
- [ ] `DELETE /api/v1/admin/auth/sessions/{other}` → 204, and the other cookie now gets 401.
- [ ] `DELETE` of a random uuid → 404 `IDENTITY_SESSION_NOT_FOUND`.
- [ ] `PUT /api/v1/admin/auth/password` with a wrong current password → 422
      `IDENTITY_CURRENT_PASSWORD_WRONG`.
- [ ] A new password of 11 characters → 422 `IDENTITY_PASSWORD_TOO_WEAK`.
- [ ] A valid password change → 204. The old password then fails at login, the new one
      works, and every other session of that user gets 401 while the current one keeps
      working.
- [ ] `POST /api/v1/admin/auth/logout` → 204, the cookie is cleared, and the same cookie
      afterwards gets 401.
- [ ] A session idle longer than `SESSION_IDLE_MINUTES` (verify by setting
      `SESSION_IDLE_MINUTES=1` and waiting) → 401.
- [ ] `just create-owner` with an existing email → exit 1 and `IDENTITY_EMAIL_TAKEN`; no
      password appears in the output.
- [ ] The API starts with an `apps/api/.env` that still has an `ADMIN_DEV_TOKEN` line. The
      key is ignored and does not grant access.
- [ ] Scalar at `/api/v1/docs`: `log_in` then `get_my_account` → 200, with no manual auth
      setup.
- [ ] `uv run just check` and `uv run just test-integration` are green, and
      `apps/api/openapi.json` is current.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | `Email`, `DisplayName` and `PlainPassword` rules and limits; role permissions; `Session.open`, `is_valid` (idle, absolute, revoked) and `revoke` |
| application | yes     | `CreateUser`, `LogIn` (throttle before verify, the dummy hash used for a missing user, email cleared and IP given back on success, 429 above the limits), `ChangePassword` (revokes others, keeps current), `RevokeSession` (other user's session is not found), `ResolveSessionActor` (invalid, expired or inactive → None, touch interval) |
| http        | yes     | status codes and error codes of the seven operations, cookie attributes, a Bearer header is ignored (401), an actor without a session on session-only routes → 403, every admin route protected (`assert_admin_routes_are_protected`) |
| integration | yes     | SQL repositories (unique email race → `EmailTaken`), atomic throttle under concurrent hits (N parallel `hit`s → counts 1..N), window reset, `give_back` floor, account queries filters, migration round trip |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

- **Step 7, migration — blocked, then resolved (2026-10-03).** `uv run just db-revision "identity users and sessions"` generated
  `apps/api/migrations/versions/0003_identity_users_and_sessions.py`. As the recipe predicts,
  autogenerate did not add `CREATE SCHEMA "identity"`. The `guard_files` hook then refused the
  hand edit ("Existing migration…", `.claude/hooks/guard_paths.py:77-78`), and `guard_bash`
  refused deleting it. The implementer did not work around the guard; it set
  `status: blocked` and stopped. While blocked:
  - `uv run just db-migrate` failed on `0003` and rolled back. The development database
    stayed at `0002`.
  - `uv run just check` was red only on `ruff check` (two E501 lines in the generated file).
  - Test database incident: the implementer ran `alembic -x test=true downgrade -1` while
    `fragancia_test` was still at `0002` (`0003` had failed). That took it to `0001`. It was
    restored with `alembic -x test=true upgrade 0002`. The development database was never
    downgraded.
  - Root cause: finding `plans/findings/platform-migration-guard-blocks-new-migration-review.md`.
  - **Resolution.** Commit `310fe2b` (`fix(harness)`, made by the main session) lets the guard
    edit a migration that git has never seen. The finding is resolved.
    - The main session installed the implementer's hand-reviewed `0003`. It is formatted like
      `0002`, starts with `CREATE SCHEMA IF NOT EXISTS "identity"` and ends its downgrade with
      `DROP SCHEMA`.
    - The main session then ran `db-migrate` on dev and test (both at `0003`), `alembic check`
      (no new operations), downgrade -1 / upgrade head on the test database, and ruff.
    - The implementer resumed the plan (`implementing`) and finished the step's checks; see
      the evidence below.
    - `plans-scope` also lists `.claude/hooks/guard_paths.py` and
      `scripts/harness/test_hooks.py`. They come from that harness commit on this branch, not
      from this plan's work.
- **Out of the file list (cosmetic, fixed forward):** `apps/api/src/fragancia_api/main/http.py`
  `DESCRIPTION`. Step 11 makes `test_openapi.py` assert that the description mentions the
  session cookie. The description lives in `main/http.py`, which no step lists, and it still
  said `Authorization: Bearer <token>`. It now names the `fragancia_session` cookie set by
  `POST /api/v1/auth/login`. `plans-scope` reports this one file.
- **Small choices the plan left open (no design impact):**
  - `CreateUser` runs `get_by_email` in its own `run`, hashes outside any transaction, then
    `add`s in a second `run`. The unique constraint still covers the race.
  - `ChangePassword` raises `LookupError` (an unexpected error, so 500) if the session's user
    does not exist. That cannot happen with `ResolveSessionActor`.
  - The router has a `current_user` dependency next to `current_session`. It raises
    `Forbidden` when `actor.id` is not a UUID (only test resolvers, e.g.
    `TestActorResolver`'s `"test-admin"`).
  - `GET /admin/auth/me` and login answer 401 if `GetMyAccount` returns `None`. This is
    defensive.
  - `access.py`'s 401 description now says "Missing or unknown session".
  - `PlainTextPasswordHasher` records `verified_hashes`, so tests can assert that the dummy
    hash was used.
  - `test_brand_http.py`'s wrong-credential case sends `Cookie: fragancia_session=nope`
    instead of a Bearer header.
- **Noticed, not changed:** `docs/harness/HARNESS.md` → "Module registry" still lists
  `identity` under **Planned**. Only HARNESS.md line 106 is in this plan's scope. Filed as
  finding `plans/findings/platform-harness-module-registry-stale.md`.

Evidence (2026-10-03):

- `alembic current` → `0003 (head)` on both databases.
- `printf '%s\n' '<synthetic>' | uv run just create-owner --email implementer@example.test --name "Implementer Prueba" --password-stdin`:
  - The first run prints "✔ owner implementer@example.test created". The row has role
    `owner`, `is_active` true and a `$argon2id$` hash.
  - The second run exits 1 with "✘ IDENTITY_EMAIL_TAKEN: A user with this email already
    exists". No password appears in either output.
  - The seeded row was removed afterwards: `SELECT COUNT(*)` gave 1, then
    `DELETE … WHERE email = 'implementer@example.test'` reported `DELETE 1`.
- `uv run just check` → green:
  - lint, typecheck, arch (7 kept) and plans-lint.
  - "24 adapters up to date".
  - "236 passed, 3 skipped, 15 deselected".
  - Harness tests: "684 passed".
  - compose config.
- `uv run just test-integration` → "15 passed".
- The step 10 grep finds only ADR 0009.
- `plans-scope`: the only file out of scope from this plan's own work is `main/http.py`
  (above).

### Repair round 1 (2026-10-03, after review round 1)

The main session returned the plan from `review` to `implementing` with the reason in
`## Review findings` and README decision 8. Every repair stays inside the plan's file list.
The earlier `## Test coverage` and `## Review findings` describe the code before this round.
The repaired code goes through testing, review and verify again. No regression tests were
added here: that is the tester's job.

- **M1, argon2 off the event loop.** `PasswordHasher.hash` and `verify` are now `async` in the
  port (`application/ports.py`). `Argon2PasswordHasher` runs both through
  `asyncio.to_thread`. The `dummy_hash` is still computed synchronously, once, when the module
  is wired at startup. `LogIn`, `ChangePassword` and `CreateUser` `await` the hasher.
  - `PlainTextPasswordHasher` (the fake) is async too. It gained a synchronous static
    `encode(password)` so that test setup and assertions do not need `await`.
  - Tests changed only for the signature: `conftest.py` and `test_create_user.py` /
    `test_change_password.py` use `hasher.encode(...)` instead of `hasher.hash(...)`.
    `test_security_adapters.py`'s five argon2 tests are now `async` and `await` the hasher.
- **L1, over-long normalized email.** `log_in.py` builds the email throttle key with
  `_email_key`. If the email is longer than `EMAIL_MAX_LENGTH` (254) after strip and lowercase,
  the key is `email-sha256:<hex of the normalized email>`. Such an email cannot belong to any
  account, so it still counts and is still throttled. The key is then 77 characters at most
  and always fits `login_throttle.key String(330)`. The `email-sha256:` prefix cannot collide
  with an `email:` key. Schema and contract are unchanged.
- **L2, throttled current-password check (decision 8).** `ChangePassword` now takes
  `throttle: LoginThrottle` and `policy: AuthPolicy`.
  - The order is: weak new password (422, nothing counted), then one transaction that `hit`s
    `password:<user_id>` and loads the user, then above `policy.email_max_attempts` → 429
    `IDENTITY_TOO_MANY_ATTEMPTS` with no verify, then a wrong current password → 422 (the
    attempt stays counted). On success the work transaction `clear`s the key.
  - `module.py` builds one `SqlLoginThrottle` and shares it between `LogIn` and
    `ChangePassword`. The unit `conftest.py` passes the in-memory throttle and the policy.
  - `PUT /admin/auth/password` declares 429 in `responses`, and its docstring names the 429.
    `uv run just openapi` regenerated `apps/api/openapi.json`: a 429 response and the new
    description on `change_my_password`.
  - The error reuses `TooManyAttempts`, whose message says "Too many sign-in attempts; try
    again later". The code is the one decision 8 names.
- **L3.** `apps/api/README.md` now says the admin-route test requires the session cookie (the
  `APIKeyCookie` security scheme), not the bearer scheme.
- **L4.** `docs/architecture.md` → "Errors" now lists rate limited (too many attempts) → 429.
- **Also updated (same canonical-docs rule, in step 10's list):** ADR 0009 describes the
  password-change throttle and says argon2 runs in a worker thread.

Evidence (2026-10-03):

- `uv run just check` → green. It runs lint, typecheck, arch, plans-lint ("7 plans, 3
  findings"), harness-check ("24 adapters up to date"), unit tests ("370 passed, 3 skipped,
  42 deselected"), harness tests ("684 passed") and compose config.
- `uv run just test-integration` → "41 passed, 1 skipped".
- `plans-scope` reports the same three out-of-scope files as before (two from `310fe2b`, plus
  `main/http.py`). Nothing new.

## Test coverage

Baseline (before any test): `uv run just check` green (236 passed, 3 skipped; harness 684) and
`uv run just test-integration` 15 passed. Closing: `uv run just check` green (370 passed, 3
skipped, 42 deselected; harness 684) and `uv run just test-integration` 41 passed, 1 skipped.
New: 134 unit tests (domain, application, http, adapters) and 26 integration tests plus 1
skip. No GAP found: every behavior the plan promises that the tester checked is in the code.

Files: `apps/api/tests/unit/identity/` (`conftest.py` builds every use case over the in-memory
fakes; `test_user_domain.py`, `test_session_domain.py`, `test_create_user.py`, `test_log_in.py`,
`test_change_password.py`, `test_sessions.py`, `test_resolve_session_actor.py`,
`test_security_adapters.py`, `test_auth_http.py`) and `apps/api/tests/integration/identity/`
(`conftest.py`, `test_sql_identity.py`). `tests/support.py` was not changed.

| Behavior | Source | Layer | Test | State |
| --- | --- | --- | --- | --- |
| Email is trimmed, lowercased; shape rules; 254 limit | `domain/user.py` `Email.create` | domain | `test_user_domain.py::test_email_is_trimmed_and_lowercased`, `::test_invalid_emails`, `::test_email_length_limit_is_254_inclusive` | CONFIRMED |
| Display name trim, collapse, 2-80 | `domain/user.py` `DisplayName.create` | domain | `test_user_domain.py::test_display_name_is_trimmed_and_inner_whitespace_collapsed`, `::test_invalid_display_names`, `::test_display_name_length_limits_are_inclusive` | CONFIRMED |
| Password 12-128 characters, kept as typed, `repr` hides it | `domain/user.py` `PlainPassword` | domain | `test_user_domain.py::test_password_limits_are_inclusive_and_it_is_kept_exactly_as_typed`, `::test_weak_passwords`, `::test_a_password_never_shows_in_its_repr` | CONFIRMED |
| Role permissions (owner both, staff catalog) and new users active | `domain/user.py` `ROLE_PERMISSIONS`, `User` | domain | `test_user_domain.py::test_the_owner_has_every_permission_and_staff_only_the_catalog`, `::test_new_users_are_active_with_both_timestamps_set_to_now`, `::test_changing_the_password_replaces_the_hash_and_stamps_the_time` | CONFIRMED |
| Session lifetime: absolute expiry, idle deadline, revoked, touch, idempotent revoke, user agent cut at 255 | `domain/session.py` | domain | `test_session_domain.py::*` | CONFIRMED |
| `CreateUser` validates in order email, name, password; conflict on taken email (case-insensitive); stores a hash | `create_user.py` | application | `test_create_user.py::*` | CONFIRMED |
| `LogIn` counts the attempt before verifying; the 6th is 429 and nothing is verified | `log_in.py` | application | `test_log_in.py::test_the_attempt_above_the_email_limit_is_rejected_before_the_password_is_checked`, `::test_the_last_attempt_within_the_email_limit_still_works` | CONFIRMED |
| `LogIn` uses the dummy hash for an unknown or malformed email; same error as a wrong password; inactive user refused | `log_in.py` | application | `test_log_in.py::test_an_unknown_email_gets_the_same_error_and_checks_the_dummy_hash`, `::test_a_malformed_email_...`, `::test_an_inactive_user_...`, `::test_a_wrong_password_...` | CONFIRMED |
| `LogIn` success clears the email key and gives the IP attempt back; IP limit per IP; window reset; unknown IP key | `log_in.py` | application | `test_log_in.py::test_a_successful_sign_in_clears_...`, `::test_the_ip_limit_...`, `::test_the_counters_start_over_...`, `::test_the_window_still_blocks_...`, `::test_an_unknown_ip_...`, `::test_after_a_successful_sign_in_the_email_has_its_full_allowance_again` | CONFIRMED |
| `ChangePassword` revokes other sessions and keeps the current one; wrong current and weak new rejected without side effects; unknown user is `LookupError` | `change_password.py` | application | `test_change_password.py::*` | CONFIRMED |
| `RevokeSession` (other user's, unknown and closed sessions are not found), `RevokeOtherSessions`, `LogOut` idempotent, `GetMyAccount`, `ListMySessions` | `revoke_sessions.py`, `log_out.py`, `my_account.py` | application | `test_sessions.py::*` | CONFIRMED |
| `ResolveSessionActor`: unknown, revoked, idle, expired, inactive user or missing user -> None; touch only after 60 s; activity extends idle | `resolve_session_actor.py` | application | `test_resolve_session_actor.py::*` | CONFIRMED |
| argon2id hash and verify (mismatch and unreadable hash return False), dummy hash, token and digest format | `argon2_password_hasher.py`, `secure_session_tokens.py` | application (adapters, no IO) | `test_security_adapters.py::*` | CONFIRMED |
| Login: 200 body, no token in body, cookie HttpOnly / SameSite=Strict / Path=/api/v1 / Max-Age, `Secure` only when configured | `router.py` `log_in`, `cookies.py` | http | `test_auth_http.py::test_login_returns_the_user_and_the_expiry`, `::test_login_never_puts_the_token_in_the_body`, `::test_login_sets_an_httponly_...`, `::test_the_cookie_is_secure_when_configured` | CONFIRMED |
| Login 401 (wrong password; unknown and malformed email identical), 429, payload validation 422 | `router.py` | http | `test_auth_http.py::test_a_wrong_password_...`, `::test_unknown_and_malformed_...`, `::test_the_attempt_after_the_email_limit_...`, `::test_a_blocked_email_does_not_block_...`, `::test_login_validates_the_payload_shape` | CONFIRMED |
| Cookie is the only transport: no cookie, unknown cookie or Bearer header (even with a real token) -> 401 `AUTHENTICATION_REQUIRED`, no `WWW-Authenticate` | `access.py`, `errors.py` | http | `test_auth_http.py::test_without_a_valid_session_cookie_admin_routes_are_401`, `::test_a_bearer_header_does_not_replace_...` | CONFIRMED |
| The seven operations exist; every admin route is protected | `router.py`, `assert_admin_routes_are_protected` | http | `test_auth_http.py::test_every_identity_admin_operation_is_declared_and_protected` (and the helper in the `client` fixture) | CONFIRMED |
| Logout 204 clears the cookie and kills the session; password change 204 / 422 codes / others closed; sessions list marks one current; close one 204/404/422; close others | `router.py` | http | `test_auth_http.py::test_logout_...`, `::test_changing_the_password_...`, `::test_a_wrong_current_password_is_422`, `::test_a_new_password_of_11_...`, `::test_lists_my_sessions_...`, `::test_closing_...` | CONFIRMED |
| Actor without a session (or whose id is not a user UUID) is 403 on session-only routes | `router.py` `current_session`, `current_user` | http | `test_auth_http.py::test_an_actor_without_a_session_is_403_...`, `::test_an_actor_whose_id_is_not_a_user_uuid_...` | CONFIRMED |
| User round trip, `save` of the password, unique email -> `EmailTaken` inside a savepoint, concurrent `CreateUser` yields one user, DB constraint | `sql_user_repository.py`, `tables.py` | integration | `test_sql_identity.py::test_a_user_round_trips_...`, `::test_saving_a_user_...`, `::test_a_duplicate_email_...`, `::test_concurrent_creation_...`, `::test_the_database_refuses_...` | CONFIRMED |
| Session round trip, `save`, `revoke_all_for_user` (except, none, already revoked), FK to users, unique token hash | `sql_session_repository.py`, `tables.py` | integration | `test_sql_identity.py::test_a_session_round_trips_...`, `::test_saving_a_session_...`, `::test_revoking_all_...`, `::test_a_session_needs_an_existing_user`, `::test_two_sessions_cannot_share_a_token_hash` | CONFIRMED |
| Throttle: counts per key, 10 parallel `hit`s give 1..10, window reset at the boundary, `clear`, `give_back` floor at 0, unknown key | `sql_login_throttle.py` | integration | `test_sql_identity.py::test_hits_in_one_window_count_up`, `::test_keys_count_independently`, `::test_parallel_hits_each_see_their_own_count`, `::test_a_hit_after_the_window_...`, `::test_clearing_...`, `::test_giving_back_...` | CONFIRMED |
| Account queries: permissions by role; sessions filtered by user, revoked, expired, idle (boundary exact) and ordered | `sql_account_queries.py` | integration | `test_sql_identity.py::test_me_derives_...`, `::test_my_sessions_filters_...`, `::test_a_session_idle_exactly_...` | CONFIRMED |
| Real stack (argon2 + SQL): sign in stores an argon2id hash and a 64-hex digest different from the token; the token resolves to the user; same failure for wrong password and unknown email; 6th attempt blocked; parallel wrong guesses cannot exceed the limit | `log_in.py`, `module.py` | integration | `test_sql_identity.py::test_a_created_user_signs_in_...`, `::test_a_wrong_password_and_an_unknown_email_...`, `::test_the_sixth_attempt_...`, `::test_parallel_wrong_guesses_...` | CONFIRMED |
| Migration round trip (downgrade -1 / upgrade head) | `migrations/versions/0003_identity_users_and_sessions.py` | integration | `test_sql_identity.py::test_migration_0003_downgrades_and_upgrades_cleanly` | NOT CONFIRMED (skipped: downgrading `fragancia_test` inside the suite would race the other tests; the main session ran it by hand, see Deviations) |
| `just create-owner` CLI (`main/cli.py`) | `main/cli.py` | none | not in this plan's required layers; exercised by hand by the implementer (Evidence) | NOT CONFIRMED by automated test |
| `Settings` bounds for the five new variables | `config.py` | none | superseded by repair round 1 below (now covered) | see below |

### Repair round 1 (2026-10-03): regression tests

The numbers and rows above describe the code before repair round 1; the section below covers the
repaired code. Baseline: `uv run just check` green, 370 unit passed (the previous closing).
Closing: `uv run just check` green (404 passed, 3 skipped, 47 deselected; harness 684) and
`uv run just test-integration` 46 passed, 1 skipped (the same migration skip). New: 34 unit and
5 integration tests. No GAP found. The earlier tests keep passing unchanged.

| Behavior | Source | Layer | Test | State |
| --- | --- | --- | --- | --- |
| M1: `hash` runs in a worker thread (not the loop's thread) | `argon2_password_hasher.py` `hash` | application (adapter, library call stubbed) | `unit/identity/test_argon2_off_the_loop.py::test_hash_runs_in_a_worker_thread` | CONFIRMED |
| M1: `verify` runs in a worker thread | `argon2_password_hasher.py` `verify` | application (adapter) | `test_argon2_off_the_loop.py::test_verify_runs_in_a_worker_thread` | CONFIRMED |
| M1: the dummy hash is still computed once, synchronously, at construction | `argon2_password_hasher.py` `__init__` | application (adapter) | `test_argon2_off_the_loop.py::test_the_dummy_hash_is_computed_once_when_the_hasher_is_built` | CONFIRMED |
| M1: the loop keeps ticking during a blocking verify (a blocked loop would allow at most one tick; the test needs 5) | `argon2_password_hasher.py` | application (adapter) | `test_argon2_off_the_loop.py::test_the_event_loop_keeps_running_while_a_slow_verify_is_in_progress` | CONFIRMED (timing-based, 0.3 s block, wide margin) |
| M1: the async port is awaited by the use cases | `log_in.py`, `change_password.py`, `create_user.py` | application | the existing `LogIn`, `ChangePassword` and `CreateUser` tests (they run over the async fake) and the real-stack integration tests | CONFIRMED |
| L1: an over-long normalized email is keyed `email-sha256:<hex>`, counted, 401 not 500, dummy hash checked | `log_in.py` `_email_key` | application | `unit/identity/test_log_in_long_email.py::test_an_email_longer_than_254_after_lowercasing_is_counted_under_a_hashed_key`, `::test_an_over_long_email_checks_the_dummy_hash` | CONFIRMED |
| L1: the hashed key is throttled (6th attempt 429), ignores casing and padding, and exactly 254 characters keeps the plain `email:` key | `log_in.py` | application | `test_log_in_long_email.py::test_an_over_long_email_is_throttled_like_any_other`, `::test_the_hashed_key_ignores_casing_and_padding`, `::test_an_email_of_exactly_254_characters_keeps_the_plain_key` | CONFIRMED |
| L1: `POST /auth/login` with `"İ" * 320` answers 401 `IDENTITY_INVALID_CREDENTIALS` | `router.py` `log_in` | http | `test_auth_http.py::test_an_email_that_lowercases_past_254_characters_is_401_not_500` | CONFIRMED |
| L1: the real table accepts the key (no truncation error) and holds one `email-sha256:` row with attempts 1; the 6th attempt is 429 | `log_in.py`, `tables.py` `key String(330)` | integration | `integration/identity/test_sql_identity.py::test_an_email_that_lowercases_past_254_characters_is_counted_and_not_a_500`, `::test_the_sixth_over_long_email_attempt_is_blocked_by_the_real_throttle` | CONFIRMED |
| L2: a wrong current password is counted under `password:<user_id>`, per user | `change_password.py` | application | `unit/identity/test_change_password_throttle.py::test_a_wrong_current_password_is_counted_under_the_users_key`, `::test_the_count_belongs_to_the_user_who_changes_the_password` | CONFIRMED |
| L2: above `email_max_attempts` is `IDENTITY_TOO_MANY_ATTEMPTS` with no verify, even with the right password; the last allowed attempt works; the window resets it | `change_password.py` | application | `test_change_password_throttle.py::test_the_attempt_above_the_limit_is_rejected_without_verifying_the_password`, `::test_the_last_attempt_within_the_limit_still_works`, `::test_the_counter_starts_over_after_the_window` | CONFIRMED |
| L2: a correct password clears the key; a weak new password is rejected before counting | `change_password.py` | application | `test_change_password_throttle.py::test_a_correct_password_clears_the_count`, `::test_a_weak_new_password_is_not_counted` | CONFIRMED |
| L2: `PUT /admin/auth/password` is 429 `IDENTITY_TOO_MANY_ATTEMPTS` above the limit, and the password stays the old one | `router.py` `change_my_password` | http | `test_auth_http.py::test_the_password_attempt_above_the_limit_is_429_even_with_the_right_password` | CONFIRMED |
| L2: with the real throttle: wrong attempts counted (limit + 1 after the block), blocked attempt changes nothing, correct password changes it and deletes the row, weak new password leaves no row | `change_password.py`, `sql_login_throttle.py` | integration | `test_sql_identity.py::test_wrong_current_passwords_are_counted_and_the_next_attempt_is_blocked`, `::test_a_correct_current_password_changes_it_and_clears_the_count`, `::test_a_weak_new_password_is_not_counted_by_the_real_throttle` | CONFIRMED |
| `Settings` bounds: each of the five variables refuses 0 and -1, accepts 1, and the defaults are 120 / 12 / 5 / 50 / 15 | `config.py:57-61` | unit (settings) | `unit/test_settings_bounds.py::*` | CONFIRMED |
| `just create-owner` CLI | `main/cli.py` | none | unchanged: not in this plan's required layers; exercised by hand (Evidence) | NOT CONFIRMED by automated test |
| Migration round trip | `0003_*.py` | integration | unchanged: still skipped in-suite; done by hand by the main session | NOT CONFIRMED (skipped) |

Note: the L2 429 reuses `TooManyAttempts`, whose message says "sign-in attempts" (the
implementer's documented choice); the tests assert the code, not the message.

## Review findings

Review 2026-10-03 (reviewer subagent, opus). Diff: `git diff main...HEAD` on
`feat/identity-access`, with a clean worktree. Commit `310fe2b` (the harness fast-lane fix) is
excluded from this plan's scope, as the dispatch said.

### Pass 1 — Checklist: 13/15 pass, 1 conditional, 1 not applicable

- [~] **`plans-scope`: exits 1, with three files out of scope.** Each one is accounted for:
  - `.claude/hooks/guard_paths.py` and `scripts/harness/test_hooks.py` come from `310fe2b`
    (the separate fast-lane fix).
  - `apps/api/src/fragancia_api/main/http.py` is a documented deviation. Its `DESCRIPTION` is
    needed by the `test_openapi.py` assertion in step 11.

  Hot files:
  - `.importlinter`: append-only.
  - `container.py` and `docs/modules.json` are **not strictly append-only**. `MODULES`
    became `(identity, catalog)`, the dev-token lines were removed, and the `identity`
    status changed from `planned` to `active`. Steps 1 and 3 order exactly these edits, so
    they are authorized.

  Accepted as documented, but the user should acknowledge `main/http.py` when marking the plan
  done. It is not in any step's `Files:` line.
- [x] `uv run just check` → green: lint, typecheck, arch "7 kept, 0 broken", "24 adapters up to
  date", "370 passed, 3 skipped, 42 deselected", harness "684 passed".
- [x] `uv run just test-integration` → "41 passed, 1 skipped". The skip is the in-suite
  migration round trip. The main session ran that round trip by hand (see Deviations).
- [x] Business rules live in `domain/`. Routers, mappers and queries hold no logic. Note
  (plan-prescribed, not a finding): `log_in.py:59` repeats `Email`'s strip-and-lowercase
  normalization to build the throttle key.
- [x] CQRS-lite: commands go through repositories inside `transactions.run` and return
  `Result`. `AccountQueries` reads through `Database.reader()` and returns contract models.
- [x] Every request and response model comes from `contracts.py`. `openapi.json` is current
  (`test_committed_document_is_up_to_date` is green) and lists the seven operations.
- [x] Expected errors use stable `IDENTITY_*` codes. The new kernel categories map to 401 and
  429. The only exceptions are the unreachable `LookupError` and the defensive 401/403
  (documented).
- [x] No money is involved. Times come from `Clock`; ids from `new_id()`.
- [x] Migration `0003` creates the schema first and drops it last. Its only foreign key stays
  inside the schema. Every constraint name matches the naming convention
  (`uq_users_email` = `USER_EMAIL_UNIQUE`). It drops nothing it did not create, and its
  downgrade mirrors the upgrade.
- [x] Every route is in `public_router` or `admin_router`. `assert_admin_routes_are_protected`
  runs with `{"APIKeyCookie": []}`.
- [x] Wiring: identity registers `ActorResolver`, and the container raises if no module does.
  `test_container.py` asserts that the resolver is a `ResolveSessionActor`. Adapters are
  created only in `module.py`, `container.py` and `main/`.
- [x] No secrets or real personal data. Fixtures use `example.test`, and the evidence uses
  `<synthetic>` passwords.
- [x] `## Deviations` is honest. Spot-checked:
  - `CreateUser` uses two `run`s with the hash in between (`create_user.py:54-75`).
  - The 401 description reads "Missing or unknown session" (`access.py:48`).
- [ ] **Docs are stale in two in-scope places.** See L3 and L4.
- n/a PR body: no PR exists yet. The main session writes it, with the six sections.

### Pass 2 — Findings

**Medium**

- **M1 — argon2 runs synchronously on the event loop.** Locations:
  `application/commands/log_in.py:93` (`verify`), `change_password.py:52,55` (`verify` + `hash`)
  and `create_user.py:64`.
  - **What fails.** `PasswordHasher.hash/verify` are synchronous CPU work called from
    `async def execute`. Measured on this machine with the library defaults: `hash` 0.094 s,
    `verify` 0.065 s. The whole uvicorn event loop stalls for that long on every call.
  - **Failure scenario.** `POST /api/v1/auth/login` is public. 20 concurrent login attempts
    (different emails and IPs, so the throttle does not stop them before `verify`) freeze every
    other request for about 1.3 s: storefront, admin and health. A sustained stream from
    rotating IPs keeps the API unresponsive.
  - **Fix direction** (for the implementer or architect, not prescribed here): move the
    hasher work off the loop, e.g. `await asyncio.to_thread(...)`. That needs either an async
    `PasswordHasher` port or the thread hop in the use cases.
  - **Decision needed.** If the user would rather accept this for a low-traffic back office,
    record it as a deviation with that decision. As it stands it is unaddressed, so status
    stays `review`.

**Low**

- **L1 — a crafted email turns login into a 500.** Locations: `log_in.py:59-60` and
  `infrastructure/tables.py:41` (`key String(330)`).
  - **What fails.** `LoginRequest.email` is capped at 320 characters *before* `.lower()`, and
    lowercasing can grow a string. Verified: `"İ" * 320` lowercases to 640 characters, so the
    key has 646 characters.
  - **Failure scenario.** The `INSERT` into `login_throttle` raises
    `StringDataRightTruncation`. The unauthenticated caller gets 500 `INTERNAL_ERROR` and a
    logged traceback.
  - The rolled-back transaction also means the attempt is not counted. It gains the attacker
    nothing, but it is an unhandled error path.
  - **Fix direction:** bound the key after normalization (e.g. reject or hash a normalized
    email longer than 254 characters), or bound the column/key differently.
- **L2 — `PUT /admin/auth/password` is an unthrottled password oracle.** Location:
  `change_password.py:52`. Uncertain: this may be by design, since the plan specifies no
  throttle here.
  - **What fails.** Whoever holds a session (e.g. a stolen or shared-machine cookie) can try
    `current_password` without limit. Each attempt returns 422
    `IDENTITY_CURRENT_PASSWORD_WRONG` or 204, and none passes through `LoginThrottle`.
  - **Failure scenario.** A hijacked session brute-forces the account password. The attacker
    then keeps access after the victim revokes the session, and the password may be reused
    elsewhere. Each attempt also costs about 65 ms of loop-blocking argon2 (M1).
  - The user decides whether to fix it here or file it for plan 002. It is not required by
    this plan's text.
- **L3 — stale doc.** Location: `apps/api/README.md:56` still says "A test checks that every
  `/api/v1/admin` operation requires the **bearer scheme**". The check is now the
  `APIKeyCookie` session-cookie scheme. The file is in step 10's list.
- **L4 — stale doc.** Location: `docs/architecture.md:154-156`, the "Errors" mapping. It lists
  401 for unauthenticated but not the new rate-limited → 429 category, while
  `apps/api/README.md:58` and `shared/http/errors.py:3` now list it. The file is in step 10's
  list.

**Informational (no change requested)**

- Three behaviors have no automated test, which the tester reported honestly:
  - `Settings` bounds (`ge=1`): the step 1 observable result
    `make_settings(session_idle_minutes=0)` raises.
  - The `create-owner` CLI.
  - The migration round trip, which is in-suite but skipped.

  The verifier should check the CLI and the round trip live, or mark them NOT VERIFIED.
- Accepted by the plan, not a defect: anyone can lock the owner out by spending 5 attempts
  per 15 minutes on the owner's email (the 6th attempt is 429 even with the right password,
  as the acceptance criterion requires). Worth remembering for the deployment and plan 002.
- Out of scope, already filed: `plans/findings/platform-harness-module-registry-stale.md`.

**Result:** 1 medium and 4 low findings. M1, L1, L3 and L4 need changes. L2 needs a user
decision. Status stays `review` (repair handoff to the implementer via the main session).

### Round 2 (2026-10-03, reviewer subagent, opus) — after repair round 1

Round 1 above is superseded for the repaired code. Diff reviewed: `git diff ee84ef6..HEAD`
(`c6a2d23` repairs, `d132922` regression tests), plus the whole branch against `main` for the
checklist. Clean worktree. `310fe2b` is excluded as before.

#### Pass 1 — Checklist: 14/15 pass, 1 conditional (scope), PR body n/a

- [~] **`plans-scope` exits 1 with four files out of scope.** The three from round 1 are
  unchanged and accounted for (`310fe2b` x2, `main/http.py` as a documented deviation). One is
  **new**: `apps/api/tests/unit/test_settings_bounds.py` (created in `d132922` by the tester).
  No step's `Files:` line names it (step 11 lists `tests/unit/test_settings.py`; step 12
  lists `tests/unit/identity/`). It is listed in `## Test coverage` but not in
  `## Deviations`. It is test-only and covers the step 1 observable result that round 1 noted
  as untested, so it needs no code change. See L5. Hot files: no change in this round.
- [x] `uv run just check` → green: lint, typecheck, arch "7 kept, 0 broken", plans-lint
  "7 plans, 3 findings", "24 adapters up to date", "404 passed, 3 skipped, 47 deselected",
  harness "684 passed", compose config.
- [x] `uv run just test-integration` → "46 passed, 1 skipped" (the same migration skip).
- [x] Business rules: still in `domain/`. `_email_key` (`log_in.py:21-28`) repeats `Email`'s
  `strip().lower()` (`domain/user.py:31`); the two are identical, so the key and the lookup
  agree.
- [x] CQRS-lite: `ChangePassword` counts and loads in one `run`, verifies outside, and
  saves/revokes/clears in a second `run`; it returns `Result`.
- [x] Contracts: no contract change. `openapi.json` was regenerated (the 429 and the new
  description on `change_my_password`); `test_committed_document_is_up_to_date` is green.
- [x] Errors: the 429 reuses `TooManyAttempts` (`IDENTITY_TOO_MANY_ATTEMPTS`, a
  `RateLimitedError`), which is the code decision 8 names.
- [x] Clock and ids: `ChangePassword` uses `self._clock.now()` for the hit.
- [x] Migrations: none in this round. `key String(330)` still fits every key: `email:` + at
  most 254 = 260, `email-sha256:` + 64 = 77, `password:` + 36 = 45.
- [x] Routes: unchanged; `PUT /admin/auth/password` declares 429 in `responses`.
- [x] Wiring: `module.py` builds one `SqlLoginThrottle` (stateless over `platform.database`)
  and shares it between `LogIn` and `ChangePassword`. `module.py:78` is the only
  `ChangePassword(...)` call. `CreateUser` in `main/cli.py` runs under `asyncio.run`, so the
  async hasher works there too.
- [x] No secrets or personal data in the new tests (synthetic passwords, `example.test`).
- [x] `## Deviations` → "Repair round 1" is honest. Spot-checked: the order in L2 (weak
  password first, then count + load, then 429 with no verify, then 422 with the attempt kept)
  matches `change_password.py:44-81`; the hasher runs both calls through `asyncio.to_thread`
  (`argon2_password_hasher.py:16-20`).
- [x] Docs: L3 (`apps/api/README.md:56`) and L4 (`docs/architecture.md:154-157`) are fixed.
  ADR 0009 now describes the password-change throttle and the worker thread.
- n/a PR body: no PR yet.

#### Round 1 findings — status

- **M1 — resolved.** `PasswordHasher.hash/verify` are `async` in the port; the argon2 adapter
  runs both in `asyncio.to_thread`. Every caller awaits them (`log_in.py:103`,
  `change_password.py:68,71`, `create_user.py:64`); no synchronous caller is left (grep over
  `apps/api/src`). The dummy hash is still computed once at wiring time. Covered by
  `test_argon2_off_the_loop.py`, including the loop-keeps-ticking test.
- **L1 — resolved.** `_email_key` hashes a normalized email longer than 254 characters into an
  `email-sha256:` key. That key is still counted and throttled, and it cannot collide with an
  `email:` key. The unit, HTTP (`"İ" * 320` → 401) and integration (real table, 6th attempt
  429) tests pass.
- **L2 — resolved per README decision 8.** The `password:<user_id>` key is counted before the
  check with `email_max_attempts`. Above it → 429 with no verify, even with the right password.
  A success clears the key in the same transaction as the save. A weak new password is
  rejected before counting, and that path does not verify the current password, so it is not an
  oracle.
- **L3, L4 — resolved** (above).

#### Pass 2 — Findings in the repair diff

No medium or high findings. Traced: request → `current_user`/`current_session` →
`ChangePassword` (validate → `run(count_and_load)` → limit → verify in thread → hash in
thread → `run(work)` with save, revoke others, clear) → `unwrap` → 204/422/429; and
`LogIn` with the new key function.

**Low**

- **L5 — a test file outside the plan's file list, not recorded in `## Deviations`.**
  Location: `apps/api/tests/unit/test_settings_bounds.py`.
  - **What fails.** Rule: "unplanned changes are findings even if the code is fine".
    `plans-scope` reports the file. Only `## Test coverage` mentions it.
  - **Failure scenario.** None at runtime. A later `plans-scope` reader cannot tell it from
    stray work. The same content could have gone in `tests/unit/test_settings.py`, which step
    11 lists.
  - **Needs no code change:** this entry documents it. The user should acknowledge it when
    marking the plan done, together with `main/http.py`.

**Informational (no change requested)**

- *Uncertain:* with argon2 off the loop, hashes now run in parallel, up to the default
  executor size (`min(32, cpu + 4)`). With the library defaults (64 MiB per hash) a flood of
  public logins from rotating IPs could use about `workers × 64 MiB` of memory at once (for
  example about 384 MiB on a 2-CPU VPS). Before, the blocked loop ran one hash at a time. This
  is not measured here. Worth remembering for sizing the deployment; a bounded semaphore around
  the hasher would cap it if it ever matters.
- `TooManyAttempts.message` says "Too many sign-in attempts" on the password-change 429. The
  implementer documented this as a choice. Clients branch on `code`.
- `test_the_event_loop_keeps_running_while_a_slow_verify_is_in_progress` depends on timing
  (0.3 s block, needs 5 of about 30 ticks). The margin is wide, but it could flake on a badly
  overloaded CI runner.
- Still not covered by automated tests (the verifier should check them live or mark them NOT
  VERIFIED): the `create-owner` CLI and the migration round trip (skipped in the suite).

**Result (round 2):** 0 high, 0 medium, 1 low (L5, documentation only, no code change).
Every round 1 finding is resolved and no regression was found. Status → `verify`.

## Verification

Verification 2026-10-03 (verifier subagent). Code under test: branch `feat/identity-access`, clean worktree.

Suites (run for real):
- `uv run just check` → green: "404 passed, 3 skipped, 47 deselected", harness "684 passed", compose config ok.
- `uv run just test-integration` → "46 passed, 1 skipped" (the skip is the in-suite migration round trip).

Migrations:
- `just db-migrate` clean on dev; `alembic check` → "No new upgrade operations detected."
- Round trip on the TEST database only: `alembic -x test=true downgrade -1` ("0003 -> 0002") then `upgrade head` ("0002 -> 0003"); current is `0003 (head)`.

Driven against `uv run just api` (port 8100), synthetic users `owner@example.test` and `throttle@example.test`
(created with `just create-owner --password-stdin`; passwords not recorded; cookies kept in the scratchpad):
- Login 200 body `{user:{id,email,name:"Dueña Prueba",role:"owner",permissions:["catalog:manage","users:manage"]},expires_at}`, no token in body.
- `Set-Cookie: fragancia_session=<redacted>; HttpOnly; Max-Age=43200; Path=/api/v1; SameSite=strict`, no `Secure`.
- `identity.sessions.token_hash` is 64 hex and equals sha256 of the cookie value (so it differs from the cookie).
- With cookie: `/admin/auth/me` 200, `/admin/brands` 200. Without: both 401 `AUTHENTICATION_REQUIRED`, no `WWW-Authenticate`. Bearer `dev-admin-token` → 401 (also on an instance started with `ADMIN_DEV_TOKEN=dev-admin-token` in its environment: key ignored, 401).
- Wrong password, unknown email and `"no-at"` → identical 401 `IDENTITY_INVALID_CREDENTIALS`.
- Throttle: 5 wrong → 401 x5, 6th with the right password → 429 `IDENTITY_TOO_MANY_ATTEMPTS`; another email from the same IP → 401.
- Two logins: `/admin/auth/sessions` lists 2, one `current: true`. `DELETE` other → 204 and its cookie then 401; random uuid → 404 `IDENTITY_SESSION_NOT_FOUND`.
- Password: wrong current → 422 `IDENTITY_CURRENT_PASSWORD_WRONG`; 11-char new → 422 `IDENTITY_PASSWORD_TOO_WEAK`; valid change → 204; other session → 401, current → 200; old password → 401, new → 200.
- Logout → 204 with a `Max-Age=0` cookie; the same cookie then 401.
- Idle: second instance with `SESSION_IDLE_MINUTES=1`: fresh session 200, after >60 s → 401.
- `just create-owner` twice → second exits 1 `✘ IDENTITY_EMAIL_TAKEN`; no password in output.
- OpenAPI served: `securitySchemes` only `APIKeyCookie` (in cookie, name `fragancia_session`). `/api/v1/docs` serves 200.
- Cleanup: seeded rows removed (5 sessions, 5 throttle rows, 2 users; counts checked first, WHERE limited to my emails and key prefixes). Servers stopped.

Note: one password-change probe used a 12-char string by mistake, so it succeeded (204); I continued with the real scenario afterwards, with no effect on results.

Acceptance criteria: 20/21 verified; 1 partial.
- NOT VERIFIED: Scalar in the browser (`log_in` then `get_my_account` with no manual auth). Only the pieces were checked: docs page 200, cookie `Path=/api/v1` covers `/api/v1/docs`, only the cookie scheme is declared. No browser was driven.
- NOT VERIFIED: a real `apps/api/.env` containing `ADMIN_DEV_TOKEN` (the file must not be read); simulated through the process environment instead.
- NOT VERIFIED: the `Secure` flag in production (unit test only).
- `plans-scope` still lists 4 out-of-scope files (already documented: `main/http.py`, `tests/unit/test_settings_bounds.py`, and two from `310fe2b`).

Accepted by the user on 2026-10-04 (status `done`): the three NOT VERIFIED items above and the
four out-of-scope files (`main/http.py`, `tests/unit/test_settings_bounds.py`, and the separate
harness fix `310fe2b`).
