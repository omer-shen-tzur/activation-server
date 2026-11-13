"""Core behaviour of POST /api/v1/activations, against a real Postgres."""

import asyncio
from datetime import date, datetime, timezone

import pytest

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
    valid_payload,
)

pytestmark = pytest.mark.anyio

NOON_DAY = date(2026, 11, 13)  # NOON_UTC is 14:00 in Israel, the same day


async def test_new_device_is_activated(client):
    """A first message returns 201 and creates the device and its rollup row."""
    set_clock(NOON_UTC)
    response = await post_activation(client, valid_payload())

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "activated"
    assert datetime.fromisoformat(body["first_activated_at"]) == NOON_UTC
    device = await get_device(MAC)
    assert device["model"] == "X100"
    assert device["attempt_count"] == 1
    assert await get_rollup() == {(NOON_DAY, "X100"): (1, 0)}
    await assert_invariants()


async def test_same_device_again_is_a_duplicate(client):
    """A second message returns 200 and only bumps attempt_count and duplicates."""
    set_clock(NOON_UTC)
    first = await post_activation(client, valid_payload())
    second = await post_activation(client, valid_payload())

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["status"] == "already_activated"
    assert second.json()["first_activated_at"] == first.json()["first_activated_at"]
    assert await count_devices() == 1
    assert (await get_device(MAC))["attempt_count"] == 2
    assert await get_rollup() == {(NOON_DAY, "X100"): (1, 1)}
    await assert_invariants()


async def test_retry_keeps_the_first_snapshot(client):
    """A retry never overwrites stored fields; only last_seen_at moves."""
    first_time = NOON_UTC
    second_time = datetime(2026, 11, 14, 8, 0, 0, tzinfo=timezone.utc)
    set_clock(first_time)
    await post_activation(
        client,
        valid_payload(
            os_version="14.1",
            firmware_version="1.0.0",
            region="IL",
            device_time="2026-11-13T11:59:00Z",
        ),
    )
    set_clock(second_time)
    await post_activation(
        client,
        valid_payload(
            os_version="15.0",
            firmware_version="2.0.0",
            region="US",
            device_time="2026-11-14T07:59:00Z",
        ),
    )

    device = await get_device(MAC)
    assert device["os_version"] == "14.1"
    assert device["firmware_version"] == "1.0.0"
    assert device["region"] == "IL"
    assert device["device_reported_at"] == datetime(2026, 11, 13, 11, 59, tzinfo=timezone.utc)
    assert device["first_activated_at"] == first_time
    assert device["last_seen_at"] == second_time
    await assert_invariants()


async def test_concurrent_requests_for_one_device(client):
    """20 simultaneous requests for one device: exactly one is counted."""
    set_clock(NOON_UTC)
    responses = await asyncio.gather(
        *(post_activation(client, valid_payload()) for _ in range(20))
    )

    codes = sorted(r.status_code for r in responses)
    assert codes == [200] * 19 + [201]
    assert await count_devices() == 1
    assert (await get_device(MAC))["attempt_count"] == 20
    assert await get_rollup() == {(NOON_DAY, "X100"): (1, 19)}
    await assert_invariants()


async def test_concurrent_requests_for_different_devices(client):
    """20 simultaneous first activations of one model are all counted."""
    set_clock(NOON_UTC)
    responses = await asyncio.gather(
        *(post_activation(client, valid_payload(device_id=mac_n(i))) for i in range(20))
    )

    assert [r.status_code for r in responses] == [201] * 20
    assert await count_devices() == 20
    assert await get_rollup() == {(NOON_DAY, "X100"): (20, 0)}
    await assert_invariants()


async def test_duplicate_counts_under_the_stored_model(client):
    """A retry claiming another model is counted under the model stored at first activation."""
    set_clock(NOON_UTC)
    await post_activation(client, valid_payload(model="X100"))
    response = await post_activation(client, valid_payload(model="Z999"))

    assert response.status_code == 200
    assert (await get_device(MAC))["model"] == "X100"
    assert await get_rollup() == {(NOON_DAY, "X100"): (1, 1)}  # no "Z999" row
    await assert_invariants()


@pytest.mark.parametrize(
    "instant, expected_day",
    [
        (datetime(2026, 11, 13, 23, 30, tzinfo=timezone.utc), date(2026, 11, 14)),
        # Winter (UTC+2): Israeli midnight is 22:00 UTC.
        (datetime(2026, 11, 13, 21, 59, 59, tzinfo=timezone.utc), date(2026, 11, 13)),
        (datetime(2026, 11, 13, 22, 0, 0, tzinfo=timezone.utc), date(2026, 11, 14)),
    ],
    ids=["23:30Z", "21:59:59Z-last-second", "22:00:00Z-first-second"],
)
async def test_rollup_day_is_the_israeli_day(client, instant, expected_day):
    """The rollup day is the Asia/Jerusalem calendar day of the receive time."""
    set_clock(instant)
    response = await post_activation(client, valid_payload())

    assert response.status_code == 201
    assert await get_rollup() == {(expected_day, "X100"): (1, 0)}
    await assert_invariants()


async def test_mac_forms_are_one_device(client):
    """aa-bb-..., AA:BB:... and 12 hex digits are the same device, stored in colon form."""
    set_clock(NOON_UTC)
    # Not AA:BB:...: first octet 0xAA has the locally administered bit set (422).
    r1 = await post_activation(client, valid_payload(device_id="00-1a-2b-3c-4d-01"))
    r2 = await post_activation(client, valid_payload(device_id="00:1A:2B:3C:4D:01"))
    r3 = await post_activation(client, valid_payload(device_id="001a2b3c4d01"))

    assert [r.status_code for r in (r1, r2, r3)] == [201, 200, 200]
    assert await count_devices() == 1
    assert (await get_device("00:1A:2B:3C:4D:01"))["attempt_count"] == 3
    await assert_invariants()


@pytest.mark.parametrize(
    "bad_mac",
    [
        "00:00:00:00:00:00",  # all zeros: firmware could not read its MAC
        "FF:FF:FF:FF:FF:FF",  # broadcast
        "01:00:5E:00:00:01",  # multicast
        "02:11:22:33:44:55",  # locally administered (randomized)
        "00:1A:2B:3C:4D",  # too short
        "00:1A-2B:3C:4D:5E",  # mixed separators (otherwise valid)
        "ZZ:1A:2B:3C:4D:5E",  # not hex
    ],
)
async def test_bad_mac_is_rejected(client, bad_mac):
    """An unusable device_id gets 422 and nothing is written."""
    response = await post_activation(client, valid_payload(device_id=bad_mac))

    assert response.status_code == 422
    assert await count_devices() == 0
    assert await get_rollup() == {}
    await assert_invariants()


@pytest.mark.parametrize(
    "payload",
    [
        valid_payload(extra_field="x"),
        {"device_id": MAC},  # model missing
        valid_payload(model="   "),
        valid_payload(model="x" * 65),
        valid_payload(model="X\x00100"),
        valid_payload(region="I\x00L"),
    ],
    ids=["extra-field", "missing-model", "blank-model", "model-too-long", "nul-in-model", "nul-in-region"],
)
async def test_invalid_body_is_rejected(client, payload):
    """A body that breaks the schema gets 422 and nothing is written."""
    response = await post_activation(client, payload)

    assert response.status_code == 422
    assert await count_devices() == 0
    assert await get_rollup() == {}
    await assert_invariants()


async def test_model_is_stored_stripped(client):
    """Surrounding whitespace is removed from model before it is stored and counted."""
    set_clock(NOON_UTC)
    response = await post_activation(client, valid_payload(model="  X100  "))

    assert response.status_code == 201
    assert (await get_device(MAC))["model"] == "X100"
    assert await get_rollup() == {(NOON_DAY, "X100"): (1, 0)}
    await assert_invariants()


async def test_script_like_model_is_plain_data(client):
    """A model that looks like HTML is stored byte-for-byte, not interpreted."""
    name = "<script>alert(1)</script>"
    set_clock(NOON_UTC)
    response = await post_activation(client, valid_payload(model=name))

    assert response.status_code == 201
    assert (await get_device(MAC))["model"] == name
    assert await get_rollup() == {(NOON_DAY, name): (1, 0)}
    await assert_invariants()
