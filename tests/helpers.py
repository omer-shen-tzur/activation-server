"""Shared test helpers: requests, clock, database reads and the invariants check.

All database access goes through the app's own pool (app.state.pool), with
parameterized SQL only.
"""

import json
from datetime import date, datetime, timezone

import httpx
from psycopg.rows import dict_row

from app.clock import utc_now
from app.main import app

# Fixed instant used when a test does not care about the time.
NOON_UTC = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)

MAC = "00:1A:2B:3C:4D:5E"  # universally administered, unicast: a valid device_id


def valid_payload(**overrides) -> dict:
    """A valid activation body; keyword arguments replace or add fields."""
    return {"device_id": MAC, "model": "X100", **overrides}


def mac_n(n: int) -> str:
    """A distinct valid MAC per n (0-255)."""
    return f"00:1A:2B:00:00:{n:02X}"


async def post_activation(client: httpx.AsyncClient, payload: dict) -> httpx.Response:
    """The ONE way tests send an activation.

    The body is serialized to bytes here and sent as content=, never json=.
    In step 5 this helper will sign these exact bytes (X-Signature), so the
    tests themselves stay unchanged.
    """
    body = json.dumps(payload).encode("utf-8")
    return await client.post(
        "/api/v1/activations",
        content=body,
        headers={"Content-Type": "application/json"},
    )


def set_clock(instant: datetime) -> None:
    """Make the app believe it is `instant` (must be an aware UTC datetime)."""

    async def fixed_now() -> datetime:
        return instant

    app.dependency_overrides[utc_now] = fixed_now


async def get_device(device_id: str) -> dict | None:
    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM devices WHERE device_id = %s", (device_id,))
            return await cur.fetchone()


async def count_devices() -> int:
    async with app.state.pool.connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM devices")
        return (await cur.fetchone())[0]


async def get_rollup() -> dict[tuple[date, str], tuple[int, int]]:
    """{(day, model): (activations, duplicates)} for every rollup row."""
    async with app.state.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT day, model, activations, duplicates FROM daily_activations"
        )
        return {(day, model): (act, dup) for day, model, act, dup in await cur.fetchall()}


async def assert_invariants() -> None:
    """SUM(activations) = COUNT(devices) and SUM(duplicates) = SUM(attempt_count - 1)."""
    async with app.state.pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT
                (SELECT COALESCE(SUM(activations), 0) FROM daily_activations),
                (SELECT COUNT(*) FROM devices),
                (SELECT COALESCE(SUM(duplicates), 0) FROM daily_activations),
                (SELECT COALESCE(SUM(attempt_count - 1), 0) FROM devices)
            """
        )
        activations, devices, duplicates, extra_attempts = await cur.fetchone()
    assert activations == devices, f"SUM(activations)={activations} != COUNT(devices)={devices}"
    assert duplicates == extra_attempts, (
        f"SUM(duplicates)={duplicates} != SUM(attempt_count-1)={extra_attempts}"
    )
