"""The app's single source of "now".

Every route that needs the current time takes it as a FastAPI dependency,
`now: datetime = Depends(utc_now)`, and passes that value on (into SQL as a
parameter, never SQL now()). Tests override it with
`app.dependency_overrides[utc_now] = lambda: <instant>` to place an
activation at a chosen moment, and the simulator's dev-only backdating
(step 6) will build on this same function.
"""

from datetime import datetime, timezone


async def utc_now() -> datetime:
    """The current time as an aware UTC datetime."""
    # async def, not def: FastAPI runs a plain def dependency in a worker
    # thread, which is pointless overhead just to read the clock.
    return datetime.now(timezone.utc)
