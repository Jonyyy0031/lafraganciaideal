import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, text

from fragancia_api.container import Container
from fragancia_api.modules.identity.application.commands.create_user import CreateUser
from fragancia_api.modules.identity.application.commands.log_in import LogIn
from fragancia_api.modules.identity.application.commands.resolve_session_actor import (
    ResolveSessionActor,
)
from fragancia_api.modules.identity.domain.user import Email, Role
from fragancia_api.modules.identity.infrastructure.sql_account_queries import SqlAccountQueries
from fragancia_api.modules.identity.infrastructure.sql_login_throttle import SqlLoginThrottle
from fragancia_api.modules.identity.infrastructure.sql_session_repository import (
    SqlSessionRepository,
)
from fragancia_api.modules.identity.infrastructure.sql_user_repository import SqlUserRepository
from fragancia_api.modules.identity.infrastructure.tables import sessions as sessions_table
from fragancia_api.modules.identity.infrastructure.tables import users as users_table
from fragancia_api.shared.application.actor import ActorResolver
from fragancia_api.shared.kernel import Err, Ok, new_id
from tests.integration.identity.conftest import (
    IDLE,
    NOW,
    in_transaction,
    make_session,
    make_user,
    store_session,
    store_user,
)

pytestmark = pytest.mark.integration

WINDOW = timedelta(minutes=15)


# --- users -----------------------------------------------------------------------------------


async def test_a_user_round_trips_through_the_table(container: Container) -> None:
    repository = SqlUserRepository(container.database)
    user = make_user()
    await store_user(container, user)

    by_id = await in_transaction(container, lambda: repository.get(user.id))
    by_email = await in_transaction(
        container, lambda: repository.get_by_email(Email(user.email.value))
    )

    for found in (by_id, by_email):
        assert found is not None
        assert (found.id, found.email, found.name, found.role, found.is_active) == (
            user.id,
            user.email,
            user.name,
            Role.OWNER,
            True,
        )
        assert (found.password_hash, found.created_at) == ("hash-1", NOW)
    assert await in_transaction(container, lambda: repository.get(new_id())) is None
    assert await in_transaction(container, lambda: repository.get_by_email(Email("x@y.co"))) is None


async def test_saving_a_user_persists_the_new_password(container: Container) -> None:
    repository = SqlUserRepository(container.database)
    user = make_user()
    await store_user(container, user)
    changed_at = NOW + timedelta(days=1)
    user.change_password("hash-2", now=changed_at)

    await in_transaction(container, lambda: repository.save(user))

    found = await in_transaction(container, lambda: repository.get(user.id))
    assert found is not None
    assert (found.password_hash, found.password_changed_at, found.created_at) == (
        "hash-2",
        changed_at,
        NOW,
    )


async def test_a_duplicate_email_is_an_err_and_keeps_the_transaction_usable(
    container: Container,
) -> None:
    repository = SqlUserRepository(container.database)
    first, clash = make_user(), make_user()  # same email, skips any exists check
    outcome: list[object] = []

    async def work() -> None:
        outcome.append(await repository.add(first))
        outcome.append(await repository.add(clash))  # hits uq_users_email inside a savepoint
        outcome.append(await repository.get_by_email(first.email))  # the transaction still works

    await in_transaction(container, work)

    assert isinstance(outcome[0], Ok)
    assert isinstance(outcome[1], Err) and outcome[1].error.code == "IDENTITY_EMAIL_TAKEN"
    assert outcome[2] is not None
    async with container.database.reader() as session:
        assert (await session.execute(select(users_table.c.id))).scalar_one() == first.id


async def test_concurrent_creation_of_the_same_email_yields_one_user_and_conflicts(
    container: Container,
) -> None:
    create = container.services.get(CreateUser)

    results = await asyncio.gather(
        *(
            create.execute("race@example.test", "Race Prueba", "a long enough password", Role.STAFF)
            for _ in range(5)
        )
    )

    assert sum(isinstance(r, Ok) for r in results) == 1
    assert [r.error.code for r in results if isinstance(r, Err)] == ["IDENTITY_EMAIL_TAKEN"] * 4
    async with container.database.reader() as session:
        assert len((await session.execute(select(users_table.c.id))).all()) == 1


async def test_the_database_refuses_a_second_email_even_when_bypassing_the_repository(
    container: Container,
) -> None:
    await store_user(container, make_user())
    other = make_user()

    with pytest.raises(Exception, match="uq_users_email"):
        async with container.database.engine.begin() as connection:
            await connection.execute(
                users_table.insert().values(
                    id=other.id,
                    email=other.email.value,
                    name="x y",
                    role="staff",
                    password_hash="h",
                    is_active=True,
                    created_at=NOW,
                    password_changed_at=NOW,
                )
            )


# --- sessions --------------------------------------------------------------------------------


async def test_a_session_round_trips_and_is_found_by_its_token_hash(container: Container) -> None:
    repository = SqlSessionRepository(container.database)
    user = make_user()
    await store_user(container, user)
    session = make_session(user.id, "a" * 64)
    await store_session(container, session)

    by_id = await in_transaction(container, lambda: repository.get(session.id))
    by_hash = await in_transaction(container, lambda: repository.get_by_token_hash("a" * 64))

    for found in (by_id, by_hash):
        assert found is not None
        assert (found.id, found.user_id, found.token_hash) == (session.id, user.id, "a" * 64)
        assert (found.created_at, found.last_seen_at, found.expires_at) == (
            NOW,
            NOW,
            NOW + timedelta(hours=12),
        )
        assert (found.revoked_at, found.user_agent, found.ip) == (None, "tests", "203.0.113.7")
    assert await in_transaction(container, lambda: repository.get_by_token_hash("b" * 64)) is None


async def test_saving_a_session_persists_last_seen_and_revocation(container: Container) -> None:
    repository = SqlSessionRepository(container.database)
    user = make_user()
    await store_user(container, user)
    session = make_session(user.id, "a" * 64)
    await store_session(container, session)
    later = NOW + timedelta(minutes=5)
    session.touch(later)
    session.revoke(later)

    await in_transaction(container, lambda: repository.save(session))

    found = await in_transaction(container, lambda: repository.get(session.id))
    assert found is not None
    assert (found.last_seen_at, found.revoked_at) == (later, later)


async def test_revoking_all_sessions_of_a_user_spares_the_kept_one_and_other_users(
    container: Container,
) -> None:
    repository = SqlSessionRepository(container.database)
    owner, staff = make_user(), make_user("staff@example.test", role=Role.STAFF)
    await store_user(container, owner)
    await store_user(container, staff)
    kept, closed, theirs = (
        make_session(owner.id, "1" * 64),
        make_session(owner.id, "2" * 64),
        make_session(staff.id, "3" * 64),
    )
    for session in (kept, closed, theirs):
        await store_session(container, session)
    later = NOW + timedelta(minutes=1)

    await in_transaction(
        container, lambda: repository.revoke_all_for_user(owner.id, except_id=kept.id, now=later)
    )

    revoked = {
        s.id: s.revoked_at
        for s in [
            await in_transaction(container, lambda i=i: repository.get(i))  # type: ignore[misc]
            for i in (kept.id, closed.id, theirs.id)
        ]
        if s is not None
    }
    assert revoked == {kept.id: None, closed.id: later, theirs.id: None}


async def test_revoking_all_without_an_exception_closes_every_session_of_the_user(
    container: Container,
) -> None:
    repository = SqlSessionRepository(container.database)
    user = make_user()
    await store_user(container, user)
    first, second = make_session(user.id, "1" * 64), make_session(user.id, "2" * 64)
    for session in (first, second):
        await store_session(container, session)

    await in_transaction(
        container, lambda: repository.revoke_all_for_user(user.id, except_id=None, now=NOW)
    )

    async with container.database.reader() as db:
        revoked = (await db.execute(select(sessions_table.c.revoked_at))).scalars().all()
    assert revoked == [NOW, NOW]


async def test_revoking_all_keeps_the_original_time_of_an_already_revoked_session(
    container: Container,
) -> None:
    repository = SqlSessionRepository(container.database)
    user = make_user()
    await store_user(container, user)
    session = make_session(user.id, "1" * 64)
    session.revoke(NOW)
    await store_session(container, session)

    await in_transaction(
        container,
        lambda: repository.revoke_all_for_user(
            user.id, except_id=None, now=NOW + timedelta(hours=1)
        ),
    )

    found = await in_transaction(container, lambda: repository.get(session.id))
    assert found is not None and found.revoked_at == NOW


async def test_a_session_needs_an_existing_user(container: Container) -> None:
    orphan = make_session(new_id(), "9" * 64)

    with pytest.raises(Exception, match="foreign key"):
        await store_session(container, orphan)


async def test_two_sessions_cannot_share_a_token_hash(container: Container) -> None:
    user = make_user()
    await store_user(container, user)
    await store_session(container, make_session(user.id, "a" * 64))

    with pytest.raises(Exception, match="token_hash"):
        await store_session(container, make_session(user.id, "a" * 64))


# --- login throttle --------------------------------------------------------------------------


async def _hit(container: Container, key: str, *, at: datetime = NOW) -> int:
    throttle = SqlLoginThrottle(container.database)
    return await in_transaction(container, lambda: throttle.hit(key, now=at, window=WINDOW))


async def test_hits_in_one_window_count_up(container: Container) -> None:
    assert [await _hit(container, "email:a") for _ in range(3)] == [1, 2, 3]


async def test_keys_count_independently(container: Container) -> None:
    assert await _hit(container, "email:a") == 1
    assert await _hit(container, "email:b") == 1
    assert await _hit(container, "email:a") == 2


async def test_parallel_hits_each_see_their_own_count(container: Container) -> None:
    counts = await asyncio.gather(*(_hit(container, "email:race") for _ in range(10)))

    assert sorted(counts) == list(range(1, 11))


async def test_a_hit_after_the_window_starts_a_new_one(container: Container) -> None:
    await _hit(container, "email:a")
    await _hit(container, "email:a")

    assert await _hit(container, "email:a", at=NOW + WINDOW - timedelta(seconds=1)) == 3
    assert await _hit(container, "email:a", at=NOW + WINDOW) == 1
    assert await _hit(container, "email:a", at=NOW + WINDOW + timedelta(minutes=1)) == 2


async def test_clearing_a_key_resets_it(container: Container) -> None:
    throttle = SqlLoginThrottle(container.database)
    await _hit(container, "email:a")
    await _hit(container, "email:a")

    await in_transaction(container, lambda: throttle.clear("email:a"))

    assert await _hit(container, "email:a") == 1


async def test_giving_back_an_attempt_never_goes_below_zero(container: Container) -> None:
    throttle = SqlLoginThrottle(container.database)
    await _hit(container, "ip:x")
    await _hit(container, "ip:x")

    for _ in range(4):
        await in_transaction(container, lambda: throttle.give_back("ip:x"))

    async with container.database.reader() as session:
        attempts = (
            await session.execute(
                text("SELECT attempts FROM identity.login_throttle WHERE key = 'ip:x'")
            )
        ).scalar_one()
    assert attempts == 0
    assert await _hit(container, "ip:x") == 1


async def test_giving_back_an_unknown_key_does_nothing(container: Container) -> None:
    throttle = SqlLoginThrottle(container.database)

    await in_transaction(container, lambda: throttle.give_back("ip:never-seen"))

    async with container.database.reader() as session:
        rows = (
            await session.execute(text("SELECT count(*) FROM identity.login_throttle"))
        ).scalar_one()
    assert rows == 0


# --- account queries -------------------------------------------------------------------------


async def test_me_derives_permissions_from_the_role(container: Container) -> None:
    queries = SqlAccountQueries(container.database)
    owner, staff = make_user(), make_user("staff@example.test", role=Role.STAFF)
    await store_user(container, owner)
    await store_user(container, staff)

    owner_me, staff_me = await queries.me(owner.id), await queries.me(staff.id)

    assert owner_me is not None and staff_me is not None
    assert (owner_me.id, owner_me.email, owner_me.name, owner_me.role) == (
        owner.id,
        "owner@example.test",
        "Owner Test",
        "owner",
    )
    assert owner_me.permissions == ["catalog:manage", "users:manage"]
    assert (staff_me.role, staff_me.permissions) == ("staff", ["catalog:manage"])
    assert await queries.me(new_id()) is None


async def test_my_sessions_filters_by_user_revocation_expiry_and_idleness_and_orders_newest_first(
    container: Container,
) -> None:
    queries = SqlAccountQueries(container.database)
    owner, staff = make_user(), make_user("staff@example.test", role=Role.STAFF)
    await store_user(container, owner)
    await store_user(container, staff)
    now = NOW + timedelta(hours=1)
    older = make_session(owner.id, "1" * 64, opened=NOW)
    newer = make_session(owner.id, "2" * 64, opened=NOW + timedelta(minutes=30))
    revoked = make_session(owner.id, "3" * 64, opened=NOW + timedelta(minutes=40))
    revoked.revoke(now)
    expired = make_session(owner.id, "4" * 64, opened=NOW, max_age=timedelta(minutes=30))
    expired.touch(now - timedelta(minutes=1))  # recently seen, but past its absolute expiry
    idle = make_session(owner.id, "5" * 64, opened=NOW - timedelta(hours=3))
    theirs = make_session(staff.id, "6" * 64, opened=NOW)
    for session in (older, newer, revoked, expired, idle, theirs):
        await store_session(container, session)

    listed = await queries.sessions(owner.id, current_session_id=newer.id, now=now, idle=IDLE)

    assert [s.id for s in listed] == [newer.id, older.id]
    assert [s.current for s in listed] == [True, False]
    assert (listed[0].user_agent, listed[0].ip) == ("tests", "203.0.113.7")
    assert (listed[0].created_at, listed[0].expires_at) == (
        newer.created_at,
        newer.expires_at,
    )


async def test_a_session_idle_exactly_for_the_timeout_is_not_listed(container: Container) -> None:
    queries = SqlAccountQueries(container.database)
    user = make_user()
    await store_user(container, user)
    session = make_session(user.id, "1" * 64)
    await store_session(container, session)

    at_deadline = await queries.sessions(
        user.id, current_session_id=session.id, now=NOW + IDLE, idle=IDLE
    )
    just_before = await queries.sessions(
        user.id,
        current_session_id=session.id,
        now=NOW + IDLE - timedelta(seconds=1),
        idle=IDLE,
    )

    assert at_deadline == []
    assert [s.id for s in just_before] == [session.id]


# --- the real stack (argon2, SQL, real clock) --------------------------------------------------

PASSWORD = "a long enough password"


async def test_a_created_user_signs_in_and_the_token_resolves_to_them(container: Container) -> None:
    created = await container.services.get(CreateUser).execute(
        "  Owner@Example.TEST ", "Dueña Prueba", PASSWORD, Role.OWNER
    )
    assert isinstance(created, Ok)

    login = await container.services.get(LogIn).execute(
        "owner@example.test", PASSWORD, ip="203.0.113.7", user_agent="tests"
    )

    assert isinstance(login, Ok)
    async with container.database.reader() as session:
        user_row = (await session.execute(select(users_table))).mappings().one()
        session_row = (await session.execute(select(sessions_table))).mappings().one()
    assert user_row["password_hash"].startswith("$argon2id$")
    assert PASSWORD not in user_row["password_hash"]
    assert (user_row["email"], user_row["name"], user_row["role"]) == (
        "owner@example.test",
        "Dueña Prueba",
        "owner",
    )
    assert len(session_row["token_hash"]) == 64
    assert session_row["token_hash"] != login.value.token
    assert session_row["user_id"] == created.value
    resolver = container.services.get(ActorResolver)  # type: ignore[type-abstract]
    assert isinstance(resolver, ResolveSessionActor)
    actor = await resolver.resolve(login.value.token)
    assert actor is not None
    assert (actor.id, actor.is_admin, actor.session_id) == (
        str(created.value),
        True,
        session_row["id"],
    )
    assert await resolver.resolve("not-a-token") is None


async def test_a_wrong_password_and_an_unknown_email_fail_the_same_way_against_the_real_stack(
    container: Container,
) -> None:
    await container.services.get(CreateUser).execute(
        "owner@example.test", "Dueña Prueba", PASSWORD, Role.OWNER
    )
    log_in = container.services.get(LogIn)

    wrong = await log_in.execute(
        "owner@example.test", "not the password!", ip=None, user_agent=None
    )
    unknown = await log_in.execute("nobody@example.test", PASSWORD, ip=None, user_agent=None)

    assert isinstance(wrong, Err) and isinstance(unknown, Err)
    assert wrong.error == unknown.error
    async with container.database.reader() as session:
        assert (await session.execute(select(sessions_table.c.id))).all() == []


async def test_the_sixth_attempt_for_an_email_is_blocked_by_the_real_throttle(
    container: Container,
) -> None:
    await container.services.get(CreateUser).execute(
        "owner@example.test", "Dueña Prueba", PASSWORD, Role.OWNER
    )
    log_in = container.services.get(LogIn)
    limit = container.settings.login_email_max_attempts

    for _ in range(limit):
        await log_in.execute("owner@example.test", "not the password!", ip=None, user_agent=None)
    blocked = await log_in.execute("owner@example.test", PASSWORD, ip=None, user_agent=None)

    assert isinstance(blocked, Err) and blocked.error.code == "IDENTITY_TOO_MANY_ATTEMPTS"
    async with container.database.reader() as session:
        assert (await session.execute(select(sessions_table.c.id))).all() == []


async def test_parallel_wrong_guesses_cannot_slip_past_the_email_limit(
    container: Container,
) -> None:
    await container.services.get(CreateUser).execute(
        "owner@example.test", "Dueña Prueba", PASSWORD, Role.OWNER
    )
    log_in = container.services.get(LogIn)
    limit = container.settings.login_email_max_attempts

    results = await asyncio.gather(
        *(
            log_in.execute("owner@example.test", f"wrong password {n}", ip=None, user_agent=None)
            for n in range(limit + 5)
        )
    )

    codes = sorted(r.error.code for r in results if isinstance(r, Err))
    assert codes == sorted(
        ["IDENTITY_INVALID_CREDENTIALS"] * limit + ["IDENTITY_TOO_MANY_ATTEMPTS"] * 5
    )


@pytest.mark.skip(
    reason="NOT CONFIRMED: the migration round trip (downgrade -1 / upgrade head) is not "
    "automated: downgrading fragancia_test inside the suite would race the other integration "
    "tests. It was run by hand by the main session (plan 001, Deviations)."
)
async def test_migration_0003_downgrades_and_upgrades_cleanly() -> None: ...
