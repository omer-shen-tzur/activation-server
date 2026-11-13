"""Protection of POST /api/v1/activations: signature, body size limit, rate limit, config.

Every rejected request must leave the database untouched.
"""

import json

import pytest

from app.config import ConfigError, load_settings
from app.main import app
from app.ratelimit import RateLimiter
from tests.helpers import (
    MAC,
    NOON_UTC,
    assert_invariants,
    count_devices,
    get_device,
    get_rollup,
    mac_n,
    post_activation,
    set_clock,
    signature_for,
    valid_payload,
)

pytestmark = pytest.mark.anyio

# A valid, correctly signable body padded with JSON whitespace to over 4 KB:
# without the size limit it would be accepted (201).
OVERSIZED_BODY = json.dumps(valid_payload()).encode("utf-8") + b" " * (5 * 1024)


async def assert_nothing_written() -> None:
    assert await count_devices() == 0
    assert await get_rollup() == {}


async def test_missing_signature_is_rejected(client):
    response = await post_activation(client, valid_payload(), signature=None)

    assert response.status_code == 401
    await assert_nothing_written()


@pytest.mark.parametrize(
    "bad_signature",
    ["0" * 64, "not-a-signature", "", b"\xe9" * 64],
    ids=["zeros", "not-hex", "empty", "non-ascii"],
)
async def test_wrong_signature_is_rejected(client, bad_signature):
    """Any wrong value is 401, including non-ASCII bytes (never a 500)."""
    response = await post_activation(client, valid_payload(), signature=bad_signature)

    assert response.status_code == 401
    await assert_nothing_written()


async def test_signature_with_another_devices_key_is_rejected(client):
    """A key taken from one device cannot sign for another device."""
    response = await post_activation(client, valid_payload(), sign_with_mac=mac_n(1))

    assert response.status_code == 401
    await assert_nothing_written()


async def test_signature_covers_the_exact_bytes_received(client):
    """Unusual whitespace and key order, signed as sent, is accepted.

    A server that re-serialized the body before checking would compute a
    different signature and reject this.
    """
    set_clock(NOON_UTC)
    raw = b'{\n  "model" :\t"X100" ,\r\n  "device_id":"00-1a-2b-3c-4d-5e"   }\n'
    assert json.dumps(json.loads(raw)).encode() != raw  # really not canonical

    response = await post_activation(client, body=raw, sign_with_mac=MAC)

    assert response.status_code == 201
    assert (await get_device(MAC))["model"] == "X100"
    await assert_invariants()


async def test_oversized_body_with_content_length_is_rejected(client):
    response = await post_activation(client, body=OVERSIZED_BODY, sign_with_mac=MAC)

    assert response.request.headers["content-length"] == str(len(OVERSIZED_BODY))
    assert response.status_code == 413
    await assert_nothing_written()


async def test_oversized_streamed_body_without_content_length_is_rejected(client):
    """A chunked upload has no Content-Length; the limit holds on the bytes read."""

    async def chunks():
        for start in range(0, len(OVERSIZED_BODY), 1024):
            yield OVERSIZED_BODY[start : start + 1024]

    response = await post_activation(
        client, body=chunks(), signature=signature_for(MAC, OVERSIZED_BODY)
    )

    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    await assert_nothing_written()


@pytest.mark.parametrize("raw", [b"not json", b"\xff\xfe{}"], ids=["not-json", "not-utf8"])
async def test_body_that_is_not_json_is_rejected(client, raw):
    """422, not a 500 from serializing the error (the input echoes raw bytes)."""
    response = await post_activation(client, body=raw, sign_with_mac=MAC)

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "json_invalid"
    await assert_nothing_written()


async def test_burst_over_the_rate_limit_is_rejected(client):
    """3 per minute: 3 requests pass, the 4th gets 429 and is not counted."""
    set_clock(NOON_UTC)
    app.state.rate_limiter = RateLimiter(3)

    allowed = [await post_activation(client, valid_payload()) for _ in range(3)]
    blocked = await post_activation(client, valid_payload())

    assert [r.status_code for r in allowed] == [201, 200, 200]
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert (await get_device(MAC))["attempt_count"] == 3
    await assert_invariants()


def test_startup_fails_without_master_secret(monkeypatch):
    # Set to empty (not deleted), so load_dotenv cannot fill it from .env.
    monkeypatch.setenv("MASTER_SECRET", "")

    with pytest.raises(ConfigError, match="MASTER_SECRET"):
        load_settings()
