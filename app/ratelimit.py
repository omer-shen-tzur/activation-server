"""Per-client-IP rate limit for the activation endpoint: an in-memory token bucket.

Each client IP has a bucket holding up to `per_minute` tokens, refilled at
per_minute / 60 tokens per second. A request takes one token; with none
left it gets 429 and a Retry-After. A rejected request takes nothing, so a
device that waits as told is let through.

Limits of this design (also in the README): the buckets live in this
process only, so they reset on restart and each process of a multi-worker
or multi-instance deployment has its own. Behind a reverse proxy every
client has the proxy's IP and they all share one bucket. Production needs a
shared limiter (for example in Redis) or rate limiting at the gateway.

Elapsed time is measured with time.monotonic(), never the app's clock
dependency: the simulator backdates that clock (step 6), and the limiter
must measure real time. monotonic() also never jumps when the system clock
is changed.

No lock is needed: the app runs on one event loop, and take() has no await,
so no other request can run between reading a bucket and updating it.
"""

import math
import time

# Above this many tracked IPs, buckets that have refilled to full are
# dropped. A full bucket carries no information (a new IP starts full), so
# dropping one never changes a decision; it only bounds memory.
MAX_TRACKED_CLIENTS = 10_000


class RateLimiter:
    def __init__(self, per_minute: int, max_tracked: int = MAX_TRACKED_CLIENTS) -> None:
        if per_minute < 1:
            raise ValueError("per_minute must be at least 1")
        self._capacity = float(per_minute)
        self._refill_per_s = per_minute / 60.0
        self._max_tracked = max_tracked
        # ip -> (tokens at last update, monotonic time of last update)
        self._buckets: dict[str, tuple[float, float]] = {}

    def take(self, client: str) -> int | None:
        """Take one token for client.

        Returns None if the request is allowed; otherwise the whole number
        of seconds (at least 1) until a token is available, for Retry-After.
        """
        now = time.monotonic()
        bucket = self._buckets.get(client)
        if bucket is None:
            if len(self._buckets) >= self._max_tracked:
                self._drop_full_buckets(now)
            tokens = self._capacity
        else:
            tokens = self._level(bucket, now)

        if tokens < 1:
            # Rejected: the bucket is left as it was (nothing is taken).
            return max(1, math.ceil((1 - tokens) / self._refill_per_s))
        self._buckets[client] = (tokens - 1, now)
        return None

    def _level(self, bucket: tuple[float, float], now: float) -> float:
        tokens, updated_at = bucket
        return min(self._capacity, tokens + (now - updated_at) * self._refill_per_s)

    def _drop_full_buckets(self, now: float) -> None:
        # O(number of tracked IPs), and only runs when a new IP arrives while
        # the table is at the limit.
        self._buckets = {
            client: bucket
            for client, bucket in self._buckets.items()
            if self._level(bucket, now) < self._capacity
        }
