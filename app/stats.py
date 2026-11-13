"""The stats queries behind GET /api/v1/stats.

Reads only daily_activations (the rollup), never devices, so the cost depends
on the number of days and models, not on the number of devices. Days are
Asia/Jerusalem calendar days, like the rollup's day column. Every value is a
query parameter (%(name)s), never formatted into the SQL text.
"""

from datetime import date, datetime, timedelta

from psycopg_pool import AsyncConnectionPool

from app.schemas import DayStats, ModelStats, StatsRange, StatsResponse, Totals

DEFAULT_RANGE_DAYS = 30
MAX_RANGE_DAYS = 366


class InvalidRange(ValueError):
    """The requested dates are not a usable range (the route turns this into 422)."""


# Today in Israel, from the same "now" the activation path uses and the same
# expression it uses for the rollup day (app/activations.py).
_TODAY = "SELECT (%(now)s AT TIME ZONE 'Asia/Jerusalem')::date"

# One row per day of the range, including days with no rollup row (zeros).
# The days come from generate_series over integers added to the start date,
# so no time zone is involved. The optional model filter sits in the JOIN
# condition, not in WHERE, so a day without data still appears with 0.
_DAILY = """
SELECT d.day,
       COALESCE(SUM(a.activations), 0),
       COALESCE(SUM(a.duplicates), 0)
FROM (
    SELECT %(from)s::date + n AS day
    FROM generate_series(0, %(to)s::date - %(from)s::date) AS n
) AS d
LEFT JOIN daily_activations a
       ON a.day = d.day
      AND (%(model)s::text IS NULL OR a.model = %(model)s)
GROUP BY d.day
ORDER BY d.day
"""

# Total activated strictly before the range (with the model filter), and the
# unfiltered all-time totals.
_BEFORE_AND_ALL_TIME = """
SELECT COALESCE(SUM(activations) FILTER (
           WHERE day < %(from)s::date AND (%(model)s::text IS NULL OR model = %(model)s)
       ), 0),
       COALESCE(SUM(activations), 0),
       COALESCE(SUM(duplicates), 0)
FROM daily_activations
"""

# Per model for the range. Ties are broken by name so the order is stable.
_BY_MODEL = """
SELECT model, SUM(activations), SUM(duplicates)
FROM daily_activations
WHERE day BETWEEN %(from)s::date AND %(to)s::date
GROUP BY model
ORDER BY SUM(activations) DESC, model
"""


async def get_stats(
    pool: AsyncConnectionPool,
    now: datetime,
    from_: date | None,
    to: date | None,
    model: str | None,
) -> StatsResponse:
    """The whole stats answer. Raises InvalidRange for a bad date range.

    All queries run in one read-only REPEATABLE READ transaction, so every
    number in the answer comes from the same snapshot: an activation that
    lands while the request runs shows up in all of them or in none, and
    the daily series always adds up to the totals. SET TRANSACTION affects
    this transaction only, so the pooled connection is left unchanged for
    the next user (the activation path writes on the same pool).
    """
    async with pool.connection() as conn, conn.transaction():
        # Must be the first statement of the transaction.
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        if to is None:
            cur = await conn.execute(_TODAY, {"now": now})
            to = (await cur.fetchone())[0]
        if from_ is None:
            try:
                from_ = to - timedelta(days=DEFAULT_RANGE_DAYS - 1)
            except OverflowError:  # `to` is at the very start of the calendar
                raise InvalidRange("'to' is too early for the default range") from None
        if from_ > to:
            raise InvalidRange("'from' must not be after 'to'")
        if (to - from_).days + 1 > MAX_RANGE_DAYS:
            raise InvalidRange(f"the range must be at most {MAX_RANGE_DAYS} days")

        params = {"from": from_, "to": to, "model": model}
        cur = await conn.execute(_DAILY, params)
        daily = [DayStats(day=d, activated=a, duplicates=u) for d, a, u in await cur.fetchall()]
        cur = await conn.execute(_BEFORE_AND_ALL_TIME, params)
        before, all_activated, all_duplicates = await cur.fetchone()
        cur = await conn.execute(_BY_MODEL, params)
        by_model = [
            ModelStats(model=m, activated=a, duplicates=u) for m, a, u in await cur.fetchall()
        ]

    return StatsResponse(
        range=StatsRange(from_=from_, to=to, model=model),
        daily=daily,
        totals_in_range=Totals(
            activated=sum(d.activated for d in daily),
            duplicates=sum(d.duplicates for d in daily),
        ),
        activated_before_range=before,
        by_model=by_model,
        all_time=Totals(activated=all_activated, duplicates=all_duplicates),
    )
