"""Shared guard for the module-level `ensure_*_table()` DDL backstops.

Several db/* modules memoize their inline DDL behind a module-global
`_DDL_READY` flag so the statements run once per process instead of on
every accessor call. Those modules are also *per-statement tolerant*:
one statement failing must not abort the rest, because the inline DDL
is only a backstop for hermetic SQLite test environments where the
Postgres-flavored statements (partial indexes, `ADD COLUMN IF NOT
EXISTS`, `DEFAULT NOW()`) are not supported at all — measured, all 41
statements fail on SQLite for audit_evidence. Postgres prod runs the
.sql migrations directly.

The bug this helper exists to prevent: those two properties were
combined by swallowing each statement's exception inside the loop and
then setting `_DDL_READY = True` *unconditionally* afterwards. A
statement that failed for a transient reason — most importantly a
`builtins.TimeoutError` from the `DB_COMMAND_TIMEOUT_SECONDS=600`
statement ceiling that went live on prod on 2026-08-17, which cuts a
`CREATE INDEX` blocked on a lock rather than waiting — still marked the
module ready. Every later caller then short-circuited on `_DDL_READY`
for the rest of the process lifetime, so the missing object was never
retried. When the cut statement is one of the UNIQUE idempotency
indexes, the constraint is silently absent while the write accessors
keep inserting. The swallow was logged at `logger.debug`, which prod
does not record, so the whole sequence was invisible.

`apply_ddl_statements()` keeps the per-statement tolerance but returns
whether the caller may memoize, so a failed pass genuinely retries.

Two properties matter as much as that return value, because the naive
version of this fix is worse than the bug:

*Retries are paced by wall time, not by call count.* Every write
accessor calls its `ensure_*` on every invocation, and callers insert
in back-to-back loops, so a budget of N *calls* is spent in
microseconds — long before a lock could clear — and then the module
memoizes with the object still missing, reproducing the original bug
one pass later. `DDL_RETRY_COOLDOWN_SECONDS` paces retries instead, so
a retry lands when the contention plausibly has cleared. It also keeps
callers queued behind the caller's `_DDL_LOCK` from each running their
own full pass: once one pass has failed, the rest return immediately
until the cooldown expires. Without that, three waiters would each pay
the full 600s statement timeout, serialized inside the lock — the
#1684 shape, where serializing a recovery path turns a fault into an
unbounded latency queue.

*A retry re-runs only the statements that failed.* These lists are
dominated by `CREATE INDEX` and `ALTER TABLE ... ADD COLUMN`, and
Postgres takes the table lock *before* evaluating `IF NOT EXISTS` —
which is exactly why one of these statements accrued 665.5s of lock
wait on prod with a near-zero execution time. Re-running all 41 would
re-block on every already-applied statement and re-park the table for
every other session. Re-running just the failed subset drops a typical
retry to one statement.

There is deliberately no cap on the number of retries. A cap is what
re-introduces the original bug: whatever bound is chosen, exhausting it
means memoizing while the schema is still incomplete. The cooldown
already bounds the *rate* — one small pass per cooldown per process —
which was the only thing a cap was protecting against. The nine
sibling `ensure_*` helpers in db/ that abort-and-return have retried
unboundedly for as long as they have existed without incident.
"""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable, Dict, List, Optional, Sequence

# Minimum wall-clock gap between two DDL passes for the same label.
# Sized against what these passes are waiting on: a lock held long
# enough to cut a statement at the 600s ceiling does not clear in
# seconds. Also the rate limit on the WARNING below.
DDL_RETRY_COOLDOWN_SECONDS = 300.0

# Per-statement failures reported at WARNING before the pass falls back
# to a single summary line. A real prod failure is one or two
# statements; SQLite fails every one of them and would otherwise emit a
# warning per statement per pass.
_MAX_STATEMENT_WARNINGS = 5


class _LabelState:
    """The statements still to apply for one label, and when the pass
    that left them outstanding finished."""

    __slots__ = ("pending", "last_pass_at")

    def __init__(self, pending: List[str], last_pass_at: float) -> None:
        self.pending = pending
        self.last_pass_at = last_pass_at


_state: Dict[str, _LabelState] = {}


def _monotonic() -> float:
    # Indirected so tests can drive the cooldown with a fake clock.
    return time.monotonic()


async def apply_ddl_statements(
    statements: Sequence[str],
    *,
    label: str,
    logger: logging.Logger,
    execute: Callable[[str], Awaitable[object]],
) -> bool:
    """Run the outstanding statements, tolerating per-statement failure.

    Returns True only when every statement has been applied — i.e. when
    the caller may set its `_DDL_READY` flag. A pass that left anything
    outstanding returns False, so the caller retries on a later call.

    Callers arriving within `DDL_RETRY_COOLDOWN_SECONDS` of a failed
    pass return False without running any DDL.
    """
    state = _state.get(label)
    if state is None:
        to_run: List[str] = list(statements)
    else:
        if _monotonic() - state.last_pass_at < DDL_RETRY_COOLDOWN_SECONDS:
            # Another caller just paid for a failed pass. Re-running now
            # would amplify whatever is blocking it and, if that caller
            # is still inside the statement timeout, stack a second full
            # stall behind it.
            return False
        to_run = state.pending

    failed: List[str] = []
    completed = False
    try:
        for stmt in to_run:
            try:
                await execute(stmt)
            except Exception as exc:  # noqa: BLE001 — per-statement tolerance
                failed.append(stmt)
                if len(failed) <= _MAX_STATEMENT_WARNINGS:
                    logger.warning(
                        "%s skip stmt: %s | %s",
                        label, str(exc)[:120], stmt[:80],
                    )
        completed = True
    finally:
        if not completed:
            # A BaseException — a scheduler run deadline cancelling this
            # task mid-pass is the realistic one — must still start the
            # cooldown, or the next call re-runs the whole pass and the
            # cancel/retry loop runs as fast as callers arrive.
            _state[label] = _LabelState(list(to_run), _monotonic())

    if not failed:
        _state.pop(label, None)
        return True

    _state[label] = _LabelState(failed, _monotonic())
    logger.warning(
        "%s: %d/%d statement(s) failed — not marking ready, retrying no "
        "sooner than %.0fs from now",
        label, len(failed), len(to_run), DDL_RETRY_COOLDOWN_SECONDS,
    )
    return False


def reset_ddl_state(label: Optional[str] = None) -> None:
    """Drop the outstanding-statement set and cooldown. Test hook — a
    test that exercises the failure path must not leave a cooldown
    behind for the next test that shares the label."""
    if label is None:
        _state.clear()
    else:
        _state.pop(label, None)
