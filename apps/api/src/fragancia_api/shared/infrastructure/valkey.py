from redis.asyncio import Redis


class ValkeyHealth:
    """Readiness check for Valkey (the job queue)."""

    def __init__(self, url: str) -> None:
        self._client = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)

    async def ping(self) -> None:
        await self._client.ping()

    async def close(self) -> None:
        await self._client.aclose()
