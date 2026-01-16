from __future__ import annotations

from typing import Optional, Set

from fastapi import HTTPException


_ASYNC_PG_BUSY_SUBSTR = "another operation is in progress"


def is_asyncpg_busy_error(err: BaseException) -> bool:
    """
    asyncpg raises `InterfaceError: cannot perform operation: another operation is in progress`
    when a pooled connection is busy/poisoned (often after cancellations/timeouts).
    Treat as transient and return a retryable response.
    """
    seen: Set[int] = set()
    cur: Optional[BaseException] = err
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if _ASYNC_PG_BUSY_SUBSTR in str(cur):
            return True
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
    return False


def db_busy_http_exception(*, retry_after_seconds: int = 1) -> HTTPException:
    return HTTPException(
        status_code=503,
        headers={"Retry-After": str(max(1, int(retry_after_seconds)))},
        detail={
            "error": "TEMPORARY_UNAVAILABLE",
            "message": "Temporary database busy. Please retry shortly.",
        },
    )

