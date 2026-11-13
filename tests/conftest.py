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
from tests.helpers import TEST_MASTER_SECRET


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


def _use_test_settings() -> None:
    """Fixed test values that win over .env (load_settings never overrides the environment).

    The helper signs with TEST_MASTER_SECRET, so the app must use the same
    one. The rate limit is set so high that the concurrency tests are never
    throttled; the 429 test installs its own low limiter.
    """
    os.environ["MASTER_SECRET"] = TEST_MASTER_SECRET
    os.environ["RATE_LIMIT_PER_MINUTE"] = "1000000"
    # A local .env must never change test behavior: backdating is off unless
    # a test turns it on (tests.helpers.enable_backdating).
    os.environ["ENV"] = "development"
    os.environ["ALLOW_BACKDATING"] = "false"


_use_test_database()
_use_test_settings()


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
