from __future__ import annotations

from typing import Optional, Set

from fastapi import HTTPException


_ASYNC_PG_BUSY_SUBSTR = "another operation is in progress"
_ASYNC_PG_POOL_CLOSING_SUBSTR = "pool is closing"
_ASYNC_PG_POOL_CLOSED_SUBSTR = "pool is closed"


def _matches_in_cause_chain(err: BaseException, substrings: tuple) -> bool:
    seen: Set[int] = set()
    cur: Optional[BaseException] = err
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text = str(cur).lower()
        if any(sub in text for sub in substrings):
            return True
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
    return False


def is_asyncpg_pool_gone_error(err: BaseException) -> bool:
    """True only when the error says the POOL ITSELF is closed or closing.

    Deliberately NARROWER than `is_asyncpg_busy_error`, which also matches
    "another operation is in progress" — a busy or poisoned CONNECTION, which
    says nothing about the pool. Callers use this one to decide whether to tear
    a pool down, and that decision must never be made on an ambiguous signal:
    rebuilding a live pool abandons it, and its server connections then stay
    open until Postgres reaps them (the 2026-08-18 wedge).
    """
    return _matches_in_cause_chain(
        err, (_ASYNC_PG_POOL_CLOSED_SUBSTR, _ASYNC_PG_POOL_CLOSING_SUBSTR)
    )


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
        cur_text = str(cur).lower()
        if _ASYNC_PG_BUSY_SUBSTR in cur_text:
            return True
        if _ASYNC_PG_POOL_CLOSING_SUBSTR in cur_text:
            return True
        if _ASYNC_PG_POOL_CLOSED_SUBSTR in cur_text:
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
