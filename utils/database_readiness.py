from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import HTTPException, status

from db.database import database
from utils.transient_errors import is_asyncpg_busy_error


logger = logging.getLogger(__name__)


class DatabaseUnavailableError(RuntimeError):
    def __init__(self, *, phase: str, error_type: str, message: str):
        super().__init__(message)
        self.phase = phase
        self.error_type = error_type
        self.message = message


def database_unavailable_http_exception(*, retry_after_seconds: int = 1) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        headers={"Retry-After": str(max(1, int(retry_after_seconds)))},
        detail={
            "error": "TEMPORARY_UNAVAILABLE",
            "message": "Temporary database unavailable. Please retry shortly.",
        },
    )


def _force_mark_database_disconnected() -> None:
    """
    Repair local `databases.Database` state after asyncpg reports a closed pool.

    `databases` leaves `is_connected=True` if backend disconnect times out or fails.
    In that state future `connect()` calls are skipped while requests keep raising
    `InterfaceError: pool is closed`. Force the wrapper back to disconnected so the
    next readiness pass creates a fresh pool.
    """
    try:
        database.is_connected = False
    except Exception:
        pass

    backend = getattr(database, "_backend", None)
    if backend is not None and hasattr(backend, "_pool"):
        try:
            backend._pool = None
        except Exception:
            pass


async def ensure_database_ready(
    *,
    connect_timeout_seconds: float = 3.0,
    probe_timeout_seconds: float = 3.0,
    disconnect_timeout_seconds: float = 1.0,
) -> None:
    """
    Best-effort request-time database recovery.

    Production startup is intentionally allowed to continue in degraded mode when DB
    connection times out. That keeps the service deployable, but quote/order flows need
    a way to recover later when the DB becomes reachable again.
    """

    async def _connect_once() -> None:
        try:
            await asyncio.wait_for(database.connect(), timeout=connect_timeout_seconds)
        except Exception as exc:  # pragma: no cover - covered via outer behavior tests
            raise DatabaseUnavailableError(
                phase="connect",
                error_type=type(exc).__name__,
                message="database connect failed",
            ) from exc

    async def _probe_once(phase: str) -> None:
        try:
            await asyncio.wait_for(database.execute("SELECT 1"), timeout=probe_timeout_seconds)
        except Exception as exc:
            raise DatabaseUnavailableError(
                phase=phase,
                error_type=type(exc).__name__,
                message="database readiness probe failed",
            ) from exc

    if not getattr(database, "is_connected", False):
        await _connect_once()

    try:
        await _probe_once("probe")
        return
    except DatabaseUnavailableError as first_err:
        logger.warning(
            "Database readiness probe failed; attempting one reconnect",
            extra={"phase": first_err.phase, "error_type": first_err.error_type},
        )

    try:
        if getattr(database, "is_connected", False):
            await asyncio.wait_for(database.disconnect(), timeout=disconnect_timeout_seconds)
    except Exception as exc:
        logger.warning(f"Database disconnect during recovery failed: {exc}")
    finally:
        _force_mark_database_disconnected()

    await _connect_once()

    try:
        await _probe_once("reconnect_probe")
    except DatabaseUnavailableError as exc:
        if is_asyncpg_busy_error(exc.__cause__ or exc):
            _force_mark_database_disconnected()
        raise
