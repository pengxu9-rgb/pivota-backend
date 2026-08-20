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


class PoolCheckoutTimeout(TimeoutError):
    """Waited too long for a free slot in the connection pool.

    A DEDICATED type because the raw signal is untypeable: asyncpg raises a bare
    `TimeoutError` whose `str()` is EMPTY, and every classifier in this module
    matches on substrings — so pool saturation would sail past all of them and
    surface as a 500 INTERNAL_SERVER_ERROR. That is loud in the wrong channel:
    it pages as a code bug rather than DB capacity, and it hands agent/partner
    clients a NON-RETRYABLE status for a state that is entirely retryable.

    Subclasses TimeoutError so any caller already catching that (e.g. the
    canonical route's `_bounded_db`) keeps working unchanged.
    """


def is_pool_checkout_timeout(err: BaseException) -> bool:
    """True if `err` (or anything it wraps) is a pool-checkout timeout."""
    seen: Set[int] = set()
    cur: Optional[BaseException] = err
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, PoolCheckoutTimeout):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


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
        # Type check FIRST: a pool-checkout timeout carries an empty message,
        # so no substring test can ever see it.
        if isinstance(cur, PoolCheckoutTimeout):
            return True
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
