"""Runtime safety helpers for production-only cleanup/debug gates."""

from __future__ import annotations

import os

from fastapi import HTTPException


def env_flag(name: str) -> bool:
    return (os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"})


def is_production_runtime() -> bool:
    return (
        os.getenv("APP_ENV", "").strip().lower() == "production"
        or os.getenv("ENVIRONMENT", "").strip().lower() == "production"
        or bool(os.getenv("RAILWAY_GIT_COMMIT_SHA"))
    )


def require_runtime_gate(flag_name: str, *, production_status_code: int = 404) -> None:
    """Disable a route in production unless a narrow opt-in flag is set."""
    if is_production_runtime() and not env_flag(flag_name):
        raise HTTPException(status_code=production_status_code, detail="Not found")


def configured_env_value(name: str) -> str:
    return os.getenv(name, "").strip()
