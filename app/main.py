"""FastAPI application: lifespan (settings, pool, migration, rate limiter) and routes."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError

from app import db, signing
from app.activations import record_activation
from app.auth import require_dashboard_auth
from app.clock import activation_time, utc_now
from app.config import load_settings
from app.ratelimit import RateLimiter
from app.schemas import (
    ACTIVATION_EXAMPLE,
    ActivationRequest,
    ActivationResponse,
    StatsResponse,
)
from app.stats import InvalidRange, get_stats

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Scripts only from this origin and from Chart.js's pinned CDN; no inline
# script. Device-supplied strings (model names) reach the page through
# /api/v1/stats, so this is a second line of defence behind textContent.
DASHBOARD_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

# A real activation body is a few hundred bytes; 4 KB leaves ample room.
MAX_BODY_BYTES = 4096


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Settings are read here, not at import time: importing the app never
    # needs a .env, and tests can point DATABASE_URL at the test database
    # before the app starts. Any error here stops startup.
    settings = load_settings()
    pool = await db.open_pool(settings.database_url, settings.db_pool_size)
    try:
        await db.apply_migrations(pool)
        app.state.settings = settings
        app.state.pool = pool
        # One limiter for the process, on app.state so a test can replace it.
        app.state.rate_limiter = RateLimiter(settings.rate_limit_per_minute)
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


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    # The dashboard itself stays behind auth; this only points to it.
    return RedirectResponse("/dashboard")


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    """200 if the database answers SELECT 1, 503 if not. No auth."""
    if await db.ping(request.app.state.pool):
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "unavailable"}, status_code=503)


@app.get(
    "/api/v1/stats",
    response_model=StatsResponse,
    # Basic auth runs before the query parameters are validated, so a caller
    # without credentials learns nothing, not even which dates are valid.
    dependencies=[Depends(require_dashboard_auth)],
    responses={
        401: {"description": "Missing or wrong dashboard credentials."},
        422: {"description": "Invalid date, from after to, or a range over 366 days."},
        503: {"description": "Database unavailable."},
    },
)
async def stats(
    request: Request,
    from_: Annotated[
        date | None,
        Query(alias="from", description="First day, YYYY-MM-DD, Israel time. Default: to - 29 days."),
    ] = None,
    to: Annotated[
        date | None,
        Query(description="Last day, YYYY-MM-DD, Israel time. Default: today in Israel."),
    ] = None,
    model: Annotated[
        str | None,
        Query(description="Only this model (exact match after trimming). Blank means all."),
    ] = None,
    now: datetime = Depends(utc_now),
) -> StatsResponse:
    """Activations and duplicates per Israel day, read from the daily rollup only."""
    model = (model or "").strip() or None
    if model is not None and "\x00" in model:
        # Postgres text cannot hold NUL: a 422 here, not a 500 from the query.
        raise HTTPException(422, "model must not contain a NUL character")
    try:
        return await get_stats(request.app.state.pool, now, from_, to, model)
    except InvalidRange as exc:
        raise HTTPException(422, str(exc)) from None


def _static_file(name: str, media_type: str) -> Response:
    # The media type is explicit: on Windows, mimetypes can read .js as
    # text/plain from the registry. Plain `def` routes run in a threadpool,
    # so the small file read does not block the event loop.
    return Response(
        (STATIC_DIR / name).read_bytes(),
        media_type=media_type,
        headers={"Content-Security-Policy": DASHBOARD_CSP},
    )


# Explicit routes, not a mounted StaticFiles directory: a mount would serve
# files without running the auth dependency.
@app.get("/dashboard", dependencies=[Depends(require_dashboard_auth)], include_in_schema=False)
def dashboard_page() -> Response:
    return _static_file("dashboard.html", "text/html; charset=utf-8")


@app.get("/dashboard/app.js", dependencies=[Depends(require_dashboard_auth)], include_in_schema=False)
def dashboard_script() -> Response:
    return _static_file("dashboard.js", "text/javascript; charset=utf-8")


async def _read_body_limited(request: Request) -> bytes:
    """The raw body, or 413 as soon as it is known to exceed MAX_BODY_BYTES.

    A declared Content-Length over the limit is rejected before reading
    anything. The limit is then enforced again on the bytes actually read,
    so a missing or lying Content-Length (for example a chunked upload)
    cannot get more than MAX_BODY_BYTES into memory.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES:
                raise _body_too_large()
        except ValueError:
            pass  # not a number: the streamed count below still applies
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_BODY_BYTES:
            raise _body_too_large()
        chunks.append(chunk)
    return b"".join(chunks)


def _body_too_large() -> HTTPException:
    return HTTPException(413, f"request body over {MAX_BODY_BYTES} bytes")


def _client_ip(request: Request) -> str:
    # The TCP peer address. X-Forwarded-For is ignored: any client can set
    # it to a new value per request and dodge the limit.
    return request.client.host if request.client else "unknown"


def _parse_activation(raw: bytes) -> ActivationRequest:
    """Validate the raw body, or raise the same 422 FastAPI would."""
    try:
        return ActivationRequest.model_validate_json(raw)
    except ValidationError as exc:
        raise RequestValidationError(_json_safe_errors(exc)) from None


def _json_safe_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Pydantic's errors, made JSON-serializable, with FastAPI's "body" loc prefix.

    FastAPI's 422 handler JSON-encodes these errors. Two values would break
    it and turn a bad request into a 500: ctx holds the ValueError raised by
    a validator (normalize_mac), and for a body that is not valid JSON the
    input is the raw bytes, which may not be valid UTF-8.
    """
    errors = exc.errors(include_url=False)
    for err in errors:
        err["loc"] = ("body", *err["loc"])
        if isinstance(err.get("input"), bytes):
            err["input"] = err["input"].decode("utf-8", errors="replace")
        if "ctx" in err:
            err["ctx"] = {
                key: str(value) if isinstance(value, Exception) else value
                for key, value in err["ctx"].items()
            }
    return errors


@app.post(
    "/api/v1/activations",
    status_code=201,
    response_description="First activation of this device: counted.",
    responses={
        200: {
            "model": ActivationResponse,
            "description": "Device already activated: counted as a duplicate only.",
        },
        401: {"description": "X-Signature missing or wrong for this device_id and body."},
        413: {"description": f"Request body over {MAX_BODY_BYTES} bytes."},
        422: {
            "description": "Body is not valid JSON or breaks the schema (bad MAC, unknown field, "
            "missing model), or, when backdating is on, X-Simulated-Time is naive, "
            "unparsable or in the future."
        },
        429: {"description": "Too many requests from this IP: retry after Retry-After seconds."},
        503: {"description": "Database unavailable: retry later."},
    },
    # The endpoint reads the raw body itself (see activate), so FastAPI does
    # not know it has one. Describe it here so the docs and "Try it out"
    # still show the body.
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": ActivationRequest.model_json_schema(),
                    "example": ACTIVATION_EXAMPLE,
                }
            },
        }
    },
)
async def activate(
    request: Request,
    response: Response,
    # Optional at the type level so a missing header reaches our check and
    # gets 401, not FastAPI's 422.
    x_signature: Annotated[
        str | None,
        Header(description="Hex HMAC-SHA256 of the exact body bytes, with the device's key."),
    ] = None,
    resolve_time: Callable[[], datetime] = Depends(activation_time),
) -> ActivationResponse:
    """Record a device boot. Idempotent: safe to retry with the same body.

    Checks run cheapest first, and a rejected request never touches the
    database: body size (413), rate limit (429), validation (422, which
    includes the dev-only X-Simulated-Time header), then the signature
    (401). The signature comes last because the device key is derived from
    the MAC inside the validated body. The body is read as raw bytes, not
    declared as a model parameter, so the size limit runs before anything is
    parsed and the signature is checked over the exact bytes received.
    """
    raw = await _read_body_limited(request)

    retry_after = request.app.state.rate_limiter.take(_client_ip(request))
    if retry_after is not None:
        raise HTTPException(429, "too many requests", headers={"Retry-After": str(retry_after)})

    now = resolve_time()
    body = _parse_activation(raw)

    if not signing.verify(request.app.state.settings.master_secret, body.device_id, raw, x_signature):
        raise HTTPException(401, "missing or invalid X-Signature")

    result = await record_activation(request.app.state.pool, body, now)
    if result.new_device:
        return ActivationResponse(status="activated", first_activated_at=result.first_activated_at)
    response.status_code = 200
    return ActivationResponse(
        status="already_activated", first_activated_at=result.first_activated_at
    )
