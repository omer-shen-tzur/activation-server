"""The simulator: a reproducible plan, the device retry rule, and a full in-process run."""

import json
import random
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from scripts import simulate
from scripts.simulate import MODELS, Attempt, DevicePlan, build_plan, run_simulation
from tests.helpers import (
    DASHBOARD_AUTH,
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
    assert len({d.model for d in plan}) == len(MODELS) == 8
    resenders = 0
    for device in plan:
        assert device.os_version is None or device.os_version in (
            "Android 12", "Android 13", "Android 14", "Android 15"
        )
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

    first = await run_simulation(
        client, plan, TEST_MASTER_SECRET, concurrency=20, dashboard_auth=DASHBOARD_AUTH
    )

    assert first.problems == [] and first.setup_error is None
    assert first.ok
    assert first.unique == first.new == 200
    assert first.existing == 0
    assert first.duplicates == messages - 200
    # The stats cross-check ran and agrees with the devices' own counts.
    assert first.stats_problem is None
    assert (first.delta_activated, first.delta_duplicates) == (200, messages - 200)
    assert await count_devices() == 200
    await assert_invariants()
    assert len({day for day, _model in await get_rollup()}) > 1

    # Same seed again: every device already exists, so nothing is counted new.
    second = await run_simulation(
        client, plan, TEST_MASTER_SECRET, concurrency=20, dashboard_auth=DASHBOARD_AUTH
    )

    assert second.problems == [] and second.setup_error is None
    assert second.ok
    assert second.new == 0
    assert second.existing == 200
    assert second.duplicates == messages
    assert second.stats_problem is None
    assert (second.delta_activated, second.delta_duplicates) == (0, messages)
    assert await count_devices() == 200
    await assert_invariants()


def _stats_server(*, activated_after: int, stats_status: int = 200) -> httpx.MockTransport:
    """A fake server: every activation is a 201, /api/v1/stats reports fixed numbers.

    The first stats read says 0/0, the second says `activated_after` activated.
    """
    stats_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal stats_reads
        if request.url.path == "/api/v1/stats":
            stats_reads += 1
            if stats_status != 200:
                return httpx.Response(stats_status)
            activated = 0 if stats_reads == 1 else activated_after
            return httpx.Response(
                200, json={"all_time": {"activated": activated, "duplicates": 0}}
            )
        return httpx.Response(
            201,
            json={
                "status": "activated",
                "first_activated_at": request.headers["X-Simulated-Time"],
            },
        )

    return httpx.MockTransport(handler)


async def test_stats_cross_check_passes_when_the_numbers_match():
    async with httpx.AsyncClient(
        transport=_stats_server(activated_after=1), base_url="http://test"
    ) as client:
        result = await run_simulation(
            client, [_device(1)], TEST_MASTER_SECRET, dashboard_auth=DASHBOARD_AUTH
        )

    assert result.stats_problem is None and result.ok
    assert (result.delta_activated, result.delta_duplicates) == (1, 0)


async def test_stats_cross_check_fails_clearly_on_a_mismatch():
    """The server's totals moved by 5, but this run only got one 201."""
    async with httpx.AsyncClient(
        transport=_stats_server(activated_after=5), base_url="http://test"
    ) as client:
        result = await run_simulation(
            client, [_device(1)], TEST_MASTER_SECRET, dashboard_auth=DASHBOARD_AUTH
        )

    assert result.delta_activated == 5
    assert result.stats_problem is not None
    assert "other traffic" in result.stats_problem.lower()
    assert not result.ok


async def test_wrong_dashboard_credentials_stop_the_run_before_sending():
    async with httpx.AsyncClient(
        transport=_stats_server(activated_after=0, stats_status=401), base_url="http://test"
    ) as client:
        result = await run_simulation(
            client, [_device(1)], TEST_MASTER_SECRET, dashboard_auth=("nope", "nope")
        )

    assert result.setup_error is not None and "401" in result.setup_error
    assert result.sent == 0
    assert not result.ok


def test_simulator_exits_1_without_dashboard_credentials(monkeypatch, capsys):
    # Set to empty (not deleted): main() loads .env without overriding
    # variables that already exist, so a local .env cannot fill them back in.
    monkeypatch.setenv("DASHBOARD_USER", "")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "")

    assert simulate.main(["--devices", "1"]) == 1
    assert "DASHBOARD_PASSWORD" in capsys.readouterr().err
