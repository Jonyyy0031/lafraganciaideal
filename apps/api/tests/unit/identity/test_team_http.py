from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.modules.identity.application.commands.invitations import (
    AcceptInvitation,
    InviteUser,
    RevokeInvitation,
)
from fragancia_api.modules.identity.application.commands.log_in import LogIn
from fragancia_api.modules.identity.application.commands.password_reset import (
    RequestPasswordReset,
    ResetPassword,
)
from fragancia_api.modules.identity.application.commands.user_status import (
    DeactivateUser,
    ReactivateUser,
)
from fragancia_api.modules.identity.application.queries.my_account import GetMyAccount
from fragancia_api.modules.identity.application.queries.team import (
    ListPendingInvitations,
    ListUsers,
)
from fragancia_api.modules.identity.domain.events import InvitationIssued, PasswordResetRequested
from fragancia_api.modules.identity.domain.user import USERS_MANAGE, Role
from fragancia_api.modules.identity.http.cookies import SessionCookie
from fragancia_api.modules.identity.http.router import routers
from fragancia_api.shared.application.actor import Actor, ActorResolver
from fragancia_api.shared.http import SESSION_COOKIE
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from tests.support import ADMIN_HEADERS, TestActorResolver, assert_admin_routes_are_protected
from tests.unit.identity.conftest import LINKS, PASSWORD, POLICY, Identity

USERS = "/api/v1/admin/users"
INVITATIONS = "/api/v1/admin/invitations"
ACCEPT = "/api/v1/auth/invitations/accept"
RESET = "/api/v1/auth/password-reset"
CONFIRM = "/api/v1/auth/password-reset/confirm"
ME = "/api/v1/admin/auth/me"
OWNER = "owner@example.test"
STAFF = "staff@example.test"
NEW_PASSWORD = "a brand new passphrase"
UNKNOWN_ID = "00000000-0000-0000-0000-000000000063"


def _services(identity: Identity, resolver: ActorResolver) -> ServiceRegistry:
    services = ServiceRegistry()
    services.add(ActorResolver, resolver)  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(LogIn, identity.log_in)
    services.add(GetMyAccount, identity.get_my_account)
    services.add(SessionCookie, SessionCookie(secure=False, max_age_seconds=3600))
    services.add(InviteUser, identity.invite_user)
    services.add(RevokeInvitation, identity.revoke_invitation)
    services.add(AcceptInvitation, identity.accept_invitation)
    services.add(RequestPasswordReset, identity.request_reset)
    services.add(ResetPassword, identity.reset_password)
    services.add(DeactivateUser, identity.deactivate_user)
    services.add(ReactivateUser, identity.reactivate_user)
    services.add(ListUsers, identity.list_users)
    services.add(ListPendingInvitations, identity.list_pending_invitations)
    return services


@asynccontextmanager
async def _client(
    identity: Identity, resolver: ActorResolver | None = None
) -> AsyncIterator[AsyncClient]:
    app = build_app(_services(identity, resolver or identity.resolver), routers)
    assert_admin_routes_are_protected(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield client


def _cookie(token: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE}={token}"}


@pytest.fixture
async def world(identity: Identity) -> AsyncIterator[tuple[AsyncClient, dict[str, str]]]:
    """A client plus the cookie of a signed-in owner."""
    identity.add_user(OWNER)
    login = await identity.sign_in(OWNER)
    async with _client(identity) as client:
        yield client, _cookie(login.token)


async def _mailed_token(identity: Identity, event_type: type) -> str:
    """Deliver the last event of that type as the worker would; read the token from the email."""
    event = next(e for e in reversed(identity.events.published) if isinstance(e, event_type))
    handler = (
        identity.send_invitation_email
        if event_type is InvitationIssued
        else identity.send_reset_email
    )
    await handler(identity.message_of(event))
    return identity.email.sent[-1].body.split("#token=")[1].split()[0]


# --- access ----------------------------------------------------------------------------------

TEAM_ROUTES = [
    ("GET", USERS, None),
    ("POST", f"{USERS}/{UNKNOWN_ID}/deactivate", None),
    ("POST", f"{USERS}/{UNKNOWN_ID}/reactivate", None),
    ("GET", INVITATIONS, None),
    ("POST", INVITATIONS, {"email": "new@example.test", "name": "Nueva Persona"}),
    ("DELETE", f"{INVITATIONS}/{UNKNOWN_ID}", None),
]


@pytest.mark.parametrize(("method", "path", "body"), TEAM_ROUTES)
async def test_a_staff_session_is_403_forbidden_on_every_team_route(
    identity: Identity, method: str, path: str, body: dict[str, str] | None
) -> None:
    identity.add_user(STAFF, role=Role.STAFF)
    login = await identity.sign_in(STAFF)
    async with _client(identity) as client:
        response = await client.request(method, path, json=body, headers=_cookie(login.token))

    assert (response.status_code, response.json()["code"]) == (403, "FORBIDDEN")
    assert identity.invitations.by_id == {}  # nothing happened


@pytest.mark.parametrize(("method", "path", "body"), TEAM_ROUTES)
async def test_no_cookie_is_401_on_every_team_route(
    identity: Identity, method: str, path: str, body: dict[str, str] | None
) -> None:
    async with _client(identity) as client:
        response = await client.request(method, path, json=body)

    assert (response.status_code, response.json()["code"]) == (401, "AUTHENTICATION_REQUIRED")


@pytest.mark.parametrize(("method", "path", "body"), TEAM_ROUTES)
async def test_an_admin_without_the_permission_and_without_a_session_is_403(
    identity: Identity, method: str, path: str, body: dict[str, str] | None
) -> None:
    async with _client(identity, TestActorResolver()) as client:
        response = await client.request(method, path, json=body, headers=ADMIN_HEADERS)

    assert (response.status_code, response.json()["code"]) == (403, "FORBIDDEN")


class PermittedSessionlessResolver:
    """An admin with `users:manage` but no session (only test resolvers can build this)."""

    async def resolve(self, token: str) -> Actor | None:
        return Actor(id="test-admin", is_admin=True, permissions=frozenset({USERS_MANAGE}))


async def test_the_team_routes_also_need_a_session_even_with_the_permission(
    identity: Identity,
) -> None:
    async with _client(identity, PermittedSessionlessResolver()) as client:
        response = await client.get(USERS, headers=ADMIN_HEADERS)

    assert (response.status_code, response.json()["code"]) == (403, "FORBIDDEN")


async def test_the_new_operations_are_declared_with_the_cookie_scheme_and_a_403(
    identity: Identity,
) -> None:
    async with _client(identity) as client:
        paths = (await client.get("/api/v1/openapi.json")).json()["paths"]

    team = {
        ("GET", "/api/v1/admin/users"),
        ("POST", "/api/v1/admin/users/{user_id}/deactivate"),
        ("POST", "/api/v1/admin/users/{user_id}/reactivate"),
        ("GET", "/api/v1/admin/invitations"),
        ("POST", "/api/v1/admin/invitations"),
        ("DELETE", "/api/v1/admin/invitations/{invitation_id}"),
    }
    for method, path in team:
        operation = paths[path][method.lower()]
        assert {"APIKeyCookie": []} in operation["security"]
        assert "403" in operation["responses"]
    assert {
        (m.upper(), p)
        for p, item in paths.items()
        for m in item
        if "/admin/users" in p or "/admin/invitations" in p
    } == team


async def test_deactivate_declares_the_401_for_a_caller_deactivated_meanwhile(
    identity: Identity,
) -> None:
    async with _client(identity) as client:
        document = (await client.get("/api/v1/openapi.json")).json()

    operation = document["paths"]["/api/v1/admin/users/{user_id}/deactivate"]["post"]
    assert "401" in operation["responses"]
    assert "IDENTITY_ACTOR_INACTIVE" in operation["description"]


# --- users -----------------------------------------------------------------------------------


async def test_listing_users_returns_everyone_with_their_status(
    identity: Identity, world: tuple[AsyncClient, dict[str, str]]
) -> None:
    client, owner = world
    staff = identity.add_user(STAFF, role=Role.STAFF, active=False, name="Staff Uno")

    response = await client.get(USERS, headers=owner)

    assert response.status_code == 200
    body = response.json()
    assert [(u["email"], u["role"], u["is_active"]) for u in body] == [
        (OWNER, "owner", True),
        (STAFF, "staff", False),
    ]
    assert {u["id"] for u in body} >= {str(staff.id)}
    assert set(body[0]) == {"id", "email", "name", "role", "is_active", "created_at"}
    assert "password" not in response.text


async def test_deactivating_is_204_and_the_users_session_stops_working(
    identity: Identity, world: tuple[AsyncClient, dict[str, str]]
) -> None:
    client, owner = world
    staff = identity.add_user(STAFF, role=Role.STAFF)
    staff_login = await identity.sign_in(STAFF)
    assert (await client.get(ME, headers=_cookie(staff_login.token))).status_code == 200

    response = await client.post(f"{USERS}/{staff.id}/deactivate", headers=owner)

    assert (response.status_code, response.content) == (204, b"")
    assert (await client.get(ME, headers=_cookie(staff_login.token))).status_code == 401
    listed = (await client.get(USERS, headers=owner)).json()
    assert [u["is_active"] for u in listed if u["email"] == STAFF] == [False]


async def test_deactivating_yourself_is_422_with_its_code(
    identity: Identity, world: tuple[AsyncClient, dict[str, str]]
) -> None:
    client, owner = world
    me = next(iter(identity.users.by_id.values()))

    response = await client.post(f"{USERS}/{me.id}/deactivate", headers=owner)

    assert (response.status_code, response.json()["code"]) == (
        422,
        "IDENTITY_CANNOT_DEACTIVATE_SELF",
    )
    assert me.is_active


async def test_deactivating_an_unknown_user_is_404(
    world: tuple[AsyncClient, dict[str, str]],
) -> None:
    client, owner = world

    response = await client.post(f"{USERS}/{UNKNOWN_ID}/deactivate", headers=owner)

    assert (response.status_code, response.json()["code"]) == (404, "IDENTITY_USER_NOT_FOUND")


async def test_deactivating_with_a_malformed_id_is_a_validation_error(
    world: tuple[AsyncClient, dict[str, str]],
) -> None:
    client, owner = world

    response = await client.post(f"{USERS}/not-a-uuid/deactivate", headers=owner)

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_reactivating_is_204_and_the_user_can_sign_in_again(
    identity: Identity, world: tuple[AsyncClient, dict[str, str]]
) -> None:
    client, owner = world
    staff = identity.add_user(STAFF, role=Role.STAFF)
    await client.post(f"{USERS}/{staff.id}/deactivate", headers=owner)

    response = await client.post(f"{USERS}/{staff.id}/reactivate", headers=owner)

    assert (response.status_code, response.content) == (204, b"")
    login = await client.post("/api/v1/auth/login", json={"email": STAFF, "password": PASSWORD})
    assert login.status_code == 200


async def test_reactivating_an_unknown_user_is_404(
    world: tuple[AsyncClient, dict[str, str]],
) -> None:
    client, owner = world

    response = await client.post(f"{USERS}/{UNKNOWN_ID}/reactivate", headers=owner)

    assert (response.status_code, response.json()["code"]) == (404, "IDENTITY_USER_NOT_FOUND")


# --- invitations (admin side) ----------------------------------------------------------------


async def test_inviting_is_201_with_the_pending_invitation_and_no_token(
    identity: Identity, world: tuple[AsyncClient, dict[str, str]]
) -> None:
    client, owner = world

    response = await client.post(
        INVITATIONS, json={"email": " Staff@Example.TEST ", "name": "Staff Uno"}, headers=owner
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "email", "name", "created_at", "expires_at"}
    assert (body["email"], body["name"]) == (STAFF, "Staff Uno")
    assert UUID(body["id"]) in identity.invitations.by_id
    assert body["expires_at"] > body["created_at"]
    assert identity.email.sent == []  # the worker sends the email, never the request


async def test_inviting_an_existing_user_is_409(
    identity: Identity, world: tuple[AsyncClient, dict[str, str]]
) -> None:
    client, owner = world
    identity.add_user(STAFF, role=Role.STAFF)

    response = await client.post(
        INVITATIONS, json={"email": STAFF, "name": "Staff Uno"}, headers=owner
    )

    assert (response.status_code, response.json()["code"]) == (409, "IDENTITY_EMAIL_TAKEN")


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"email": "no-at", "name": "Staff Uno"}, "IDENTITY_EMAIL_INVALID"),
        ({"email": STAFF, "name": "x"}, "IDENTITY_NAME_INVALID"),
    ],
)
async def test_inviting_with_an_invalid_value_is_422_with_the_domain_code(
    world: tuple[AsyncClient, dict[str, str]], payload: dict[str, str], code: str
) -> None:
    client, owner = world

    response = await client.post(INVITATIONS, json=payload, headers=owner)

    assert (response.status_code, response.json()["code"]) == (422, code)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"email": STAFF},
        {"name": "Staff Uno"},
        {"email": "a" * 321, "name": "Staff Uno"},
        {"email": STAFF, "name": "n" * 201},
    ],
)
async def test_inviting_validates_the_payload_shape(
    world: tuple[AsyncClient, dict[str, str]], payload: dict[str, str]
) -> None:
    client, owner = world

    response = await client.post(INVITATIONS, json=payload, headers=owner)

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_listing_invitations_returns_the_pending_ones_newest_first(
    world: tuple[AsyncClient, dict[str, str]],
) -> None:
    client, owner = world
    await client.post(
        INVITATIONS, json={"email": "a@example.test", "name": "Ana Uno"}, headers=owner
    )
    kept = await client.post(
        INVITATIONS, json={"email": "b@example.test", "name": "Beto Dos"}, headers=owner
    )
    revoked = await client.post(
        INVITATIONS, json={"email": "c@example.test", "name": "Cata Tres"}, headers=owner
    )
    await client.delete(f"{INVITATIONS}/{revoked.json()['id']}", headers=owner)

    response = await client.get(INVITATIONS, headers=owner)

    assert response.status_code == 200
    assert {i["email"] for i in response.json()} == {"a@example.test", "b@example.test"}
    assert kept.json() in response.json()


async def test_revoking_an_invitation_is_204_then_404(
    world: tuple[AsyncClient, dict[str, str]],
) -> None:
    client, owner = world
    created = await client.post(
        INVITATIONS, json={"email": STAFF, "name": "Staff Uno"}, headers=owner
    )
    path = f"{INVITATIONS}/{created.json()['id']}"

    first = await client.delete(path, headers=owner)
    second = await client.delete(path, headers=owner)

    assert (first.status_code, first.content) == (204, b"")
    assert (second.status_code, second.json()["code"]) == (404, "IDENTITY_INVITATION_NOT_FOUND")
    assert (await client.get(INVITATIONS, headers=owner)).json() == []


# --- accept ----------------------------------------------------------------------------------


async def test_accepting_an_invitation_is_204_and_the_new_staff_member_can_sign_in(
    identity: Identity, world: tuple[AsyncClient, dict[str, str]]
) -> None:
    client, owner = world
    await client.post(INVITATIONS, json={"email": STAFF, "name": "Staff Uno"}, headers=owner)
    token = await _mailed_token(identity, InvitationIssued)

    response = await client.post(ACCEPT, json={"token": token, "password": NEW_PASSWORD})

    assert (response.status_code, response.content) == (204, b"")
    login = await client.post("/api/v1/auth/login", json={"email": STAFF, "password": NEW_PASSWORD})
    assert login.status_code == 200
    assert (login.json()["user"]["role"], login.json()["user"]["permissions"]) == (
        "staff",
        ["catalog:manage"],
    )
    assert (await client.get(INVITATIONS, headers=owner)).json() == []


async def test_accepting_the_same_link_twice_is_422_link_invalid(
    identity: Identity, world: tuple[AsyncClient, dict[str, str]]
) -> None:
    client, owner = world
    await client.post(INVITATIONS, json={"email": STAFF, "name": "Staff Uno"}, headers=owner)
    token = await _mailed_token(identity, InvitationIssued)
    await client.post(ACCEPT, json={"token": token, "password": NEW_PASSWORD})

    response = await client.post(ACCEPT, json={"token": token, "password": NEW_PASSWORD})

    assert (response.status_code, response.json()["code"]) == (422, "IDENTITY_LINK_INVALID")


async def test_accepting_with_an_unknown_token_is_422_link_invalid(identity: Identity) -> None:
    async with _client(identity) as client:
        response = await client.post(ACCEPT, json={"token": "nope", "password": NEW_PASSWORD})

    assert (response.status_code, response.json()["code"]) == (422, "IDENTITY_LINK_INVALID")


async def test_accepting_with_an_expired_link_is_422_link_invalid(
    identity: Identity, world: tuple[AsyncClient, dict[str, str]]
) -> None:
    client, owner = world
    await client.post(INVITATIONS, json={"email": STAFF, "name": "Staff Uno"}, headers=owner)
    token = await _mailed_token(identity, InvitationIssued)
    identity.clock.current += LINKS.invitation_ttl

    response = await client.post(ACCEPT, json={"token": token, "password": NEW_PASSWORD})

    assert (response.status_code, response.json()["code"]) == (422, "IDENTITY_LINK_INVALID")


async def test_accepting_with_a_weak_password_is_422(
    identity: Identity, world: tuple[AsyncClient, dict[str, str]]
) -> None:
    client, owner = world
    await client.post(INVITATIONS, json={"email": STAFF, "name": "Staff Uno"}, headers=owner)
    token = await _mailed_token(identity, InvitationIssued)

    response = await client.post(ACCEPT, json={"token": token, "password": "short"})

    assert (response.status_code, response.json()["code"]) == (422, "IDENTITY_PASSWORD_TOO_WEAK")


async def test_accepting_when_the_email_became_a_user_is_409(
    identity: Identity, world: tuple[AsyncClient, dict[str, str]]
) -> None:
    client, owner = world
    await client.post(INVITATIONS, json={"email": STAFF, "name": "Staff Uno"}, headers=owner)
    token = await _mailed_token(identity, InvitationIssued)
    identity.add_user(STAFF, role=Role.STAFF)

    response = await client.post(ACCEPT, json={"token": token, "password": NEW_PASSWORD})

    assert (response.status_code, response.json()["code"]) == (409, "IDENTITY_EMAIL_TAKEN")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"token": "t"},
        {"password": NEW_PASSWORD},
        {"token": "t" * 129, "password": NEW_PASSWORD},
    ],
)
async def test_accepting_validates_the_payload_shape(
    identity: Identity, payload: dict[str, str]
) -> None:
    async with _client(identity) as client:
        response = await client.post(ACCEPT, json=payload)

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


# --- password reset --------------------------------------------------------------------------


async def test_requesting_a_reset_is_202_and_queues_the_event(identity: Identity) -> None:
    identity.add_user(OWNER)
    async with _client(identity) as client:
        response = await client.post(RESET, json={"email": OWNER})

    assert response.status_code == 202
    assert [type(e) for e in identity.events.published] == [PasswordResetRequested]


async def test_requesting_a_reset_answers_with_no_body(identity: Identity) -> None:
    identity.add_user(OWNER)
    async with _client(identity) as client:
        response = await client.post(RESET, json={"email": OWNER})

    assert (response.status_code, response.content) == (202, b"")


async def test_the_answer_is_the_same_for_unknown_inactive_and_malformed_emails(
    identity: Identity,
) -> None:
    identity.add_user(OWNER)
    identity.add_user(STAFF, role=Role.STAFF, active=False)
    async with _client(identity) as client:
        known = await client.post(RESET, json={"email": OWNER})
        unknown = await client.post(RESET, json={"email": "nobody@example.test"})
        inactive = await client.post(RESET, json={"email": STAFF})
        malformed = await client.post(RESET, json={"email": "no-at"})

    for response in (unknown, inactive, malformed):
        assert (response.status_code, response.content) == (known.status_code, known.content)
    assert len(identity.events.published) == 1  # only the known account


async def test_requesting_too_often_is_429(identity: Identity) -> None:
    identity.add_user(OWNER)
    async with _client(identity) as client:
        for _ in range(POLICY.email_max_attempts):
            assert (await client.post(RESET, json={"email": OWNER})).status_code == 202

        response = await client.post(RESET, json={"email": OWNER})

    assert (response.status_code, response.json()["code"]) == (429, "IDENTITY_TOO_MANY_ATTEMPTS")


@pytest.mark.parametrize("payload", [{}, {"email": "a" * 321}])
async def test_requesting_a_reset_validates_the_payload_shape(
    identity: Identity, payload: dict[str, str]
) -> None:
    async with _client(identity) as client:
        response = await client.post(RESET, json=payload)

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_confirming_is_204_closes_every_session_and_switches_the_password(
    identity: Identity,
) -> None:
    identity.add_user(OWNER)
    first = await identity.sign_in(OWNER)
    second = await identity.sign_in(OWNER)
    async with _client(identity) as client:
        await client.post(RESET, json={"email": OWNER})
        token = await _mailed_token(identity, PasswordResetRequested)

        response = await client.post(CONFIRM, json={"token": token, "new_password": NEW_PASSWORD})

        assert (response.status_code, response.content) == (204, b"")
        for login in (first, second):
            assert (await client.get(ME, headers=_cookie(login.token))).status_code == 401
        old = await client.post("/api/v1/auth/login", json={"email": OWNER, "password": PASSWORD})
        new = await client.post(
            "/api/v1/auth/login", json={"email": OWNER, "password": NEW_PASSWORD}
        )
        assert (old.status_code, new.status_code) == (401, 200)


async def test_confirming_twice_is_422_link_invalid(identity: Identity) -> None:
    identity.add_user(OWNER)
    async with _client(identity) as client:
        await client.post(RESET, json={"email": OWNER})
        token = await _mailed_token(identity, PasswordResetRequested)
        await client.post(CONFIRM, json={"token": token, "new_password": NEW_PASSWORD})

        response = await client.post(CONFIRM, json={"token": token, "new_password": NEW_PASSWORD})

    assert (response.status_code, response.json()["code"]) == (422, "IDENTITY_LINK_INVALID")


async def test_confirming_with_a_weak_password_is_422(identity: Identity) -> None:
    identity.add_user(OWNER)
    async with _client(identity) as client:
        await client.post(RESET, json={"email": OWNER})
        token = await _mailed_token(identity, PasswordResetRequested)

        response = await client.post(CONFIRM, json={"token": token, "new_password": "short"})

    assert (response.status_code, response.json()["code"]) == (422, "IDENTITY_PASSWORD_TOO_WEAK")


async def test_confirming_with_an_unknown_token_is_422_link_invalid(identity: Identity) -> None:
    async with _client(identity) as client:
        response = await client.post(CONFIRM, json={"token": "nope", "new_password": NEW_PASSWORD})

    assert (response.status_code, response.json()["code"]) == (422, "IDENTITY_LINK_INVALID")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"token": "t"},
        {"new_password": NEW_PASSWORD},
        {"token": "t" * 129, "new_password": NEW_PASSWORD},
    ],
)
async def test_confirming_validates_the_payload_shape(
    identity: Identity, payload: dict[str, str]
) -> None:
    async with _client(identity) as client:
        response = await client.post(CONFIRM, json=payload)

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_the_new_public_routes_need_no_cookie(identity: Identity) -> None:
    async with _client(identity) as client:
        statuses = [
            (await client.post(ACCEPT, json={"token": "x", "password": NEW_PASSWORD})).status_code,
            (await client.post(RESET, json={"email": OWNER})).status_code,
            (
                await client.post(CONFIRM, json={"token": "x", "new_password": NEW_PASSWORD})
            ).status_code,
        ]

    assert 401 not in statuses and 403 not in statuses
