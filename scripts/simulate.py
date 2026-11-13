"""Simulate a fleet of devices activating over the last N days.

    python -m scripts.simulate [--url URL] [--devices 5000] [--days 30]
                               [--seed 1] [--concurrency 20]

The server must be started with ALLOW_BACKDATING=true (and ENV not
production), because the history is built with the dev-only X-Simulated-Time
header, and the simulator must be the only traffic during the run: it also
cross-checks the change in /api/v1/stats against its own 201 and 200 counts
(logging in with DASHBOARD_USER / DASHBOARD_PASSWORD). Exit code: 0 ok,
1 self-check failed or dashboard credentials missing, 2 setup problem.

Two parts:
  build_plan()      what every device will send and when; fully determined by
                    the seed (its own random.Random, never the global one).
  run_simulation()  sends the plan through an httpx.AsyncClient and returns a
                    Result, so a test can run it in-process against the app.

Devices behave like real firmware: sign every body with the per-device key
(app.signing), retry on network errors, 429 and 5xx with exponential backoff
and jitter, and count a device as activated only after a 200 or 201.
"""

import argparse
import asyncio
import json
import math
import os
import random
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

from app import signing
from app.config import ENV_FILE
from app.schemas import normalize_mac

ACTIVATIONS_PATH = "/api/v1/activations"
STATS_PATH = "/api/v1/stats"

# Share of devices that resend, how many extra messages they send, and how
# many of those come on a later day rather than minutes later.
RESEND_SHARE = 0.25
RESEND_COUNTS = (2, 3)
LATER_DAY_SHARE = 0.4
# Share of devices that leave out each optional field.
OMIT_SHARE = 0.1
# Newest planned time is this far before "now", so no planned time is ever
# in the future by the time the server sees it.
MARGIN = timedelta(minutes=1)

# Retry rule of one request.
MAX_TRIES = 8
BACKOFF_BASE_S = 0.5
BACKOFF_CAP_S = 30.0


@dataclass(frozen=True)
class ModelSpec:
    name: str
    # Fixed vendor prefix: first octet even and bit 0x02 clear, so every MAC
    # built from it is unicast and universally administered (accepted).
    prefix: str
    share: float
    firmware: tuple[str, ...]


# Invented Android phones and tablets (no real brands). Shares sum to 1.
MODELS = (
    ModelSpec("Vexa P1", "00:1B:44", 0.20, ("1.0.2", "1.1.0", "1.2.3")),
    ModelSpec("Vexa P1 Pro", "00:1C:B3", 0.12, ("2.0.0", "2.0.1", "2.1.0")),
    ModelSpec("Vexa Lite", "00:1E:C0", 0.18, ("0.9.5", "1.0.0")),
    ModelSpec("Orbix Edge 5G", "00:25:9C", 0.10, ("1.4.0", "1.5.0", "1.5.1")),
    ModelSpec("Orbix Mini", "00:26:BB", 0.08, ("3.0.0", "3.1.2")),
    ModelSpec("Tavo Tab 8", "00:1D:0F", 0.12, ("1.0.0", "1.0.4")),
    ModelSpec("Tavo Tab 10", "00:21:6A", 0.12, ("2.2.0", "2.3.1")),
    ModelSpec("Tavo Tab 10 Pro", "00:23:14", 0.08, ("4.0.0", "4.1.0")),
)
OS_VERSIONS = ("Android 12", "Android 13", "Android 14", "Android 15")
REGIONS = ("IL", "US", "DE", "FR", "GB")


@dataclass(frozen=True)
class Attempt:
    at: datetime  # sent as X-Simulated-Time (the activation time we want)
    device_time: datetime | None  # the device's own clock reading, or None if omitted


@dataclass(frozen=True)
class DevicePlan:
    mac: str
    model: str
    os_version: str | None
    firmware_version: str | None
    region: str | None
    attempts: tuple[Attempt, ...]  # at least 1, strictly increasing in time


def build_plan(seed: int, devices: int, days: int, now: datetime) -> list[DevicePlan]:
    """The whole simulation, decided up front. Same arguments, same plan."""
    if devices < 1 or days < 1:
        raise ValueError("devices and days must be at least 1")
    if devices > 1 << 24:
        raise ValueError("at most 16777216 devices (3 free MAC bytes)")
    rng = random.Random(seed)
    now = now.astimezone(timezone.utc).replace(microsecond=0)
    start = now - timedelta(days=days)
    latest = now - MARGIN
    span_s = (latest - start).total_seconds()

    # Three MAC bytes per device, drawn without repetition: MACs are unique.
    suffixes = rng.sample(range(1 << 24), devices)
    weights = [m.share for m in MODELS]
    plan: list[DevicePlan] = []
    for suffix in suffixes:
        spec = rng.choices(MODELS, weights=weights)[0]
        mac = f"{spec.prefix}:{suffix >> 16:02X}:{(suffix >> 8) & 0xFF:02X}:{suffix & 0xFF:02X}"
        assert normalize_mac(mac) == mac  # valid, universally administered, canonical form

        os_version = rng.choice(OS_VERSIONS)
        firmware_version = rng.choice(spec.firmware)
        region = rng.choice(REGIONS)
        has_os, has_fw, has_region, has_time = (rng.random() >= OMIT_SHARE for _ in range(4))

        # Gentle upward trend: density proportional to 1 + t over t in [0, 1]
        # (about 1.7x more activations at the end than at the start).
        t = math.sqrt(1 + 3 * rng.random()) - 1
        times = [start + timedelta(seconds=int(t * span_s))]
        if rng.random() < RESEND_SHARE:
            for _ in range(rng.choice(RESEND_COUNTS)):
                if rng.random() < LATER_DAY_SHARE:
                    delta = timedelta(days=rng.randint(1, 5), minutes=rng.randint(0, 1439))
                else:
                    delta = timedelta(minutes=rng.randint(1, 90))
                nxt = min(times[-1] + delta, latest)
                if nxt - times[-1] < MARGIN:
                    break  # no room left before "now": this device resends less
                times.append(nxt)

        attempts = tuple(
            Attempt(
                at=at,
                # Device clocks are a little off; the server never trusts this.
                device_time=at + timedelta(seconds=rng.randint(-120, 120)) if has_time else None,
            )
            for at in times
        )
        plan.append(
            DevicePlan(
                mac=mac,
                model=spec.name,
                os_version=os_version if has_os else None,
                firmware_version=firmware_version if has_fw else None,
                region=region if has_region else None,
                attempts=attempts,
            )
        )
    rng.shuffle(plan)  # arrival order is random, not model by model
    return plan


def build_body(device: DevicePlan, attempt: Attempt) -> bytes:
    """The exact bytes a device sends (and signs) for one attempt."""
    payload: dict[str, str] = {"device_id": device.mac, "model": device.model}
    if device.os_version is not None:
        payload["os_version"] = device.os_version
    if device.firmware_version is not None:
        payload["firmware_version"] = device.firmware_version
    if device.region is not None:
        payload["region"] = device.region
    if attempt.device_time is not None:
        payload["device_time"] = attempt.device_time.isoformat()
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


@dataclass
class Result:
    unique: int = 0  # devices in the plan
    sent: int = 0  # HTTP requests sent, including re-sends
    retries: int = 0  # re-sends after a network error, 429 or 5xx
    new: int = 0  # 201 responses
    existing: int = 0  # devices whose first success was 200 (already in the database)
    duplicates: int = 0  # 200 responses
    completed: int = 0  # devices that got 200 or 201 for every message
    failures: int = 0
    problems: list[str] = field(default_factory=list)
    setup_error: str | None = None  # set when the run was stopped at once
    elapsed_s: float = 0.0
    # Cross-check against /api/v1/stats (only when credentials were given):
    # how much the server's all-time totals changed during the run.
    delta_activated: int | None = None
    delta_duplicates: int | None = None
    stats_problem: str | None = None  # set when those deltas do not match, or could not be read

    @property
    def ok(self) -> bool:
        return (
            self.setup_error is None
            and not self.problems
            and self.stats_problem is None
            and self.completed == self.unique
        )


async def run_simulation(
    client: httpx.AsyncClient,
    plan: list[DevicePlan],
    master_secret: str,
    *,
    concurrency: int = 20,
    progress: Callable[[int, int], None] | None = None,
    jitter: random.Random | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    dashboard_auth: tuple[str, str] | None = None,
) -> Result:
    """Send every device's messages and check the answers.

    The self-check holds on any database state: no device gets more than one
    201, and every device ends with a 200 or 201. Devices that already
    existed (first success was 200) are reported in `existing`, not a failure.
    Never raises for a server problem; see Result.problems and setup_error.

    With dashboard_auth=(user, password) it also cross-checks the server's
    own numbers: the all-time totals from /api/v1/stats are read before and
    after, and their change must equal the 201 and 200 counts. That holds only
    if nothing else talks to the server during the run.

    jitter times the backoff only and is unseeded by default: it must not
    change the plan. sleep is a parameter so a test can skip the waiting.
    """
    result = Result(unique=len(plan))
    jitter = jitter or random.Random()
    auth = httpx.BasicAuth(*dashboard_auth) if dashboard_auth else None
    before: tuple[int, int] | None = None
    if auth is not None:
        try:
            before = await _read_all_time(client, auth)
        except StatsError as exc:
            result.setup_error = f"stats check: {exc}"
            return result
    queue: asyncio.Queue[DevicePlan] = asyncio.Queue()
    for device in plan:
        queue.put_nowait(device)
    started = time.monotonic()
    finished = 0

    async def worker() -> None:
        nonlocal finished
        while result.setup_error is None:
            try:
                device = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await _run_device(client, device, master_secret, result, jitter, sleep)
            finished += 1
            if progress is not None:
                progress(finished, len(plan))

    async with asyncio.TaskGroup() as group:
        for _ in range(max(1, concurrency)):
            group.create_task(worker())
    result.elapsed_s = time.monotonic() - started
    if before is not None and auth is not None and result.setup_error is None:
        await _cross_check_stats(client, auth, before, result)
    return result


class StatsError(Exception):
    """/api/v1/stats could not be read."""


async def _read_all_time(client: httpx.AsyncClient, auth: httpx.BasicAuth) -> tuple[int, int]:
    """The server's all-time (activated, duplicates) from /api/v1/stats."""
    try:
        response = await client.get(STATS_PATH, auth=auth)
    except httpx.TransportError as exc:
        raise StatsError(f"cannot reach {STATS_PATH}: {exc!r}") from None
    if response.status_code == 401:
        raise StatsError(
            f"{STATS_PATH} answered 401: DASHBOARD_USER / DASHBOARD_PASSWORD "
            "must match the server's"
        )
    if response.status_code != 200:
        raise StatsError(f"{STATS_PATH} answered {response.status_code}: {response.text[:200]}")
    try:
        all_time = response.json()["all_time"]
        return int(all_time["activated"]), int(all_time["duplicates"])
    except (ValueError, KeyError, TypeError):
        raise StatsError(f"{STATS_PATH} answered with an unexpected body") from None


async def _cross_check_stats(
    client: httpx.AsyncClient,
    auth: httpx.BasicAuth,
    before: tuple[int, int],
    result: Result,
) -> None:
    """Compare the change in the server's totals with what the devices saw."""
    try:
        after = await _read_all_time(client, auth)
    except StatsError as exc:
        result.stats_problem = f"could not read the stats after the run: {exc}"
        return
    result.delta_activated = after[0] - before[0]
    result.delta_duplicates = after[1] - before[1]
    if (result.delta_activated, result.delta_duplicates) != (result.new, result.duplicates):
        result.stats_problem = (
            f"stats changed by activated {result.delta_activated:+d} and duplicates "
            f"{result.delta_duplicates:+d}, but the devices saw {result.new} 201 replies and "
            f"{result.duplicates} 200 replies. Any other traffic to the server during the run "
            "(or a reply lost after the server had already counted the message) breaks this "
            "comparison: run the simulator alone against a quiet server."
        )


async def _run_device(
    client: httpx.AsyncClient,
    device: DevicePlan,
    master_secret: str,
    result: Result,
    jitter: random.Random,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    """One device sends its messages strictly in order: N+1 only after N got 200/201."""
    key = signing.device_key(master_secret, device.mac)
    first_success: int | None = None
    for number, attempt in enumerate(device.attempts, start=1):
        if result.setup_error is not None:
            return  # another device found the server ignoring backdating: stop at once
        body = build_body(device, attempt)
        headers = {
            "Content-Type": "application/json",
            "X-Signature": signing.sign(key, body),
            "X-Simulated-Time": attempt.at.isoformat(),
        }
        status, detail = await _send(client, body, headers, result, jitter, sleep)
        if status not in (200, 201):
            result.failures += 1
            result.problems.append(f"{device.mac} message {number}: {detail}")
            return  # no 200/201: the device does not move on
        if status == 201:
            if first_success is not None:
                result.problems.append(
                    f"{device.mac} message {number}: got 201 after an earlier success (double count)"
                )
            result.new += 1
            if not _backdating_honored(detail, attempt.at):
                result.setup_error = (
                    "the server ignored X-Simulated-Time: start it with "
                    "ALLOW_BACKDATING=true (and ENV not production)"
                )
                return
        else:
            result.duplicates += 1
        if first_success is None:
            first_success = status
            if status == 200:
                result.existing += 1
    result.completed += 1


def _backdating_honored(response_text: str, sent: datetime) -> bool:
    """On a 201, first_activated_at must be the time we sent (within 1 second)."""
    try:
        got = datetime.fromisoformat(json.loads(response_text)["first_activated_at"])
        return abs((got - sent).total_seconds()) <= 1
    except (ValueError, KeyError, TypeError):
        return False


async def _send(
    client: httpx.AsyncClient,
    body: bytes,
    headers: dict[str, str],
    result: Result,
    jitter: random.Random,
    sleep: Callable[[float], Awaitable[None]],
) -> tuple[int | None, str]:
    """One message with the device retry rule.

    Returns (200 or 201, response text) on success, else (None, reason).
    Retries on network errors, 429 (waits Retry-After plus a little jitter)
    and 5xx, with exponential backoff and full jitter, at most MAX_TRIES
    tries. Any other 4xx is not retried.
    """
    reason = ""
    for attempt in range(MAX_TRIES):
        if attempt > 0:
            result.retries += 1
        wait = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**attempt) * jitter.random()  # full jitter
        try:
            result.sent += 1
            response = await client.post(ACTIVATIONS_PATH, content=body, headers=headers)
        except httpx.TransportError as exc:
            reason = f"network error: {exc!r}"
        else:
            status = response.status_code
            if status in (200, 201):
                return status, response.text
            reason = f"HTTP {status}: {response.text[:200]}"
            if status == 429:
                wait = _retry_after(response) + jitter.uniform(0, 1)
            elif status < 500:
                return None, reason  # a 4xx other than 429: retrying cannot help
        if attempt < MAX_TRIES - 1:
            await sleep(wait)
    return None, f"gave up after {MAX_TRIES} tries; last: {reason}"


def _retry_after(response: httpx.Response) -> float:
    try:
        return max(0.0, float(response.headers.get("Retry-After", "1")))
    except ValueError:
        return 1.0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.simulate",
        description="Simulate devices activating over the last N days (needs ALLOW_BACKDATING=true on the server).",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--devices", type=int, default=5000)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=20)
    args = parser.parse_args(argv)
    for name in ("devices", "days", "concurrency"):
        if getattr(args, name) < 1:
            parser.error(f"--{name} must be at least 1")
    return args


def _print_summary(result: Result) -> None:
    print(
        f"sent {result.sent} requests ({result.retries} retries) for {result.unique} unique devices\n"
        f"  new (201):                {result.new}\n"
        f"  already existing (200):   {result.existing}\n"
        f"  duplicates (200 replies): {result.duplicates}\n"
        f"  failures:                 {result.failures}\n"
        f"  elapsed:                  {result.elapsed_s:.1f} s"
    )
    if result.delta_activated is not None:
        print(
            f"  stats activated change:   {result.delta_activated:+d} (201 replies: {result.new})\n"
            f"  stats duplicates change:  {result.delta_duplicates:+d} (200 replies: {result.duplicates})"
        )


async def _amain(
    args: argparse.Namespace, master_secret: str, dashboard_auth: tuple[str, str]
) -> int:
    base_url = args.url.rstrip("/")
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        try:
            health = await client.get("/health")
        except httpx.TransportError as exc:
            print(f"cannot reach {base_url}: {exc!r}", file=sys.stderr)
            return 2
        if health.status_code != 200:
            print(f"{base_url}/health answered {health.status_code}, not 200", file=sys.stderr)
            return 2

        plan = build_plan(args.seed, args.devices, args.days, datetime.now(timezone.utc))

        def progress(done: int, total: int) -> None:
            if done % 500 == 0:
                print(f"  {done}/{total} devices", flush=True)

        result = await run_simulation(
            client,
            plan,
            master_secret,
            concurrency=args.concurrency,
            progress=progress,
            dashboard_auth=dashboard_auth,
        )

    _print_summary(result)
    if result.setup_error:
        print(f"stopped: {result.setup_error}", file=sys.stderr)
        return 2
    for problem in result.problems[:10]:
        print(f"problem: {problem}", file=sys.stderr)
    if len(result.problems) > 10:
        print(f"... and {len(result.problems) - 10} more", file=sys.stderr)
    if result.stats_problem:
        print(f"stats cross-check failed: {result.stats_problem}", file=sys.stderr)
    if not result.ok:
        print("SELF-CHECK FAILED", file=sys.stderr)
        return 1
    print(
        "self-check ok: no device counted twice, every device ended with 200 or 201, "
        "and the server's stats changed by exactly the 201 and 200 counts"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # A device only has its key, not DATABASE_URL, so this does not use
    # app.config.load_settings (which requires the database settings).
    load_dotenv(ENV_FILE, override=False)
    master_secret = os.environ.get("MASTER_SECRET", "").strip()
    if not master_secret:
        print(
            "MASTER_SECRET is not set. Copy .env.example to .env and fill it in "
            "(use the same value as the server), or set it in the environment.",
            file=sys.stderr,
        )
        return 2
    # The stats cross-check logs in like the dashboard does.
    dashboard_user = os.environ.get("DASHBOARD_USER", "").strip()
    dashboard_password = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if not dashboard_user or not dashboard_password:
        print(
            "DASHBOARD_USER and DASHBOARD_PASSWORD are not set. The simulator reads "
            "/api/v1/stats to cross-check its counts. Copy .env.example to .env and fill "
            "them in (same values as the server), or set them in the environment.",
            file=sys.stderr,
        )
        return 1
    return asyncio.run(_amain(args, master_secret, (dashboard_user, dashboard_password)))


if __name__ == "__main__":
    sys.exit(main())
