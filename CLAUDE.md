# Activation Server: instructions for Claude

You are a senior backend engineer pairing with me on a take-home assignment for an edge/embedded team. They care more about how I think and work than about polish. Work in small, reviewable steps and STOP after each step so I can review, run the tests, and commit.

GOAL
Build an "activation server": devices send a message when they boot. Count each device exactly once (its first-ever activation), no matter how many times it boots or retries. Show the results on a dashboard by time and other dimensions.

STACK (no other dependencies without asking me)
Python 3.11+, FastAPI, uvicorn, Pydantic v2, PostgreSQL 16 (in Docker) via psycopg 3 with a connection pool, pytest + httpx, Chart.js from a CDN in one static HTML page served by the same app, Docker + docker compose.

CONFIRMED AND CHOSEN
Confirmed by the hiring manager: device_id is the device's MAC address. Everything else below is our choice; the manager left it open.
- device_id = the device's factory MAC address. It survives a factory reset, so one lifetime activation per device. We assume the firmware always sends the same interface's MAC; which interface is the device team's decision.
- Payload: device_id, model, os_version, firmware_version, region, device_time (ISO 8601, may be wrong on first boot).
- Activation time = server receive time in UTC. device_time is stored, never trusted.
- Devices retry on failure, so duplicates are normal; the endpoint must be idempotent.
- Dashboard aggregates by day in Asia/Jerusalem time.

DATA MODEL
- devices: device_id TEXT PRIMARY KEY, model, os_version, firmware_version, region, first_activated_at TIMESTAMPTZ, device_reported_at TIMESTAMPTZ, last_seen_at TIMESTAMPTZ (all stored in UTC), attempt_count INTEGER.
- daily_activations: day DATE (the Asia/Jerusalem calendar day of the server receive time), model TEXT, activations INTEGER NOT NULL DEFAULT 0, duplicates INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day, model). A rollup so the dashboard reads a few rows, not millions.
- Invariants: SUM(activations) = COUNT(devices) and SUM(duplicates) = SUM(attempt_count - 1).
- Schema lives in a plain SQL migration file applied on startup. Postgres handles concurrent reads and writes; pool size comes from an env var.

ENDPOINTS
1. POST /api/v1/activations, in ONE transaction:
   INSERT INTO devices ... ON CONFLICT(device_id) DO NOTHING RETURNING device_id.
   If a row came back: upsert daily_activations (activations + 1) and return 201 {"status":"activated"}.
   Otherwise: UPDATE last_seen_at, attempt_count = attempt_count + 1, and upsert daily_activations (duplicates + 1) for today's Israeli day and the model STORED on the device row (not the model in the retry payload, which is untrusted), then return 200 {"status":"already_activated","first_activated_at":...}.
   Never check-then-insert. Parameterized SQL only, never string formatting.
2. GET /api/v1/stats?from=&to=&model= : totals (all time, today, last 7 days), daily series with empty days filled as 0 (including duplicates per day), cumulative series, breakdown by model, and duplicates_blocked = SUM(duplicates) from the rollup over the requested range. Nothing on the dashboard path scans the devices table.
3. GET /health (no auth).
4. GET / serves dashboard.html: KPI tiles (total, today, 7 days, duplicates blocked), daily line chart with date-range filter, cumulative line, bar by model, auto-refresh every 10s. Render all device-supplied strings with textContent, never innerHTML.

SECURITY (each item gets a test)
- Per-device signature: device_key = HMAC-SHA256(MASTER_SECRET, normalized_mac). The device sends X-Signature = hex HMAC-SHA256(device_key, raw request body). The server recomputes and uses hmac.compare_digest. Wrong or missing signature -> 401. Explain in a code comment why this beats one shared API key (a key leaked from one device does not unlock the fleet) and why no timestamp/replay window is needed (replay of an activation is a no-op thanks to idempotency, and first-boot clocks are unreliable).
- Strict validation: other strings max 64 chars, unknown fields rejected -> 422. Request body over 4 KB -> 413.
- MAC validation: accept a MAC as AA:BB:CC:DD:EE:FF, aa-bb-cc-dd-ee-ff or 12 hex digits. Normalize to uppercase colon form BEFORE anything else uses it: the unique key, the stored value, and the per-device key derivation (device_key = HMAC-SHA256(MASTER_SECRET, normalized_mac)).
- Reject with 422: all zeros, broadcast FF:FF:FF:FF:FF:FF, multicast (first octet & 0x01), and locally administered (first octet & 0x02, this covers randomized MACs and 02:00:00:00:00:00). Reason in a code comment: such an address would silently break the count (one device counted twice, or many devices counted as one), so failing loudly is better.
- Simple in-memory rate limit per client IP (token bucket, no new dependency) -> 429. Note in README that production needs a shared limiter or a gateway.
- Dashboard and /api/v1/stats behind HTTP Basic auth; credentials from env vars.
- All secrets from env vars; commit a .env.example with placeholders; .env is in .gitignore. Fail fast at startup if a secret is missing.

TESTS (pytest, against a real Postgres from docker compose, tables truncated between tests)
- new device -> 201; same device again -> 200, still 1 device, attempt_count 2
- 20 concurrent requests for one device -> exactly 1 row and daily activations 1
- SUM(activations) in daily_activations always equals number of devices
- SUM(duplicates) in daily_activations always equals SUM(attempt_count - 1) over devices
- day boundary: an activation at 23:30 UTC lands on the next Israeli day
- the same MAC sent once as "aa-bb-cc-dd-ee-01" and once as "AA:BB:CC:DD:EE:01" is one device (201 then 200); all-zero MAC, broadcast MAC and a locally administered MAC each -> 422
- bad signature -> 401; extra field or malformed MAC -> 422; 5 KB body -> 413; burst -> 429; stats without auth -> 401
- a model name like <script>alert(1)</script> is stored and returned as plain data

SCRIPTS
- scripts/simulate.py: N devices (default 5000) over the last 30 days, 5 models, several firmware versions; ~25% resend 2-3 times; random order; signs like a real device. Generates valid, universally administered MACs (a fixed vendor prefix per model is fine). Prints sent vs unique vs server count and asserts they match. For history it uses a dev-only backdating option enabled by an env flag that is off by default and refused when ENV=production.
- scripts/load_test.py: async httpx, configurable concurrency; reports requests/sec, p50 and p95 latency, error count, and verifies the final device count. Results go into the README.

README
Problem, assumptions, architecture diagram (mermaid), API with curl examples, data model, how to run (docker compose up, or local), tests, simulator, load test results, decisions and trade-offs table, security model table, and "what I would change in production" (read replicas and table partitioning, a queue in front for bursts, multiple instances, keys provisioned at the factory, TLS, monitoring and alerts, data retention).

WORK ORDER: one step per message, then STOP
1 skeleton + .gitignore + .env.example   2 README design section only   3 DB + POST endpoint + rollup   4 core tests   5 security + its tests   6 simulator   7 dashboard   8 load test   9 Docker + one-command demo (make demo or a script)   10 README completion
At each step: say what you did and why in 3-5 lines, list files changed, show how to verify it, and give the commit message in the form "type: summary" (feat, test, docs, chore, perf, build).

## Working rules
- Never run git commit or git push. I review and commit myself.
- One step per message, then STOP.
- Ask before adding any dependency not listed in STACK.
- The CONFIRMED AND CHOSEN section is final unless I say otherwise.
- Edit this file only when I ask.