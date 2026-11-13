"""Test setup: point the app at the test database, run the real lifespan, start each test empty."""

import asyncio
import os
import sys
from urllib.parse import urlparse

import httpx
import pytest
from dotenv import load_dotenv
from httpx import ASGITransport

from app.config import ENV_FILE
from app.main import app


def _db_name(url: str) -> str:
    return urlparse(url).path.lstrip("/")


def _use_test_database() -> None:
    """Make the app connect to TEST_DATABASE_URL, or stop pytest.

    Runs when conftest is imported, before any test and before the app's
    lifespan reads DATABASE_URL (load_settings is called inside the lifespan).
    """
    load_dotenv(ENV_FILE, override=False)
    test_url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not test_url:
        pytest.exit(
            "TEST_DATABASE_URL is not set. Copy .env.example to .env "
            "(it defines TEST_DATABASE_URL), or set it in the environment.",
            returncode=2,
        )

    # Safety guard: the tests TRUNCATE tables, so they must never run against
    # dev or simulator data. The name must say "_test" and must not be the
    # database the app normally uses.
    test_db = _db_name(test_url)
    dev_db = _db_name(os.environ.get("DATABASE_URL", ""))
    if not test_db.endswith("_test"):
        pytest.exit(
            f"Refusing to run: TEST_DATABASE_URL points at database {test_db!r}, "
            "whose name does not end with '_test'. Tests truncate tables.",
            returncode=2,
        )
    if test_db == dev_db:
        pytest.exit(
            f"Refusing to run: TEST_DATABASE_URL and DATABASE_URL both use database "
            f"{test_db!r}. Tests truncate tables and must never touch dev data.",
            returncode=2,
        )

    os.environ["DATABASE_URL"] = test_url


_use_test_database()


@pytest.fixture
def anyio_backend():
    # psycopg's async mode does not work on Windows' default ProactorEventLoop
    # (same reason as app/__main__.py), so the tests use a SelectorEventLoop.
    if sys.platform == "win32":
        return ("asyncio", {"loop_factory": asyncio.SelectorEventLoop})
    return ("asyncio", {})


@pytest.fixture
async def client(anyio_backend):
    """An HTTP client on the real app, with its lifespan running and empty tables."""
    async with app.router.lifespan_context(app):
        async with app.state.pool.connection() as conn:
            await conn.execute("TRUNCATE devices, daily_activations")
        transport = ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                yield c
        finally:
            app.dependency_overrides.clear()
