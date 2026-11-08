"""Runtime safety helpers for production-only cleanup/debug gates."""

from __future__ import annotations

import logging
import os
from typing import Callable

from fastapi import HTTPException

from config.platform import is_deployed, is_production

logger = logging.getLogger(__name__)


def env_flag(name: str) -> bool:
    return (os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"})


def is_production_runtime() -> bool:
    return (
        os.getenv("APP_ENV", "").strip().lower() == "production"
        or os.getenv("ENVIRONMENT", "").strip().lower() == "production"
        # `bool(RAILWAY_GIT_COMMIT_SHA)` meant "Railway injected build metadata,
        # so I am deployed" — it is False on Cloud Run, which would re-open every
        # route this gate closes.
        or is_deployed()
    )


def require_runtime_gate(flag_name: str, *, production_status_code: int = 404) -> None:
    """Disable a route in production unless a narrow opt-in flag is set."""
    if is_production_runtime() and not env_flag(flag_name):
        raise HTTPException(status_code=production_status_code, detail="Not found")


def configured_env_value(name: str) -> str:
    return os.getenv(name, "").strip()


def refuse_in_production(surface: str, *, status_code: int = 404) -> Callable[[], None]:
    """A route dependency that refuses `surface` in PRODUCTION, whatever any env flag says.

    Production is `config.platform.is_production()` (PIVOTA_ENV, else the Railway environment, else
    fail-closed to production on any Cloud Run / Railway host), NOT `is_production_runtime()`, which
    is true on every deployed host including staging: staging and local keep their behaviour. There
    is no opt-in flag on purpose: these surfaces must not be one env value away from live.
    """

    def _refuse_in_production() -> None:
        if is_production():
            logger.warning("runtime_safety: %s refused in production (%s)", surface, status_code)
            raise HTTPException(status_code=status_code, detail="Not found")

    _refuse_in_production.__name__ = f"refuse_in_production_{surface}"
    return _refuse_in_production
