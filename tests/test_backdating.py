"""Dev-only backdating (X-Simulated-Time) and the settings that guard it."""

import dataclasses
from datetime import date, datetime, timezone

import pytest

from app.config import ConfigError, load_settings
from app.main import app
from app.ratelimit import RateLimiter
from tests.helpers import (
    MAC,
    NOON_UTC,
    assert_invariants,
    count_devices,
    enable_backdating,
    get_device,
    get_rollup,
    post_activation,
    set_clock,
    valid_payload,
)

pytestmark = pytest.mark.anyio

# 23:30 UTC on Nov 1 2026 is 01:30 on Nov 2 in Israel (winter time, UTC+2).
# Written with a +02:00 offset to also prove the header is converted to UTC.
BACKDATED_HEADER = "2026-11-02T01:30:00+02:00"
BACKDATED_UTC = datetime(2026, 11, 1, 23, 30, tzinfo=timezone.utc)
BACKDATED_ISRAEL_DAY = date(2026, 11, 2)


async def test_header_is_ignored_when_backdating_is_off(client):
    set_clock(NOON_UTC)

    response = await post_activation(
        client, valid_payload(), extra_headers={"X-Simulated-Time": BACKDATED_HEADER}
    )

    assert response.status_code == 201
    assert (await get_device(MAC))["first_activated_at"] == NOON_UTC
    await assert_invariants()


async def test_header_is_ignored_in_production_even_if_the_flag_is_on(client):
    """load_settings refuses this combination; the endpoint is safe even so."""
    app.state.settings = dataclasses.replace(
        app.state.settings, allow_backdating=True, env="production"
    )
    set_clock(NOON_UTC)

    # Even a header that would be a 422 when enabled is silently ignored.
    response = await post_activation(
        client, valid_payload(), extra_headers={"X-Simulated-Time": "garbage"}
    )

    assert response.status_code == 201
    assert (await get_device(MAC))["first_activated_at"] == NOON_UTC


async def test_header_is_honored_when_backdating_is_on(client):
    enable_backdating()
    set_clock(NOON_UTC)

    response = await post_activation(
        client, valid_payload(), extra_headers={"X-Simulated-Time": BACKDATED_HEADER}
    )

    assert response.status_code == 201
    assert datetime.fromisoformat(response.json()["first_activated_at"]) == BACKDATED_UTC
    device = await get_device(MAC)
    assert device["first_activated_at"] == BACKDATED_UTC
    assert device["last_seen_at"] == BACKDATED_UTC
    assert await get_rollup() == {(BACKDATED_ISRAEL_DAY, "X100"): (1, 0)}
    await assert_invariants()


async def test_no_header_uses_the_clock_when_backdating_is_on(client):
    enable_backdating()
    set_clock(NOON_UTC)

    response = await post_activation(client, valid_payload())

    assert response.status_code == 201
    assert (await get_device(MAC))["first_activated_at"] == NOON_UTC


@pytest.mark.parametrize(
    "header",
    [
        "2026-11-13T12:00:01+00:00",  # one second after "now"
        "2027-01-01T00:00:00Z",  # in the future
        "2026-11-01T08:00:00",  # naive: no offset
        "2026-11-01",  # a date only: naive
        "yesterday",  # not a datetime
        "",  # empty
    ],
    ids=["just-future", "future", "naive", "date-only", "garbage", "empty"],
)
async def test_bad_header_is_rejected_when_backdating_is_on(client, header):
    enable_backdating()
    set_clock(NOON_UTC)

    response = await post_activation(
        client, valid_payload(), extra_headers={"X-Simulated-Time": header}
    )

    assert response.status_code == 422
    assert "X-Simulated-Time" in response.json()["detail"]
    assert await count_devices() == 0
    assert await get_rollup() == {}


async def test_header_equal_to_now_is_accepted(client):
    enable_backdating()
    set_clock(NOON_UTC)

    response = await post_activation(
        client, valid_payload(), extra_headers={"X-Simulated-Time": NOON_UTC.isoformat()}
    )

    assert response.status_code == 201


async def test_rate_limit_is_checked_before_the_header(client):
    """Check order: size, rate limit, validation (incl. this header), signature."""
    enable_backdating()
    set_clock(NOON_UTC)
    app.state.rate_limiter = RateLimiter(1)
    assert (await post_activation(client, valid_payload())).status_code == 201

    response = await post_activation(
        client, valid_payload(), extra_headers={"X-Simulated-Time": "garbage"}
    )

    assert response.status_code == 429


def test_backdating_in_production_stops_startup(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("ALLOW_BACKDATING", "true")

    with pytest.raises(ConfigError, match="ALLOW_BACKDATING"):
        load_settings()


def test_backdating_is_allowed_in_development(monkeypatch):
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("ALLOW_BACKDATING", "TRUE")  # case-insensitive

    settings = load_settings()

    assert settings.allow_backdating is True
    assert settings.env == "development"


def test_production_without_backdating_starts(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("ALLOW_BACKDATING", "false")

    settings = load_settings()

    assert settings.env == "production"
    assert settings.allow_backdating is False


def test_unknown_env_value_stops_startup(monkeypatch):
    monkeypatch.setenv("ENV", "staging")

    with pytest.raises(ConfigError, match="ENV"):
        load_settings()


def test_bad_allow_backdating_value_stops_startup(monkeypatch):
    monkeypatch.setenv("ALLOW_BACKDATING", "maybe")

    with pytest.raises(ConfigError, match="ALLOW_BACKDATING"):
        load_settings()
