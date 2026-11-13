-- 001_init.sql: initial schema.
--
-- The app applies this file on every startup (app/db.py, in one
-- transaction), so every statement must be safe to run again:
-- CREATE ... IF NOT EXISTS.
--
-- The app runs as ONE process, so only one copy of this runs at a time.
-- With several workers or instances starting together, two could run it
-- at once, and concurrent CREATE TABLE IF NOT EXISTS can still fail on a
-- system catalog unique index. That setup needs a lock around the migration
-- (for example pg_advisory_lock) or a separate migrate step that runs once
-- before the app starts.
--
-- All timestamps are TIMESTAMPTZ: Postgres stores them as UTC instants.

-- One row per device. device_id is the normalized MAC (AA:BB:CC:DD:EE:FF);
-- this primary key is what guarantees each device is counted once.
-- The row is a snapshot of the first activation: a retry only updates
-- last_seen_at and attempt_count.
CREATE TABLE IF NOT EXISTS devices (
    device_id          TEXT        PRIMARY KEY,
    model              TEXT        NOT NULL,
    os_version         TEXT,
    firmware_version   TEXT,
    region             TEXT,
    first_activated_at TIMESTAMPTZ NOT NULL,  -- server receive time
    device_reported_at TIMESTAMPTZ,           -- device_time as sent, never trusted
    last_seen_at       TIMESTAMPTZ NOT NULL,  -- server receive time of the latest message
    attempt_count      INTEGER     NOT NULL CHECK (attempt_count >= 1)
);

-- Rollup the dashboard reads instead of scanning devices. It is updated in
-- the same transaction as devices, so these always hold:
--   SUM(activations) = COUNT(*) FROM devices
--   SUM(duplicates)  = SUM(attempt_count - 1) FROM devices
-- day is the Asia/Jerusalem calendar day of the server receive time.
-- model is the model stored on the device row.
CREATE TABLE IF NOT EXISTS daily_activations (
    day         DATE    NOT NULL,
    model       TEXT    NOT NULL,
    activations INTEGER NOT NULL DEFAULT 0 CHECK (activations >= 0),
    duplicates  INTEGER NOT NULL DEFAULT 0 CHECK (duplicates >= 0),
    PRIMARY KEY (day, model)
);
