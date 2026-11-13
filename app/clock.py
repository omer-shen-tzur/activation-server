"""The app's single source of "now", and the dev-only backdating built on it.

Every route that needs the current time takes it as a FastAPI dependency
and passes that value on (into SQL as a parameter, never SQL now()). Tests
override utc_now with `app.dependency_overrides[utc_now] = lambda: <instant>`
to place an activation at a chosen moment.

The simulator needs to build history (devices activated over the last 30
days), so the activation endpoint takes its time from activation_time()
instead, which starts from utc_now and honors an X-Simulated-Time header
only when backdating is enabled.
"""

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request


async def utc_now() -> datetime:
    """The current time as an aware UTC datetime."""
    # async def, not def: FastAPI runs a plain def dependency in a worker
    # thread, which is pointless overhead just to read the clock.
    return datetime.now(timezone.utc)


async def activation_time(
    request: Request,
    now: datetime = Depends(utc_now),
    x_simulated_time: Annotated[
        str | None,
        Header(
            description=(
                "Dev only, ignored unless ALLOW_BACKDATING=true and ENV is not production. "
                "ISO 8601 datetime with an offset, not in the future: the activation is "
                "recorded at this instant instead of now."
            )
        ),
    ] = None,
) -> Callable[[], datetime]:
    """A function that returns the time to record this activation at.

    It is a function, not the time itself, because FastAPI runs dependencies
    before the endpoint body, and a bad header must not be rejected ahead of
    the body size and rate limit checks. The endpoint calls it right after
    the rate limit, as part of validation.

    Why dev only: a fake time in production would corrupt the history, so
    the header is ignored (not rejected) unless backdating is explicitly on,
    and the app refuses to start with backdating on in production
    (app/config.py). Why it is not covered by the signature: it only exists
    in dev, off by default, and nothing is gained by signing a value that is
    ignored in production.
    """
    settings = request.app.state.settings
    backdating = settings.allow_backdating and settings.env != "production"

    def resolve() -> datetime:
        if not backdating or x_simulated_time is None:
            return now
        return _parse_simulated_time(x_simulated_time, now)

    return resolve


def _parse_simulated_time(raw: str, now: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        raise HTTPException(
            422, "X-Simulated-Time must be an ISO 8601 datetime with an offset"
        ) from None
    if parsed.utcoffset() is None:
        raise HTTPException(
            422, "X-Simulated-Time must include an offset (for example +00:00 or Z)"
        )
    try:
        as_utc = parsed.astimezone(timezone.utc)
    except OverflowError:  # an offset pushes it outside the supported years
        raise HTTPException(422, "X-Simulated-Time is out of range") from None
    if as_utc > now:
        raise HTTPException(422, "X-Simulated-Time must not be in the future")
    return as_utc
