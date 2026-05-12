"""P2.5b — audit telemetry context.

`record_probe_run` needs `audit_run_id` + `merchant_id` so probe-run
telemetry can be aggregated into `cost_summary_jsonb` per audit. But
those identifiers are owned by the worker (services/audit_run_worker.py)
and the probe call sites are 4–6 stack frames deep inside async code
that doesn't currently take them as parameters.

Threading them through every signature would be invasive (touches
every probe + scoring helper). Instead this module exposes them via
`contextvars.ContextVar`, which propagates correctly through:
  - `await` boundaries (asyncio integrates with contextvars natively)
  - `asyncio.gather` (each spawned coroutine inherits the context)
  - `asyncio.create_task` (same — inherits context at task creation)

Pattern at the worker:
    from services.audit_telemetry_context import audit_telemetry
    async with audit_telemetry(run_id=run_id, merchant_id=merchant_id):
        await run_brand_report(...)

Pattern at probe call sites:
    from services.audit_telemetry_context import current_audit_context
    ctx = current_audit_context()
    await record_probe_run(
        ...,
        audit_run_id=ctx.audit_run_id,
        merchant_id=ctx.merchant_id,
    )

Outside an `audit_telemetry()` block (e.g., one-off BD interactive
lookups, scripts), `current_audit_context()` returns AuditContext
with both fields = None — `record_probe_run` accepts None for both,
so the row is still recorded as a one-off for global cost-cap
accounting.
"""

from __future__ import annotations

import contextvars
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AuditContext:
    """Snapshot of the audit identifiers in scope at the call site.
    Frozen so callers can't accidentally mutate the contextvar value."""
    audit_run_id: Optional[str] = None
    merchant_id: Optional[str] = None


_AUDIT_CONTEXT: contextvars.ContextVar[AuditContext] = (
    contextvars.ContextVar(
        "pivota_audit_context",
        default=AuditContext(),
    )
)


def current_audit_context() -> AuditContext:
    """Read the audit context for the current async stack. Returns
    AuditContext(None, None) when no `audit_telemetry()` block is
    active — call sites should treat that as 'no audit context' and
    pass None for both fields to record_probe_run."""
    return _AUDIT_CONTEXT.get()


@asynccontextmanager
async def audit_telemetry(
    *, run_id: Optional[str], merchant_id: Optional[str],
):
    """Set the audit context for the duration of the `async with`
    block. The previous context is restored on exit (re-entrant safe).

    Used by the worker to scope every probe call inside one audit
    run — see services/audit_run_worker.process_one_audit_run.
    """
    token = _AUDIT_CONTEXT.set(AuditContext(
        audit_run_id=run_id, merchant_id=merchant_id,
    ))
    try:
        yield
    finally:
        _AUDIT_CONTEXT.reset(token)
