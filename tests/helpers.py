"""Shared test helpers: requests, clock, database reads and the invariants check.

All database access goes through the app's own pool (app.state.pool), with
parameterized SQL only.
"""

import json
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone

import httpx
from psycopg.rows import dict_row

from app.clock import utc_now
from app.main import app
from app.signing import device_key, sign

# conftest.py puts this in MASTER_SECRET (overriding .env), so the test
# signatures match what the app expects.
TEST_MASTER_SECRET = "test-master-secret"

# Fixed instant used when a test does not care about the time.
NOON_UTC = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)

MAC = "00:1A:2B:3C:4D:5E"  # universally administered, unicast: a valid device_id


def valid_payload(**overrides) -> dict:
    """A valid activation body; keyword arguments replace or add fields."""
    return {"device_id": MAC, "model": "X100", **overrides}


def mac_n(n: int) -> str:
    """A distinct valid MAC per n (0-255)."""
    return f"00:1A:2B:00:00:{n:02X}"


AUTO = object()  # post_activation's default: sign like a real device


def signature_for(mac: str, body: bytes) -> str:
    """The X-Signature a device with this MAC sends for these exact bytes."""
    return sign(device_key(TEST_MASTER_SECRET, mac), body)


async def post_activation(
    client: httpx.AsyncClient,
    payload: dict | None = None,
    *,
    body: bytes | AsyncIterator[bytes] | None = None,
    signature: str | bytes | None | object = AUTO,
    sign_with_mac: str | None = None,
) -> httpx.Response:
    """The ONE way tests send an activation.

    By default the payload is serialized to bytes here (sent as content=,
    never json=) and signed over those exact bytes with the key of
    payload["device_id"], like a real device. If device_id is missing or not
    a valid MAC the request goes unsigned; validation rejects it first anyway.

    Options for the security tests:
      body=           send these bytes (or this async stream, which goes out
                      chunked without Content-Length) instead of the payload
      signature=      send this X-Signature instead; None sends no header.
                      A stream is never signed automatically.
      sign_with_mac=  sign with this MAC's key instead of payload["device_id"]
    """
    content = body if body is not None else json.dumps(payload).encode("utf-8")
    headers: dict[str, str | bytes] = {"Content-Type": "application/json"}

    if signature is AUTO:
        mac = sign_with_mac if sign_with_mac is not None else (payload or {}).get("device_id")
        if isinstance(content, bytes) and isinstance(mac, str):
            try:
                headers["X-Signature"] = signature_for(mac, content)
            except ValueError:
                pass  # not a valid MAC: send unsigned
    elif signature is not None:
        headers["X-Signature"] = signature

    return await client.post("/api/v1/activations", content=content, headers=headers)


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
