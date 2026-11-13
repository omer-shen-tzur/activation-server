"""HTTP Basic auth for the dashboard and the stats API (never for the device endpoint).

Use it as a dependency: `dependencies=[Depends(require_dashboard_auth)]`.
The device endpoint authenticates with per-device signatures instead
(app/signing.py), and /health stays open.

The Authorization header is parsed here rather than with FastAPI's HTTPBasic:
that class only accepts ASCII credentials (a correct non-ASCII password could
never log in) and answers a malformed header with its own 401 without the
realm. Here every failure is the same 401.
"""

import base64
import secrets

from fastapi import HTTPException, Request


def _parse_basic(header: str | None) -> tuple[str, str] | None:
    """(username, password) from an "Authorization: Basic ..." header, else None."""
    if not header:
        return None
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except ValueError:  # bad base64 (binascii.Error) or not UTF-8 (UnicodeDecodeError)
        return None
    username, separator, password = decoded.partition(":")
    return (username, password) if separator else None


async def require_dashboard_auth(request: Request) -> None:
    """Raise 401 unless the request carries the configured dashboard user and password."""
    settings = request.app.state.settings
    parsed = _parse_basic(request.headers.get("authorization"))
    # Missing credentials are compared as empty strings, so the work done
    # does not depend on whether a header was sent.
    given_user, given_password = parsed or ("", "")
    # Bytes, because compare_digest on str raises TypeError for non-ASCII text.
    # Both comparisons always run (no early return), so the response time does
    # not say whether the username was right.
    user_ok = secrets.compare_digest(
        given_user.encode("utf-8"), settings.dashboard_user.encode("utf-8")
    )
    password_ok = secrets.compare_digest(
        given_password.encode("utf-8"), settings.dashboard_password.encode("utf-8")
    )
    if parsed is None or not (user_ok and password_ok):
        raise HTTPException(
            401,
            "invalid or missing credentials",
            headers={"WWW-Authenticate": 'Basic realm="dashboard"'},
        )
