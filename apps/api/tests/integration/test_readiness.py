import pytest
from httpx import ASGITransport, AsyncClient

from fragancia_api.container import Container
from fragancia_api.main.http import build_app

pytestmark = pytest.mark.integration


async def test_ready_with_real_database_and_valkey(container: Container) -> None:
    app = build_app(container.services, container.routers)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"] == {"database": "ok", "valkey": "ok"}
