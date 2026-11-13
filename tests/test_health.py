import pytest

pytestmark = pytest.mark.anyio


async def test_health_ok(client):
    """/health answers 200 {"status": "ok"} when the database is up."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
