import hmac

from fragancia_api.shared.application.actor import Actor


class DevTokenActorResolver:
    """DEVELOPMENT ONLY: one static admin token from `ADMIN_DEV_TOKEN`.

    Settings refuse to start in production with that variable set. The identity module
    replaces this adapter behind the same `ActorResolver` port.
    """

    def __init__(self, admin_token: str | None) -> None:
        self._admin_token = admin_token

    async def resolve(self, token: str) -> Actor | None:
        if self._admin_token and hmac.compare_digest(token.encode(), self._admin_token.encode()):
            return Actor(id="dev-admin", is_admin=True)
        return None
