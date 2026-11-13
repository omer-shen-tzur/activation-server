"""Postgres: the async connection pool, the startup migration and the health ping."""

import asyncio
import logging
from pathlib import Path

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Longest a request waits for a pool connection. psycopg_pool's default is
# 30 s; with the database down every request would hang that long. Failing
# after 5 s returns an error quickly, and a device simply retries.
POOL_TIMEOUT_S = 5.0
# Startup waits this long for the pool's connections, then the app exits
# with an error instead of hanging.
STARTUP_TIMEOUT_S = 10.0
# TCP connect timeout per attempt (libpq connect_timeout), so one attempt
# to an unreachable host cannot use up the whole startup budget.
CONNECT_TIMEOUT_S = 3
# How long the pool keeps retrying a lost connection, with exponential
# backoff, before it drops that slot (the next request then opens a fresh
# one). With the default of 300 s the backoff grows to minutes between tries.
# After an outage, dead pooled connections are discarded only when requests
# pick them up, so recovery takes a few requests. Measured on Windows with
# Docker Desktop: 2-6 s with requests once a second (polling by hand every
# ~40 s made it look like 40-75 s). Meanwhile requests get 503 and devices
# retry.
RECONNECT_TIMEOUT_S = 10.0
# /health waits at most this long for a connection, and again for SELECT 1.
HEALTH_TIMEOUT_S = 2.0


class DatabaseUnavailableError(RuntimeError):
    """The database could not be reached at startup."""


async def open_pool(database_url: str, size: int) -> AsyncConnectionPool:
    """Open a fixed-size pool and wait until its connections are ready.

    Raises DatabaseUnavailableError if that takes longer than STARTUP_TIMEOUT_S.
    """
    pool = AsyncConnectionPool(
        conninfo=database_url,
        # Fixed size (min = max), psycopg_pool's recommended setup: the
        # connections are opened once at startup, not during a burst.
        min_size=size,
        max_size=size,
        name="activations",
        open=False,
        timeout=POOL_TIMEOUT_S,
        reconnect_timeout=RECONNECT_TIMEOUT_S,
        # Test each connection with an empty query before handing it out.
        # After a database restart every pooled connection is dead; this
        # throws them away and opens new ones, so the app recovers without
        # a restart. Cost: one short round trip per checkout.
        check=AsyncConnectionPool.check_connection,
        kwargs={"connect_timeout": CONNECT_TIMEOUT_S},
    )
    try:
        await pool.open(wait=True, timeout=STARTUP_TIMEOUT_S)
    except PoolTimeout:
        # The pool has already closed itself. The psycopg.pool warnings
        # logged before this say why each attempt failed (connection
        # refused, wrong password, unknown database...).
        raise DatabaseUnavailableError(
            f"Could not connect to the database within {STARTUP_TIMEOUT_S:.0f} s. "
            "Is it running? Start it with: docker compose up -d --wait db. "
            "If it is running, check DATABASE_URL in .env."
        ) from None
    return pool


async def apply_migrations(pool: AsyncConnectionPool) -> None:
    """Run every migrations/*.sql in name order, all in one transaction.

    Each file must be safe to run again (IF NOT EXISTS). Only one process
    runs this; several workers would need a lock (see 001_init.sql).
    """
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise RuntimeError(f"No migration files found in {MIGRATIONS_DIR}")
    async with pool.connection() as conn:
        for path in files:
            # The file is ours, not user input. Sent without parameters,
            # psycopg uses the simple query protocol, which allows several
            # statements in one call. utf-8-sig ignores a BOM that some
            # Windows editors add.
            await conn.execute(path.read_text(encoding="utf-8-sig"))
            log.info("applied migration %s", path.name)


async def ping(pool: AsyncConnectionPool) -> bool:
    """True if the database answers SELECT 1 in time. Never raises for a DB problem."""
    try:
        async with pool.connection(timeout=HEALTH_TIMEOUT_S) as conn:
            async with asyncio.timeout(HEALTH_TIMEOUT_S):
                await conn.execute("SELECT 1")
    except (psycopg.Error, TimeoutError) as exc:
        # PoolTimeout is a psycopg.OperationalError. The reason is logged
        # here, not returned: /health has no auth.
        log.warning("health check failed: %r", exc)
        return False
    return True
