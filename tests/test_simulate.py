"""The simulator: a reproducible plan, the device retry rule, and a full in-process run."""

import json
import random
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from scripts.simulate import Attempt, DevicePlan, build_plan, run_simulation
from tests.helpers import (
    TEST_MASTER_SECRET,
    assert_invariants,
    count_devices,
    enable_backdating,
    get_rollup,
    set_clock,
)

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)


def test_same_seed_gives_the_same_plan():
    plan = build_plan(seed=5, devices=300, days=30, now=NOW)

    assert plan == build_plan(seed=5, devices=300, days=30, now=NOW)
    assert plan != build_plan(seed=6, devices=300, days=30, now=NOW)


def test_plan_is_valid():
    plan = build_plan(seed=5, devices=300, days=30, now=NOW)

    assert len({d.mac for d in plan}) == 300  # unique MACs
    assert len({d.model for d in plan}) == 5
    resenders = 0
    for device in plan:
        times = [a.at for a in device.attempts]
        assert times == sorted(set(times)), "attempts must be strictly increasing"
        assert times[0] >= NOW - timedelta(days=30)
        assert times[-1] < NOW  # never in the future
        resenders += len(times) > 1
    assert 0 < resenders < 300


def _device(n: int) -> DevicePlan:
    return DevicePlan(
        mac=f"00:1B:44:00:00:{n:02X}",
        model="X100",
        os_version=None,
        firmware_version=None,
        region=None,
        attempts=(Attempt(at=NOW - timedelta(days=1), device_time=None),),
    )


async def test_device_retries_transient_errors_and_stops_on_other_4xx():
    good, bad = _device(1), _device(2)
    calls = {good.mac: 0, bad.mac: 0}

    def handler(request: httpx.Request) -> httpx.Response:
        mac = json.loads(request.content)["device_id"]
        calls[mac] += 1
        if mac == bad.mac:
            return httpx.Response(422, json={"detail": "nope"})
        # The good device meets a 503, a network error and a 429, then succeeds.
        if calls[mac] == 1:
            return httpx.Response(503)
        if calls[mac] == 2:
            raise httpx.ConnectError("connection refused")
        if calls[mac] == 3:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(
            201,
            json={
                "status": "activated",
                "first_activated_at": request.headers["X-Simulated-Time"],
            },
        )

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    async with client:
        result = await run_simulation(
            client,
            [good, bad],
            TEST_MASTER_SECRET,
            concurrency=2,
            jitter=random.Random(0),
            sleep=fake_sleep,
        )

    assert calls == {good.mac: 4, bad.mac: 1}  # the 4xx was not retried
    assert (result.sent, result.retries) == (5, 3)
    assert (result.new, result.completed, result.failures) == (1, 1, 1)
    assert len(result.problems) == 1 and "422" in result.problems[0]
    assert not result.ok
    # Full jitter inside 0.5 s then 1 s; the 429 waits Retry-After plus jitter.
    assert len(sleeps) == 3
    assert 0 <= sleeps[0] <= 0.5
    assert 0 <= sleeps[1] <= 1.0
    assert 2 <= sleeps[2] <= 3


async def test_run_stops_when_the_server_ignores_the_simulated_time():
    def handler(request: httpx.Request) -> httpx.Response:
        # A server with backdating off answers with its own clock.
        return httpx.Response(
            201, json={"status": "activated", "first_activated_at": NOW.isoformat()}
        )

    plan = [_device(n) for n in range(1, 20)]
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    async with client:
        result = await run_simulation(client, plan, TEST_MASTER_SECRET, concurrency=1)

    assert result.setup_error is not None and "ALLOW_BACKDATING" in result.setup_error
    assert result.sent == 1  # stopped at once


async def test_simulator_runs_in_process_and_a_second_run_counts_nothing_new(client):
    enable_backdating()
    set_clock(NOW)  # the server's "now"; every planned time is before it
    plan = build_plan(seed=3, devices=200, days=30, now=NOW)
    messages = sum(len(d.attempts) for d in plan)
    assert messages > 200  # some devices resend

    first = await run_simulation(client, plan, TEST_MASTER_SECRET, concurrency=20)

    assert first.problems == [] and first.setup_error is None
    assert first.ok
    assert first.unique == first.new == 200
    assert first.existing == 0
    assert first.duplicates == messages - 200
    assert await count_devices() == 200
    await assert_invariants()
    assert len({day for day, _model in await get_rollup()}) > 1

    # Same seed again: every device already exists, so nothing is counted new.
    second = await run_simulation(client, plan, TEST_MASTER_SECRET, concurrency=20)

    assert second.problems == [] and second.setup_error is None
    assert second.ok
    assert second.new == 0
    assert second.existing == 200
    assert second.duplicates == messages
    assert await count_devices() == 200
    await assert_invariants()
