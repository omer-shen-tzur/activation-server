"""Settings from environment variables, with .env loaded for local runs.

Each step reads only the settings it uses, so a setting needed by a later
feature can never stop the current one from starting. Missing or invalid
values raise ConfigError at startup (in the app's lifespan), never later in
the middle of a request.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

DEFAULT_DB_POOL_SIZE = 10
DEFAULT_RATE_LIMIT_PER_MINUTE = 60


class ConfigError(RuntimeError):
    """A required setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    db_pool_size: int
    # Root of the per-device keys (app/signing.py). repr=False keeps it out
    # of logs and tracebacks that print the settings.
    master_secret: str = field(repr=False)
    rate_limit_per_minute: int


def load_settings() -> Settings:
    # Variables already in the environment win over .env (override=False),
    # so tests and Docker can set DATABASE_URL without editing the file.
    # A missing .env is fine: in Docker the values come from the environment.
    load_dotenv(ENV_FILE, override=False)
    return Settings(
        database_url=_required("DATABASE_URL"),
        db_pool_size=_positive_int("DB_POOL_SIZE", DEFAULT_DB_POOL_SIZE),
        master_secret=_required("MASTER_SECRET"),
        rate_limit_per_minute=_positive_int(
            "RATE_LIMIT_PER_MINUTE", DEFAULT_RATE_LIMIT_PER_MINUTE
        ),
    )


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in, "
            "or set it in the environment."
        )
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}.") from None
    if value < 1:
        raise ConfigError(f"{name} must be at least 1, got {value}.")
    return value
