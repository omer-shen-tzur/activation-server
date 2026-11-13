"""GET /api/v1/stats: Basic auth, range rules, correct numbers, and the dashboard settings."""

import dataclasses
from datetime import date, datetime, timedelta, timezone

import pytest
from dotenv import dotenv_values

from app.config import (
    DASHBOARD_PASSWORD_PLACEHOLDER,
    ENV_FILE,
    ConfigError,
    load_settings,
)
from app.main import app
from tests.helpers import (
    DASHBOARD_AUTH,
    NOON_UTC,
    assert_invariants,
    mac_n,
    post_activation,
    set_clock,
    valid_payload,
)

pytestmark = pytest.mark.anyio

STATS = "/api/v1/stats"
TODAY = date(2026, 11, 13)  # the Israeli day of NOON_UTC


async def get_stats(client, params=None, auth=DASHBOARD_AUTH):
    return await client.get(STATS, params=params, auth=auth)


def at(day: str, hour: int) -> datetime:
    """An instant on a given UTC date (hours 8-11 are the same Israeli day)."""
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=timezone.utc)


async def activate(client, n: int, model: str, instant: datetime) -> int:
    set_clock(instant)
    response = await post_activation(client, valid_payload(device_id=mac_n(n), model=model))
    assert response.status_code in (200, 201)
    return response.status_code


async def seed_known_data(client) -> None:
    """Six devices, two models, eight messages, over four Israeli days.

    day         X100 activated / duplicates     Z5 activated / duplicates
    2026-10-20  1 (device 5)
    2026-10-25                                  1 (device 6)
    2026-11-10  1 (device 4) / 0                0 / 1 (device 6 again)
    2026-11-13  2 (devices 1, 2) / 1 (device 1) 1 (device 3) / 0
    """
    assert await activate(client, 5, "X100", at("2026-10-20", 10)) == 201
    assert await activate(client, 6, "Z5", at("2026-10-25", 10)) == 201
    assert await activate(client, 4, "X100", at("2026-11-10", 10)) == 201
    assert await activate(client, 6, "Z5", at("2026-11-10", 11)) == 200
    assert await activate(client, 1, "X100", at("2026-11-13", 8)) == 201
    assert await activate(client, 2, "X100", at("2026-11-13", 9)) == 201
    assert await activate(client, 3, "Z5", at("2026-11-13", 10)) == 201
    assert await activate(client, 1, "X100", at("2026-11-13", 11)) == 200
    await assert_invariants()
    set_clock(NOON_UTC)


# --- Basic auth -------------------------------------------------------------


async def test_no_credentials_is_401_with_a_basic_challenge(client):
    response = await get_stats(client, auth=None)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Basic realm="dashboard"'


@pytest.mark.parametrize(
    "auth",
    [
        ("wrong-user", "test-dashboard-password"),
        ("test-dashboard-user", "wrong-password"),
        ("wrong-user", "wrong-password"),
        ("", ""),
        ("test-dashboard-üser", "test-dashboard-password"),  # non-ASCII: 401, never a 500
    ],
    ids=["wrong-user", "wrong-password", "both-wrong", "empty", "non-ascii"],
)
async def test_wrong_credentials_are_401(client, auth):
    response = await get_stats(client, auth=auth)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Basic realm="dashboard"'


async def test_malformed_authorization_header_is_401(client):
    response = await client.get(STATS, headers={"Authorization": "Basic !!!not-base64!!!"})

    assert response.status_code == 401


async def test_a_non_ascii_password_works(client):
    app.state.settings = dataclasses.replace(app.state.settings, dashboard_password="pässwörd-é")

    assert (await get_stats(client, auth=("test-dashboard-user", "pässwörd-é"))).status_code == 200
    assert (await get_stats(client)).status_code == 401  # the old password no longer works


async def test_auth_is_checked_before_the_query_is_validated(client):
    response = await get_stats(client, {"from": "garbage"}, auth=None)

    assert response.status_code == 401


async def test_correct_credentials_give_200(client):
    response = await get_stats(client)

    assert response.status_code == 200


async def test_health_needs_no_credentials(client):
    assert (await client.get("/health")).status_code == 200


# --- range rules ------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-13-45"},
        {"from": "garbage"},
        {"to": "2026-02-30"},
        {"from": "2026-11-14", "to": "2026-11-13"},  # from after to
        {"from": "2026-11-14"},  # from after the default to (today)
        {"from": "2025-11-12", "to": "2026-11-13"},  # 367 days
        {"from": "0001-01-01", "to": "9999-12-31"},  # absurdly long
        {"to": "0001-01-01"},  # the default from would be before year 1
        {"model": "bad\x00model"},  # Postgres text cannot hold NUL
    ],
    ids=[
        "invalid-date",
        "not-a-date",
        "impossible-day",
        "from-after-to",
        "from-after-today",
        "367-days",
        "huge-range",
        "to-at-calendar-start",
        "nul-in-model",
    ],
)
async def test_bad_query_is_422(client, params):
    set_clock(NOON_UTC)

    response = await get_stats(client, params)

    assert response.status_code == 422


async def test_exactly_366_days_is_accepted(client):
    set_clock(NOON_UTC)
    first = TODAY - timedelta(days=365)

    response = await get_stats(client, {"from": first.isoformat(), "to": TODAY.isoformat()})

    assert response.status_code == 200
    daily = response.json()["daily"]
    assert len(daily) == 366
    assert daily[0]["day"] == first.isoformat()
    assert daily[-1]["day"] == TODAY.isoformat()


async def test_a_one_day_range_works(client):
    response = await get_stats(client, {"from": "2026-11-13", "to": "2026-11-13"})

    assert response.status_code == 200
    assert [d["day"] for d in response.json()["daily"]] == ["2026-11-13"]


# --- defaults ---------------------------------------------------------------


async def test_default_range_is_the_30_days_ending_today(client):
    set_clock(NOON_UTC)

    body = (await get_stats(client)).json()

    assert body["range"] == {"from": "2026-10-15", "to": "2026-11-13", "model": None}
    assert len(body["daily"]) == 30
    assert body["daily"][0]["day"] == "2026-10-15"
    assert body["daily"][-1]["day"] == "2026-11-13"


async def test_today_is_the_israeli_day(client):
    # 22:30 UTC on Nov 13 is 00:30 on Nov 14 in Israel (UTC+2 in winter).
    set_clock(at("2026-11-13", 22).replace(minute=30))

    body = (await get_stats(client)).json()

    assert body["range"]["to"] == "2026-11-14"
    assert body["range"]["from"] == "2026-10-16"


async def test_only_to_given_the_range_is_the_29_days_before_it(client):
    body = (await get_stats(client, {"to": "2026-03-31"})).json()

    assert body["range"]["from"] == "2026-03-02"
    assert len(body["daily"]) == 30


# --- the numbers ------------------------------------------------------------


async def test_empty_database_gives_zeros_not_errors(client):
    set_clock(NOON_UTC)

    body = (await get_stats(client)).json()

    assert len(body["daily"]) == 30
    assert all(d["activated"] == 0 and d["duplicates"] == 0 for d in body["daily"])
    assert body["totals_in_range"] == {"activated": 0, "duplicates": 0}
    assert body["activated_before_range"] == 0
    assert body["by_model"] == []
    assert body["all_time"] == {"activated": 0, "duplicates": 0}


async def test_known_data(client):
    await seed_known_data(client)

    response = await get_stats(client, {"from": "2026-11-08", "to": "2026-11-13"})

    assert response.status_code == 200
    body = response.json()
    assert body["range"] == {"from": "2026-11-08", "to": "2026-11-13", "model": None}
    # Days with no activity are present, as zeros.
    assert body["daily"] == [
        {"day": "2026-11-08", "activated": 0, "duplicates": 0},
        {"day": "2026-11-09", "activated": 0, "duplicates": 0},
        {"day": "2026-11-10", "activated": 1, "duplicates": 1},
        {"day": "2026-11-11", "activated": 0, "duplicates": 0},
        {"day": "2026-11-12", "activated": 0, "duplicates": 0},
        {"day": "2026-11-13", "activated": 3, "duplicates": 1},
    ]
    assert body["totals_in_range"] == {"activated": 4, "duplicates": 2}
    # Devices 5 and 6 activated before the range: the cumulative line starts at 2.
    assert body["activated_before_range"] == 2
    # Sorted by activated, descending.
    assert body["by_model"] == [
        {"model": "X100", "activated": 3, "duplicates": 1},
        {"model": "Z5", "activated": 1, "duplicates": 1},
    ]
    assert body["all_time"] == {"activated": 6, "duplicates": 2}


async def test_range_ending_before_all_the_data(client):
    await seed_known_data(client)

    body = (await get_stats(client, {"from": "2026-10-01", "to": "2026-10-21"})).json()

    assert body["totals_in_range"] == {"activated": 1, "duplicates": 0}
    assert body["activated_before_range"] == 0
    assert body["by_model"] == [{"model": "X100", "activated": 1, "duplicates": 0}]
    assert body["all_time"] == {"activated": 6, "duplicates": 2}  # not affected by the range


async def test_model_filter(client):
    await seed_known_data(client)

    body = (await get_stats(client, {"from": "2026-11-08", "to": "2026-11-13", "model": "Z5"})).json()

    assert body["range"]["model"] == "Z5"
    assert body["daily"][2] == {"day": "2026-11-10", "activated": 0, "duplicates": 1}
    assert body["daily"][5] == {"day": "2026-11-13", "activated": 1, "duplicates": 0}
    assert body["totals_in_range"] == {"activated": 1, "duplicates": 1}
    assert body["activated_before_range"] == 1  # device 6 only
    # by_model and all_time ignore the filter.
    assert [m["model"] for m in body["by_model"]] == ["X100", "Z5"]
    assert body["all_time"] == {"activated": 6, "duplicates": 2}


async def test_model_filter_is_trimmed_exact_and_blank_means_all(client):
    await seed_known_data(client)
    params = {"from": "2026-11-08", "to": "2026-11-13"}

    trimmed = (await get_stats(client, {**params, "model": "  Z5  "})).json()
    assert trimmed["range"]["model"] == "Z5"
    assert trimmed["totals_in_range"] == {"activated": 1, "duplicates": 1}

    lowercase = (await get_stats(client, {**params, "model": "z5"})).json()
    assert lowercase["totals_in_range"] == {"activated": 0, "duplicates": 0}  # exact match

    blank = (await get_stats(client, {**params, "model": "   "})).json()
    assert blank["range"]["model"] is None
    assert blank["totals_in_range"] == {"activated": 4, "duplicates": 2}


async def test_unknown_model_gives_zeros_but_keeps_the_unfiltered_parts(client):
    await seed_known_data(client)

    body = (await get_stats(client, {"model": "nope"})).json()

    assert body["totals_in_range"] == {"activated": 0, "duplicates": 0}
    assert body["activated_before_range"] == 0
    assert len(body["by_model"]) == 2
    assert body["all_time"] == {"activated": 6, "duplicates": 2}


async def test_by_model_is_sorted_by_activated_descending(client):
    set_clock(NOON_UTC)
    for n, model in enumerate(["B", "A", "A", "C", "C", "C"], start=1):
        await activate(client, n, model, at("2026-11-13", 8))

    body = (await get_stats(client)).json()

    assert [(m["model"], m["activated"]) for m in body["by_model"]] == [
        ("C", 3),
        ("A", 2),
        ("B", 1),
    ]


async def test_stats_read_only_the_rollup_table(client):
    """With no rows in devices at all, the numbers still come from daily_activations."""
    async with app.state.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO daily_activations (day, model, activations, duplicates) "
            "VALUES ('2026-11-13', 'X100', 7, 3)"
        )

    body = (await get_stats(client, {"from": "2026-11-13", "to": "2026-11-13"})).json()

    assert body["totals_in_range"] == {"activated": 7, "duplicates": 3}
    assert body["all_time"] == {"activated": 7, "duplicates": 3}


async def test_script_like_model_name_comes_back_as_plain_data(client):
    name = "<script>alert(1)</script>"
    await activate(client, 1, name, NOON_UTC)

    body = (await get_stats(client)).json()
    filtered = (await get_stats(client, {"model": name})).json()

    assert body["by_model"] == [{"model": name, "activated": 1, "duplicates": 0}]
    assert filtered["totals_in_range"] == {"activated": 1, "duplicates": 0}
    assert filtered["range"]["model"] == name


# --- startup settings -------------------------------------------------------


@pytest.mark.parametrize("name", ["DASHBOARD_USER", "DASHBOARD_PASSWORD"])
@pytest.mark.parametrize("value", ["", "   "], ids=["empty", "blank"])
def test_startup_refuses_without_dashboard_credentials(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError, match=name):
        load_settings()


def test_startup_refuses_the_placeholder_password_in_production(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("DASHBOARD_PASSWORD", DASHBOARD_PASSWORD_PLACEHOLDER)

    with pytest.raises(ConfigError, match="DASHBOARD_PASSWORD"):
        load_settings()


def test_placeholder_password_is_fine_in_development(monkeypatch):
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("DASHBOARD_PASSWORD", DASHBOARD_PASSWORD_PLACEHOLDER)

    assert load_settings().dashboard_password == DASHBOARD_PASSWORD_PLACEHOLDER


def test_a_real_password_is_fine_in_production(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("ALLOW_BACKDATING", "false")

    settings = load_settings()

    assert settings.env == "production"
    assert "test-dashboard-password" not in repr(settings)  # kept out of logs


def test_the_placeholder_matches_env_example():
    """If .env.example changes its placeholder, the production check must follow."""
    example = dotenv_values(ENV_FILE.with_name(".env.example"))

    assert example["DASHBOARD_PASSWORD"] == DASHBOARD_PASSWORD_PLACEHOLDER
    assert example["DASHBOARD_USER"]
