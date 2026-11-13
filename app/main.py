"""FastAPI application: lifespan (settings, pool, migration) and routes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import db
from app.config import load_settings


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


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    """200 if the database answers SELECT 1, 503 if not. No auth."""
    if await db.ping(request.app.state.pool):
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "unavailable"}, status_code=503)
