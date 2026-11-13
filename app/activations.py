"""Record one activation message: the SQL, all in one transaction.

Every value is passed as a parameter (%(name)s): psycopg sends it separately
from the SQL text, so a device-supplied string can never change the query.

%(now)s is the request time from the clock dependency (app/clock.py), never
SQL now() or CURRENT_DATE. Tests set it to place an activation at a chosen
instant, and first_activated_at, last_seen_at and the rollup day must all
come from that same instant.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from psycopg_pool import AsyncConnectionPool

from app.schemas import ActivationRequest

# Inserts the device only if its MAC is new. This one statement is atomic:
# when several requests for the same new device run at once, exactly one
# gets a row back and the others get nothing. Never check-then-insert
# instead: two requests could both see "not found" and both count.
_INSERT_DEVICE = """
INSERT INTO devices (
    device_id, model, os_version, firmware_version, region,
    first_activated_at, device_reported_at, last_seen_at, attempt_count
)
VALUES (
    %(device_id)s, %(model)s, %(os_version)s, %(firmware_version)s, %(region)s,
    %(now)s, %(device_time)s, %(now)s, 1
)
ON CONFLICT (device_id) DO NOTHING
RETURNING device_id, first_activated_at
"""

# A known device: only last_seen_at and attempt_count change. The first
# activation is a snapshot; a retry never overwrites model, versions,
# region or device_time.
_TOUCH_DEVICE = """
UPDATE devices
SET last_seen_at = %(now)s,
    attempt_count = attempt_count + 1
WHERE device_id = %(device_id)s
RETURNING model, first_activated_at
"""

# Adds to the (Israeli day, model) rollup row, creating it if needed. The
# day is computed here in SQL, so Python needs no time zone database.
_COUNT_IN_ROLLUP = """
INSERT INTO daily_activations (day, model, activations, duplicates)
VALUES (
    (%(now)s AT TIME ZONE 'Asia/Jerusalem')::date,
    %(model)s, %(activations)s, %(duplicates)s
)
ON CONFLICT (day, model) DO UPDATE
SET activations = daily_activations.activations + EXCLUDED.activations,
    duplicates  = daily_activations.duplicates + EXCLUDED.duplicates
"""


@dataclass(frozen=True)
class ActivationResult:
    new_device: bool
    first_activated_at: datetime  # aware, UTC


async def record_activation(
    pool: AsyncConnectionPool, req: ActivationRequest, now: datetime
) -> ActivationResult:
    """Count the device if this is its first activation, else count a duplicate.

    The device row and the rollup change in one transaction, so they cannot
    drift apart. Lock order is always the device row first, then one rollup
    row, so concurrent requests cannot deadlock each other.
    """
    params = {
        "device_id": req.device_id,  # already normalized by validation
        "model": req.model,
        "os_version": req.os_version,
        "firmware_version": req.firmware_version,
        "region": req.region,
        "device_time": req.device_time,
        "now": now,
    }
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(_INSERT_DEVICE, params)
            row = await cur.fetchone()
            if row is not None:
                new_device = True
                _, first_activated_at = row
                rollup_model = req.model
            else:
                new_device = False
                cur = await conn.execute(_TOUCH_DEVICE, params)
                row = await cur.fetchone()
                if row is None:
                    # Device rows are never deleted, so a conflict on insert
                    # means the row exists. Reaching this is a bug: let it
                    # be a 500, not a silent miscount.
                    raise RuntimeError("insert conflicted but the device row was not found")
                # The duplicate counts under the model stored at the first
                # activation, not the one in this retry's payload, which is
                # untrusted.
                rollup_model, first_activated_at = row
            await conn.execute(
                _COUNT_IN_ROLLUP,
                {
                    "now": now,
                    "model": rollup_model,
                    "activations": 1 if new_device else 0,
                    "duplicates": 0 if new_device else 1,
                },
            )
    # psycopg returns TIMESTAMPTZ in the session's time zone; answer in UTC.
    return ActivationResult(new_device, first_activated_at.astimezone(timezone.utc))
