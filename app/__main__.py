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

HOST = "127.0.0.1"  # loopback only, like the database port
PORT = 8000


def main() -> int:
    # lifespan="on": a failing startup (missing setting, database down)
    # must stop the server, never be skipped.
    config = uvicorn.Config("app.main:app", host=HOST, port=PORT, lifespan="on")
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
    # uvicorn.run() exits non-zero when startup fails; serve() just returns.
    # Do the same, so a failed startup gives exit code 1.
    return 0 if server.started else 1


if __name__ == "__main__":
    sys.exit(main())
