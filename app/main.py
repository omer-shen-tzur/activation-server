"""FastAPI application: lifespan (settings, pool, migration) and routes."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

import psycopg
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app import db
from app.activations import record_activation
from app.clock import utc_now
from app.config import load_settings
from app.schemas import ActivationRequest, ActivationResponse

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Settings are read here, not at import time: importing the app never
    # needs a .env, and tests can point DATABASE_URL at the test database
    # before the app starts. Any error here stops startup.
    settings = load_settings()
    pool = await db.open_pool(settings.database_url, settings.db_pool_size)
    try:
        await db.apply_migrations(pool)
        app.state.pool = pool
        yield
    finally:
        await pool.close()


app = FastAPI(title="Activation Server", lifespan=lifespan)


@app.exception_handler(psycopg.OperationalError)
async def database_unavailable(request: Request, exc: psycopg.OperationalError) -> JSONResponse:
    """503 when the database cannot serve the request, so the device retries later.

    psycopg.OperationalError covers a lost or refused connection, PoolTimeout
    (no connection within db.POOL_TIMEOUT_S) and PoolClosed, plus the
    server's "try again" errors: shutting down, too many connections, query
    canceled, deadlock, serialization failure. A retry is the right answer
    for all of them. Bugs are other classes (ProgrammingError for bad SQL,
    IntegrityError for a broken constraint, DataError) and stay 500, so a
    real bug never looks like an outage.
    """
    # The reason is logged here, never returned to the client.
    log.warning("database unavailable for %s %s: %r", request.method, request.url.path, exc)
    return JSONResponse({"status": "unavailable"}, status_code=503)


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    """200 if the database answers SELECT 1, 503 if not. No auth."""
    if await db.ping(request.app.state.pool):
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "unavailable"}, status_code=503)


@app.post(
    "/api/v1/activations",
    status_code=201,
    response_description="First activation of this device: counted.",
    responses={
        200: {
            "model": ActivationResponse,
            "description": "Device already activated: counted as a duplicate only.",
        },
        503: {"description": "Database unavailable: retry later."},
    },
)
async def activate(
    body: ActivationRequest,
    request: Request,
    response: Response,
    now: datetime = Depends(utc_now),
) -> ActivationResponse:
    """Record a device boot. Idempotent: safe to retry with the same body."""
    result = await record_activation(request.app.state.pool, body, now)
    if result.new_device:
        return ActivationResponse(status="activated", first_activated_at=result.first_activated_at)
    response.status_code = 200
    return ActivationResponse(
        status="already_activated", first_activated_at=result.first_activated_at
    )
