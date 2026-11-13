# Activation Server: instructions for Claude

You are a senior backend engineer pairing with me on a take-home assignment for an edge/embedded team. They care more about how I think and work than about polish. Aim for simple and correct, not impressive. Work in small, reviewable steps and STOP after each step so I can review, run the tests, and commit.

GOAL
Build an "activation server": devices send a message when they boot. Count each device exactly once (its first-ever activation), no matter how many times it boots or retries. Show the results on a dashboard by time and other dimensions.

STACK (no other dependencies without asking me)
Python 3.11+, FastAPI, uvicorn, Pydantic v2, python-dotenv, PostgreSQL 16 (in Docker) via psycopg 3 with an async connection pool, pytest + httpx, Chart.js from a CDN in one static HTML page served by the same app, Docker + docker compose. I work on Windows: every command and script must work there (no make, no bash-only scripts).
On Windows the server and the tests must run on a SelectorEventLoop: psycopg's async mode does not work on the default ProactorEventLoop. `python -m app` starts the server that way.

CONFIRMED AND CHOSEN
Confirmed by the hiring manager: device_id is the device's MAC address, which is unique. Everything else below is our choice; the manager left it open.
- device_id = the device's factory MAC address. Our reading: it survives a factory reset, so one lifetime activation per device. We assume the firmware always sends the same interface's MAC; which interface is the device team's decision.
- Payload: device_id and model are required. os_version, firmware_version, region and device_time (ISO 8601) are optional. Rejecting a real activation because of a missing optional field would lose a count, which is the worst failure here.
- Activation time = server receive time in UTC. device_time is stored, never trusted; a naive device_time is treated as UTC.
- Devices retry on failure, so duplicates are normal; the endpoint must be idempotent.
- The first activation is a snapshot: a retry never overwrites model, versions, region or device_time. It only updates last_seen_at and attempt_count.
- Dashboard aggregates by day in Asia/Jerusalem time.
- The Asia/Jerusalem day is computed inside SQL (AT TIME ZONE 'Asia/Jerusalem'), not in Python, so no tzdata dependency is needed on Windows.

DATA MODEL
- devices: device_id TEXT PRIMARY KEY, model TEXT NOT NULL, os_version, firmware_version, region, first_activated_at TIMESTAMPTZ NOT NULL, device_reported_at TIMESTAMPTZ, last_seen_at TIMESTAMPTZ NOT NULL (all stored in UTC), attempt_count INTEGER NOT NULL CHECK (attempt_count >= 1).
- daily_activations: day DATE (the Asia/Jerusalem calendar day of the server receive time), model TEXT, activations INTEGER NOT NULL DEFAULT 0 CHECK (>= 0), duplicates INTEGER NOT NULL DEFAULT 0 CHECK (>= 0), PRIMARY KEY(day, model). A rollup so the dashboard reads a few rows, not millions.
- Invariants: SUM(activations) = COUNT(devices) and SUM(duplicates) = SUM(attempt_count - 1).
- Schema lives in a plain SQL migration file (CREATE TABLE IF NOT EXISTS) applied on startup. The app runs as one process; note in a comment that several workers would need a lock around the migration.

ENDPOINTS
1. POST /api/v1/activations, in ONE transaction:
   INSERT INTO devices ... ON CONFLICT(device_id) DO NOTHING RETURNING device_id, first_activated_at.
   If a row came back: upsert daily_activations (activations + 1) and return 201 {"status":"activated","first_activated_at":...}.
   Otherwise: UPDATE last_seen_at, attempt_count = attempt_count + 1 RETURNING model, first_activated_at, then upsert daily_activations (duplicates + 1) for today's Israeli day and the model STORED on the device row (not the model in the retry payload, which is untrusted), and return 200 {"status":"already_activated","first_activated_at":...}.
   Never check-then-insert. Parameterized SQL only, never string formatting.
   If the database is unavailable, return 503 so the device retries. Never a bare 500 for that case.
2. GET /api/v1/stats?from=&to=&model= : totals (all time, today, last 7 days, in Israel days), daily series with empty days filled as 0 (activations and duplicates per day), cumulative series that starts from the all-time total BEFORE the range (not from 0), breakdown by model, and duplicates_blocked = SUM(duplicates) over the range. Default range: last 30 days. from must be <= to, and the range is capped at 366 days (422 otherwise). Nothing on the dashboard path scans the devices table.
3. GET /health (no auth): runs SELECT 1; 200 if the database answers, 503 if not.
4. GET / serves dashboard.html: KPI tiles (total, today, 7 days, duplicates blocked), daily line chart with date-range filter, duplicates per day, cumulative line, bar by model, auto-refresh every 10s, zeros (not errors) when there is no data. Render all device-supplied strings with textContent, never innerHTML.

CLOCK
All code gets "now" from ONE function (a FastAPI dependency). Tests override it to place an activation at a chosen instant. The simulator's dev-only backdating (step 6) is built on the same function.

VALIDATION (step 3, part of counting correctly)
- Unknown fields rejected -> 422. Optional strings max 64 chars. model is stripped of surrounding whitespace and must be 1-64 chars after stripping.
- MAC: accept AA:BB:CC:DD:EE:FF, aa-bb-cc-dd-ee-ff or 12 hex digits. Normalize to uppercase colon form BEFORE anything else uses it: the unique key, the stored value, and (from step 5) the per-device key derivation.
- Reject with 422: all zeros, broadcast FF:FF:FF:FF:FF:FF, multicast (first octet & 0x01), and locally administered (first octet & 0x02, this covers randomized MACs and 02:00:00:00:00:00). Reason in a code comment: such an address would silently break the count (one device counted twice, or many devices counted as one), so failing loudly is better.

SECURITY (step 5 for the activation endpoint, step 7 for the dashboard; each item gets a test)
- Check order on POST, cheapest first: body size, rate limit, validation, signature. The signature is last because the device key is derived from the MAC inside the body.
- Body over 4 KB -> 413. Enforce on the bytes actually read, not only the Content-Length header.
- Per-client-IP in-memory token bucket (no new dependency) -> 429. Limit from RATE_LIMIT_PER_MINUTE. Note in README: per process, resets on restart, and behind a proxy every client shares one IP; production needs a shared limiter or a gateway.
- Per-device signature: device_key = HMAC-SHA256(MASTER_SECRET, normalized_mac). The device sends X-Signature = hex HMAC-SHA256(device_key, raw request body), computed over the exact bytes received, never a re-serialized body. The server uses hmac.compare_digest. Wrong or missing signature -> 401. Key derivation and signing live in ONE module that the scripts import. Explain in a code comment why this beats one shared API key (a key leaked from one device does not unlock the fleet) and why no timestamp/replay window is needed (replay of an activation is a no-op thanks to idempotency, and first-boot clocks are unreliable).
- Dashboard and /api/v1/stats behind HTTP Basic auth (step 7), credentials from env vars, compared with hmac.compare_digest.
- All secrets from env vars; .env.example has placeholders; .env is in .gitignore. Fail fast at startup if a required setting is missing. Each step requires only the settings it uses.

TESTS (pytest, against a real Postgres from docker compose)
- Tests use a SEPARATE database (TEST_DATABASE_URL, created by the compose init script) and truncate tables between tests, so they never wipe dev or simulator data.
- Every test request goes through ONE helper. From step 5 that helper signs the body, so earlier tests keep passing unchanged.
- Concurrency tests must be truly concurrent against the database (asyncio.gather over an async client with the app's lifespan running), not sequential calls.
- Step 4 (core): new device -> 201; same device again -> 200, still 1 device, attempt_count 2; 20 concurrent requests for one device -> exactly 1 row and daily activations 1; 20 concurrent requests for 20 different devices of one model -> daily activations 20; a retry with a different model counts its duplicate under the stored model; both invariants checked after every scenario; an activation at 23:30 UTC lands on the next Israeli day; the same MAC as "aa-bb-cc-dd-ee-01" then "AA:BB:CC:DD:EE:01" is one device (201 then 200); all-zero, broadcast, multicast and locally administered MACs -> 422; extra field or malformed MAC -> 422; missing or blank model -> 422; a model like <script>alert(1)</script> is stored as plain data.
- Step 5: bad signature -> 401; missing signature -> 401; a body signed with ANOTHER device's key -> 401; 5 KB body -> 413; burst over the limit -> 429. Tests run with a high limit; the 429 test sets a low one.
- Step 6: backdating is ignored when ALLOW_BACKDATING is off; startup fails when ALLOW_BACKDATING=true and ENV=production.
- Step 7: stats correct for known data, empty days filled with 0, cumulative series starts from the prior total, bad or oversized range -> 422, stats and dashboard without auth -> 401, the script-like model name comes back as a plain string.

SCRIPTS
- scripts/simulate.py (step 6): N devices (default 5000) over the last 30 days, 5 models, several firmware versions; ~25% resend 2-3 times, some of them on a later day; random order with a --seed option for reproducible runs; signs like a real device using the shared signing module. Generates valid, universally administered MACs (a fixed vendor prefix per model is fine). Device rule: retry on network error, 429 or 5xx with exponential backoff plus jitter; mark a device activated only after 200 or 201. Self-check from responses: the number of 201 responses must equal the number of unique devices. Prints sent vs unique vs activated and exits non-zero on mismatch. History uses a dev-only backdating header honored only when ALLOW_BACKDATING=true and ENV is not production, off by default.
- Demo (step 9): a Python script (Windows-friendly) that starts compose, waits for /health, runs the simulator, and prints the dashboard URL.

DOCKER
- Step 3: docker-compose.yml with the db service only: postgres:16, port bound to 127.0.0.1 only, a named volume, a pg_isready healthcheck, and an init script that creates the test database.
- Step 9: add the app service: slim Python image, runs as a non-root user, starts after db is healthy, DATABASE_URL uses the db service name (not localhost).

README
Problem, assumptions, architecture diagram (mermaid), API with curl examples, data model, how to run (docker compose up, or local), tests, simulator, decisions and trade-offs table, security model table, and "what I would change in production" (read replicas and table partitioning, a queue in front for bursts, multiple instances, keys provisioned at the factory, TLS, monitoring and alerts, data retention).

WORK ORDER: one step per message, then STOP
1 skeleton + .gitignore + .env.example (done)
2 README design section only (done)
3 docker compose (db only, plus test DB) + migration + async pool + POST endpoint + rollup + validation and MAC normalization + clock dependency + /health
4 core tests (see TESTS)
5 activation endpoint protection: size limit, rate limit, per-device signature, + tests; the test helper starts signing
6 simulator with retries and backoff, dev-only backdating, + tests
7 stats API + dashboard + Basic auth, + tests
9 app in Docker + one-command demo script
10 README completion, check that no secret is in git history, fresh-clone run following the README
At each step: say what you did and why in 3-5 lines, list files changed, show how to verify it, and give the commit message in the form "type: summary" (feat, test, docs, chore, perf, build).

## Working rules
- Never run git commit or git push. I review and commit myself.
- One step per message, then STOP.
- Ask before adding any dependency not listed in STACK.
- The CONFIRMED AND CHOSEN section is final unless I say otherwise.
- Edit this file only when I ask.
