"""Stage 9: listen address, GET / redirect, and the renamed dashboard label."""

import pytest

from app.__main__ import listen_host
from tests.helpers import DASHBOARD_AUTH

pytestmark = pytest.mark.anyio

LABEL = "Repeat requests (not counted)"


def test_host_defaults_to_loopback(monkeypatch):
    monkeypatch.delenv("HOST", raising=False)
    assert listen_host() == "127.0.0.1"


def test_host_can_be_set(monkeypatch):
    monkeypatch.setenv("HOST", "0.0.0.0")
    assert listen_host() == "0.0.0.0"


async def test_root_redirects_to_dashboard(client):
    response = await client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/dashboard"


async def test_dashboard_uses_new_label(client):
    html = (await client.get("/dashboard", auth=DASHBOARD_AUTH)).text
    script = (await client.get("/dashboard/app.js", auth=DASHBOARD_AUTH)).text
    assert LABEL in html
    assert LABEL in script
