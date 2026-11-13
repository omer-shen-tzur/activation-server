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
# The DASHBOARD_PASSWORD value in .env.example (a test keeps them in sync).
# Anyone who has read the repo knows it, so production must not use it.
DASHBOARD_PASSWORD_PLACEHOLDER = "change-me-too"


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
    # HTTP Basic credentials for /api/v1/stats and the dashboard page.
    dashboard_user: str
    dashboard_password: str = field(repr=False)
    env: str  # "development" or "production"
    allow_backdating: bool  # dev only; never true together with env == "production"


def load_settings() -> Settings:
    # Variables already in the environment win over .env (override=False),
    # so tests and Docker can set DATABASE_URL without editing the file.
    # A missing .env is fine: in Docker the values come from the environment.
    load_dotenv(ENV_FILE, override=False)
    env = _env_name()
    allow_backdating = _bool("ALLOW_BACKDATING", default=False)
    if allow_backdating and env == "production":
        # A fake activation time in production would corrupt the history, so
        # refuse to start rather than quietly ignore the setting.
        raise ConfigError(
            "ALLOW_BACKDATING=true is not allowed when ENV=production. "
            "Set ALLOW_BACKDATING=false (or ENV=development for local simulation)."
        )
    dashboard_password = _required("DASHBOARD_PASSWORD")
    if env == "production" and dashboard_password == DASHBOARD_PASSWORD_PLACEHOLDER:
        raise ConfigError(
            "DASHBOARD_PASSWORD is still the .env.example placeholder, which is not allowed "
            "when ENV=production. Set a real password."
        )
    return Settings(
        database_url=_required("DATABASE_URL"),
        db_pool_size=_positive_int("DB_POOL_SIZE", DEFAULT_DB_POOL_SIZE),
        master_secret=_required("MASTER_SECRET"),
        rate_limit_per_minute=_positive_int(
            "RATE_LIMIT_PER_MINUTE", DEFAULT_RATE_LIMIT_PER_MINUTE
        ),
        dashboard_user=_required("DASHBOARD_USER"),
        dashboard_password=dashboard_password,
        env=env,
        allow_backdating=allow_backdating,
    )


def _env_name() -> str:
    raw = os.environ.get("ENV", "").strip().lower()
    if not raw:
        return "development"
    if raw not in ("development", "production"):
        raise ConfigError(f"ENV must be 'development' or 'production', got {raw!r}.")
    return raw


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw not in ("true", "false"):
        raise ConfigError(f"{name} must be 'true' or 'false', got {raw!r}.")
    return raw == "true"


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
