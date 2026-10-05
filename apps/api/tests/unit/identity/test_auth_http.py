from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient, Response

from fragancia_api.main.http import build_app
from fragancia_api.modules.identity.application.commands.change_password import ChangePassword
from fragancia_api.modules.identity.application.commands.log_in import LogIn
from fragancia_api.modules.identity.application.commands.log_out import LogOut
from fragancia_api.modules.identity.application.commands.revoke_sessions import (
    RevokeOtherSessions,
    RevokeSession,
)
from fragancia_api.modules.identity.application.queries.my_account import (
    GetMyAccount,
    ListMySessions,
)
from fragancia_api.modules.identity.domain.user import Role
from fragancia_api.modules.identity.http.cookies import SessionCookie
from fragancia_api.modules.identity.http.router import routers
from fragancia_api.shared.application.actor import Actor, ActorResolver
from fragancia_api.shared.http import SESSION_COOKIE
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from tests.support import ADMIN_HEADERS, TestActorResolver, assert_admin_routes_are_protected
from tests.unit.identity.conftest import PASSWORD, POLICY, Identity

LOGIN = "/api/v1/auth/login"
ME = "/api/v1/admin/auth/me"
LOGOUT = "/api/v1/admin/auth/logout"
PASSWORD_URL = "/api/v1/admin/auth/password"
SESSIONS = "/api/v1/admin/auth/sessions"
OWNER = "owner@example.test"
NEW_PASSWORD = "a brand new passphrase"
MAX_AGE_SECONDS = int(POLICY.session_max_age.total_seconds())


def _services(identity: Identity, resolver: ActorResolver, *, secure: bool) -> ServiceRegistry:
    services = ServiceRegistry()
    services.add(ActorResolver, resolver)  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(LogIn, identity.log_in)
    services.add(LogOut, identity.log_out)
    services.add(ChangePassword, identity.change_password)
    services.add(RevokeSession, identity.revoke_session)
    services.add(RevokeOtherSessions, identity.revoke_others)
    services.add(GetMyAccount, identity.get_my_account)
    services.add(ListMySessions, identity.list_my_sessions)
    services.add(SessionCookie, SessionCookie(secure=secure, max_age_seconds=MAX_AGE_SECONDS))
    return services


@asynccontextmanager
async def _client(
    identity: Identity, resolver: ActorResolver | None = None, *, secure: bool = False
) -> AsyncIterator[AsyncClient]:
    app = build_app(_services(identity, resolver or identity.resolver, secure=secure), routers)
    assert_admin_routes_are_protected(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield client


@pytest.fixture
async def client(identity: Identity) -> AsyncIterator[AsyncClient]:
    identity.add_user()
    async with _client(identity) as client:
        yield client


async def _login(client: AsyncClient, email: str = OWNER, password: str = PASSWORD) -> Response:
    response = await client.post(LOGIN, json={"email": email, "password": password})
    client.cookies.clear()  # no cookie jar: every request sends only the cookies it names
    return response


def _token(response: Response) -> str:
    return response.headers["set-cookie"].split(";")[0].split("=", 1)[1]


def _cookie(token: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE}={token}"}


async def _signed_in(client: AsyncClient) -> dict[str, str]:
    response = await _login(client)
    assert response.status_code == 200
    return _cookie(_token(response))


# --- login -----------------------------------------------------------------------------------


async def test_login_returns_the_user_and_the_expiry(client: AsyncClient) -> None:
    response = await _login(client)

    body = response.json()
    assert response.status_code == 200
    assert set(body) == {"user", "expires_at"}
    assert {k: v for k, v in body["user"].items() if k != "id"} == {
        "email": OWNER,
        "name": "Owner Test",
        "role": "owner",
        "permissions": ["catalog:manage", "users:manage"],
    }
    UUID(body["user"]["id"])
    assert body["expires_at"].startswith("2026-10-03T00:00:00")  # FixedClock + 12h


async def test_login_never_puts_the_token_in_the_body(client: AsyncClient) -> None:
    response = await _login(client)

    assert _token(response) not in response.text


async def test_login_sets_an_httponly_samesite_strict_cookie_scoped_to_the_api(
    client: AsyncClient,
) -> None:
    response = await _login(client)

    header = response.headers["set-cookie"].lower()
    assert header.startswith(f"{SESSION_COOKIE}=token-1;")
    assert "httponly" in header
    assert "samesite=strict" in header
    assert "path=/api/v1" in header
    assert f"max-age={MAX_AGE_SECONDS}" in header
    assert "secure" not in header


async def test_the_cookie_is_secure_when_configured(identity: Identity) -> None:
    identity.add_user()
    async with _client(identity, secure=True) as client:
        response = await _login(client)

    assert "secure" in response.headers["set-cookie"].lower().split("; ")


async def test_a_wrong_password_is_401_with_the_credentials_code(client: AsyncClient) -> None:
    response = await _login(client, password="not the password")

    assert response.status_code == 401
    assert response.json()["code"] == "IDENTITY_INVALID_CREDENTIALS"
    assert "set-cookie" not in response.headers


async def test_unknown_and_malformed_emails_get_exactly_the_same_answer(
    client: AsyncClient,
) -> None:
    wrong_password = await _login(client, password="not the password")
    unknown = await _login(client, email="nobody@example.test")
    malformed = await _login(client, email="no-at")

    assert unknown.status_code == malformed.status_code == 401
    assert unknown.json() == malformed.json() == wrong_password.json()


async def test_the_attempt_after_the_email_limit_is_429_even_with_the_right_password(
    client: AsyncClient,
) -> None:
    for _ in range(POLICY.email_max_attempts):
        await _login(client, password="not the password")

    response = await _login(client)

    assert response.status_code == 429
    assert response.json()["code"] == "IDENTITY_TOO_MANY_ATTEMPTS"
    assert "set-cookie" not in response.headers


async def test_a_blocked_email_does_not_block_another_account_from_the_same_ip(
    identity: Identity, client: AsyncClient
) -> None:
    identity.add_user("staff@example.test", role=Role.STAFF)
    for _ in range(POLICY.email_max_attempts + 1):
        await _login(client, password="not the password")

    response = await _login(client, email="staff@example.test")

    assert response.status_code == 200


@pytest.mark.parametrize(
    "payload",
    [{}, {"email": OWNER}, {"password": PASSWORD}, {"email": "a" * 321, "password": PASSWORD}],
)
async def test_login_validates_the_payload_shape(
    client: AsyncClient, payload: dict[str, str]
) -> None:
    response = await client.post(LOGIN, json=payload)

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


# --- access ----------------------------------------------------------------------------------


async def test_the_session_cookie_opens_the_admin_routes(client: AsyncClient) -> None:
    headers = await _signed_in(client)

    response = await client.get(ME, headers=headers)

    assert response.status_code == 200
    assert response.json()["email"] == OWNER


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Cookie": f"{SESSION_COOKIE}=unknown"},
        {"Authorization": "Bearer dev-admin-token"},
    ],
    ids=["no credentials", "unknown cookie", "bearer header"],
)
async def test_without_a_valid_session_cookie_admin_routes_are_401(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.get(ME, headers=headers)

    assert (response.status_code, response.json()["code"]) == (401, "AUTHENTICATION_REQUIRED")
    assert "www-authenticate" not in response.headers


async def test_a_bearer_header_does_not_replace_a_missing_cookie_even_with_a_real_token(
    client: AsyncClient,
) -> None:
    token = _token(await _login(client))

    response = await client.get(ME, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


async def test_every_identity_admin_operation_is_declared_and_protected(
    identity: Identity,
) -> None:
    async with _client(identity) as client:
        paths = (await client.get("/api/v1/openapi.json")).json()["paths"]

    operations = {
        (method.upper(), path) for path, item in paths.items() for method in item if "auth" in path
    }
    assert operations == {
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/admin/auth/logout"),
        ("GET", "/api/v1/admin/auth/me"),
        ("PUT", "/api/v1/admin/auth/password"),
        ("GET", "/api/v1/admin/auth/sessions"),
        ("DELETE", "/api/v1/admin/auth/sessions/{session_id}"),
        ("DELETE", "/api/v1/admin/auth/sessions"),
    }


# --- logout ----------------------------------------------------------------------------------


async def test_logout_is_204_clears_the_cookie_and_kills_the_session(client: AsyncClient) -> None:
    headers = await _signed_in(client)

    response = await client.post(LOGOUT, headers=headers)

    cleared = response.headers["set-cookie"].lower()
    assert response.status_code == 204
    assert cleared.startswith(f"{SESSION_COOKIE}=;") or f'{SESSION_COOKIE}=""' in cleared
    assert "max-age=0" in cleared
    assert "path=/api/v1" in cleared
    assert (await client.get(ME, headers=headers)).status_code == 401


# --- password --------------------------------------------------------------------------------


async def test_changing_the_password_keeps_this_session_and_closes_the_others(
    client: AsyncClient,
) -> None:
    current = await _signed_in(client)
    other = await _signed_in(client)

    response = await client.put(
        PASSWORD_URL,
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=current,
    )

    assert response.status_code == 204
    assert (await client.get(ME, headers=current)).status_code == 200
    assert (await client.get(ME, headers=other)).status_code == 401
    assert (await _login(client, password=PASSWORD)).status_code == 401
    assert (await _login(client, password=NEW_PASSWORD)).status_code == 200


async def test_a_wrong_current_password_is_422(client: AsyncClient) -> None:
    headers = await _signed_in(client)

    response = await client.put(
        PASSWORD_URL,
        json={"current_password": "not it at all", "new_password": NEW_PASSWORD},
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "IDENTITY_CURRENT_PASSWORD_WRONG"


async def test_a_new_password_of_11_characters_is_422_with_the_limits(
    client: AsyncClient,
) -> None:
    headers = await _signed_in(client)

    response = await client.put(
        PASSWORD_URL,
        json={"current_password": PASSWORD, "new_password": "a" * 11},
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "IDENTITY_PASSWORD_TOO_WEAK"
    assert response.json()["details"] == {"min": 12, "max": 128}


async def test_the_password_route_validates_the_payload_shape(client: AsyncClient) -> None:
    headers = await _signed_in(client)

    response = await client.put(PASSWORD_URL, json={"new_password": NEW_PASSWORD}, headers=headers)

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


# --- sessions --------------------------------------------------------------------------------


async def test_lists_my_sessions_marking_exactly_the_current_one(client: AsyncClient) -> None:
    first = await _signed_in(client)
    second = await _signed_in(client)

    response = await client.get(SESSIONS, headers=second)

    sessions = response.json()
    assert response.status_code == 200
    assert len(sessions) == 2
    assert [s["current"] for s in sessions].count(True) == 1
    assert set(sessions[0]) == {
        "id",
        "created_at",
        "last_seen_at",
        "expires_at",
        "user_agent",
        "ip",
        "current",
    }
    current = next(s for s in sessions if s["current"])
    assert (await client.get(SESSIONS, headers=first)).json() != sessions  # the mark moved
    assert current["ip"] == "127.0.0.1"  # the ASGI test client address


async def test_closing_another_session_is_204_and_that_cookie_stops_working(
    client: AsyncClient,
) -> None:
    current = await _signed_in(client)
    other = await _signed_in(client)
    listed = (await client.get(SESSIONS, headers=current)).json()
    other_id = next(s["id"] for s in listed if not s["current"])

    response = await client.delete(f"{SESSIONS}/{other_id}", headers=current)

    assert response.status_code == 204
    assert (await client.get(ME, headers=other)).status_code == 401
    assert (await client.get(ME, headers=current)).status_code == 200


async def test_closing_an_unknown_session_is_404(client: AsyncClient) -> None:
    headers = await _signed_in(client)

    response = await client.delete(f"{SESSIONS}/{uuid4()}", headers=headers)

    assert (response.status_code, response.json()["code"]) == (404, "IDENTITY_SESSION_NOT_FOUND")


async def test_a_session_id_that_is_not_a_uuid_is_a_validation_error(client: AsyncClient) -> None:
    headers = await _signed_in(client)

    response = await client.delete(f"{SESSIONS}/not-a-uuid", headers=headers)

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_closing_the_other_sessions_keeps_the_current_one(client: AsyncClient) -> None:
    current = await _signed_in(client)
    others = [await _signed_in(client) for _ in range(2)]

    response = await client.delete(SESSIONS, headers=current)

    assert response.status_code == 204
    assert (await client.get(ME, headers=current)).status_code == 200
    for other in others:
        assert (await client.get(ME, headers=other)).status_code == 401
    assert len((await client.get(SESSIONS, headers=current)).json()) == 1


# --- repair round 1: over-long email (L1) and the password-change throttle (L2) ---------------


async def test_an_email_that_lowercases_past_254_characters_is_401_not_500(
    client: AsyncClient,
) -> None:
    response = await _login(client, email="İ" * 320)  # 640 characters once lowercased

    assert response.status_code == 401
    assert response.json()["code"] == "IDENTITY_INVALID_CREDENTIALS"


async def test_the_password_attempt_above_the_limit_is_429_even_with_the_right_password(
    client: AsyncClient,
) -> None:
    headers = await _signed_in(client)
    for _ in range(POLICY.email_max_attempts):
        wrong = await client.put(
            PASSWORD_URL,
            json={"current_password": "not it at all", "new_password": NEW_PASSWORD},
            headers=headers,
        )
        assert wrong.json()["code"] == "IDENTITY_CURRENT_PASSWORD_WRONG"

    response = await client.put(
        PASSWORD_URL,
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=headers,
    )

    assert response.status_code == 429
    assert response.json()["code"] == "IDENTITY_TOO_MANY_ATTEMPTS"
    assert (await _login(client, password=PASSWORD)).status_code == 200  # still the old one


# --- actors the identity resolver did not produce --------------------------------------------


class SessionlessAdmin:
    """An admin with a real user id but no session: only a test resolver can be like this."""

    async def resolve(self, token: str) -> Actor | None:
        return Actor(id=str(uuid4()), is_admin=True, session_id=None)


ID = uuid4()
SESSION_ONLY_ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("POST", LOGOUT, None),
    ("GET", ME, None),
    ("PUT", PASSWORD_URL, {"current_password": PASSWORD, "new_password": NEW_PASSWORD}),
    ("GET", SESSIONS, None),
    ("DELETE", f"{SESSIONS}/{ID}", None),
    ("DELETE", SESSIONS, None),
]


@pytest.mark.parametrize(("method", "url", "body"), SESSION_ONLY_ROUTES)
async def test_an_actor_without_a_session_is_403_on_every_session_route(
    identity: Identity, method: str, url: str, body: dict[str, Any] | None
) -> None:
    async with _client(identity, SessionlessAdmin()) as client:
        response = await client.request(method, url, json=body, headers=_cookie("anything"))

    assert (response.status_code, response.json()["code"]) == (403, "FORBIDDEN")


@pytest.mark.parametrize(
    ("method", "url", "body"), [route for route in SESSION_ONLY_ROUTES if route[0] != "POST"]
)
async def test_an_actor_whose_id_is_not_a_user_uuid_is_403_on_the_account_routes(
    identity: Identity, method: str, url: str, body: dict[str, Any] | None
) -> None:
    async with _client(identity, TestActorResolver()) as client:
        response = await client.request(method, url, json=body, headers=ADMIN_HEADERS)

    assert (response.status_code, response.json()["code"]) == (403, "FORBIDDEN")
