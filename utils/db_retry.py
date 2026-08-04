"""Transient-DB retry helper shared by the charge paths.

Moved out of routes/agent_payment_sdk (where it was a route-private helper) so
SERVICES can use it without importing a route module: services/
acp_offsession_payment needs exactly this posture for its post-capture writes,
and a service reaching back into a route for a utility is an import cycle
waiting to happen. routes/agent_payment_sdk re-exports the same object, so its
own call sites (and anything importing the old private name) are unchanged.

Behavior is byte-equivalent to the original helper — same attempt count, same
backoff, same `db_busy_http_exception()` on exhaustion, same log line.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional, TypeVar

from utils.logger import logger
from utils.transient_errors import db_busy_http_exception, is_asyncpg_busy_error

_T = TypeVar("_T")


async def with_asyncpg_busy_retry(
    label: str,
    operation: Callable[[], Awaitable[_T]],
    *,
    attempts: int = 2,
    base_delay_seconds: float = 0.05,
) -> _T:
    """
    Retry only local DB operations that are safe to replay.

    Do not wrap PSP/Stripe creation with this helper. Once a PSP call may have
    happened, retry the following DB write independently so idempotency stays
    anchored to the original PaymentIntent.
    """
    last_exc: Optional[BaseException] = None
    total_attempts = max(1, int(attempts or 1))
    for attempt in range(total_attempts):
        try:
            return await operation()
        except Exception as exc:
            if not is_asyncpg_busy_error(exc):
                raise
            last_exc = exc
            if attempt >= total_attempts - 1:
                break
            logger.warning(
                "[AgentPayments] transient asyncpg state during %s; retrying once",
                label,
            )
            try:
                await asyncio.sleep(max(0.0, base_delay_seconds) * (attempt + 1))
            except Exception:
                pass
    raise db_busy_http_exception() from last_exc


# Historical private name — kept so existing imports keep resolving.
_with_asyncpg_busy_retry = with_asyncpg_busy_retry
