"""Run the server: python -m app   (http://127.0.0.1:8000)

Why not just `uvicorn app.main:app`: psycopg's async mode cannot run on
Windows' default ProactorEventLoop; it needs a SelectorEventLoop. The uvicorn
CLI creates the event loop itself, and how it does that on Windows has
changed between uvicorn versions (event loop policies in older releases,
loop factories in newer ones), so whether psycopg gets a usable loop would
depend on the installed version. Here we create the loop ourselves with
asyncio.Runner(loop_factory=...) (Python 3.11+) and run uvicorn.Server.serve()
inside it. serve() runs on whatever loop is current and never picks one, so
this behaves the same with any uvicorn version. On Linux and macOS the
default loop is already selector-based, so nothing changes there.
"""

import asyncio
import logging
import sys

import uvicorn
from starlette.types import Message, Receive, Scope, Send

from app.config import ConfigError
from app.db import DatabaseUnavailableError
from app.main import app

HOST = "127.0.0.1"  # loopback only, like the database port
PORT = 8000

# Startup errors whose message already says what is wrong and what to do.
# For these the log shows that message alone. Any other startup error keeps
# its full traceback, because there the traceback is what you need.
CLEAN_STARTUP_ERRORS = (ConfigError, DatabaseUnavailableError)


async def app_with_clean_startup_errors(scope: Scope, receive: Receive, send: Send) -> None:
    """The FastAPI app, reporting CLEAN_STARTUP_ERRORS without a traceback.

    When the lifespan raises, Starlette sends uvicorn a "startup failed"
    message whose text is the whole traceback (traceback.format_exc()), and
    uvicorn logs that text; then Starlette re-raises the exception. Here,
    for lifespan events only, that message is held back until the exception
    arrives. For a known error its text is replaced by the exception's own
    message; anything else is passed on unchanged. HTTP requests go straight
    to the app. No logging configuration is involved.
    """
    if scope["type"] != "lifespan":
        await app(scope, receive, send)
        return

    failed: Message | None = None  # Starlette's "startup failed", held back

    async def send_holding_failure(message: Message) -> None:
        nonlocal failed
        if message["type"] == "lifespan.startup.failed":
            failed = message
        else:
            await send(message)

    try:
        await app(scope, receive, send_holding_failure)
    except CLEAN_STARTUP_ERRORS as exc:
        if failed is None:  # not raised during startup: leave it alone
            raise
        # Not re-raised: uvicorn learns that startup failed from this
        # message, logs it, and exits.
        failed = {"type": "lifespan.startup.failed", "message": str(exc)}
    finally:
        if failed is not None:
            await send(failed)


def main() -> int:
    # lifespan="on": a failing startup (missing setting, database down)
    # must stop the server, never be skipped.
    config = uvicorn.Config(
        app_with_clean_startup_errors, host=HOST, port=PORT, lifespan="on"
    )
    # uvicorn configures only its own loggers, when Config is created (hence
    # this order). Also show our app.* logs and psycopg's warnings, which say
    # why a database connection failed. psycopg logs every pool checkout at
    # INFO, so keep it at WARNING.
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s: %(message)s")
    logging.getLogger("psycopg").setLevel(logging.WARNING)

    server = uvicorn.Server(config)
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            runner.run(server.serve())
    except KeyboardInterrupt:
        pass  # Ctrl+C: uvicorn has already shut down cleanly.
    except SystemExit:
        # When startup fails (lifespan error, port already taken), uvicorn
        # 0.54's serve() calls sys.exit(3) after logging the reason. If a
        # version returns instead, server.started is False all the same.
        # Either way a failed startup ends below as exit code 1.
        if server.started:
            raise
    return 0 if server.started else 1


if __name__ == "__main__":
    sys.exit(main())
