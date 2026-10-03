---
status: approved
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

## Test coverage

## Review findings

## Verification
