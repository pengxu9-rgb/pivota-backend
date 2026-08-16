"""Shared guard for the module-level `ensure_*_table()` DDL backstops.

Several db/* modules memoize their inline DDL behind a module-global
`_DDL_READY` flag so the statements run once per process instead of on
every accessor call. Those modules are also *per-statement tolerant*:
one statement failing must not abort the rest, because the inline DDL
is only a backstop for hermetic SQLite test environments where the
Postgres-flavored statements (partial indexes, `ADD COLUMN IF NOT
EXISTS`, JSONB/ARRAY columns) are not supported at all. Postgres prod
runs the .sql migrations directly.

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
whether the caller may memoize, so a failed pass genuinely retries on
the next call.

Retries are bounded by `DDL_MAX_ATTEMPTS`. A *permanent* failure must
not turn a once-per-process cost into a per-call one: on SQLite every
statement in these lists fails (measured: 41/41 for audit_evidence), and
on Postgres a `CREATE UNIQUE INDEX` over a table that has already
accumulated duplicates — exactly what the original bug produces — fails
on every attempt too. Without a bound, "never memoize after a failure"
would re-run the whole DDL list, including the expensive `CREATE INDEX`
statements, on every accessor call forever. After `DDL_MAX_ATTEMPTS`
failed passes the module memoizes anyway and says so at WARNING level.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Dict, Optional, Sequence

# Full passes that may fail before a module gives up and memoizes
# anyway. A transient failure (lock wait cut by the statement timeout)
# clears well within this; a permanent one costs at most this many
# extra passes instead of one per accessor call forever.
DDL_MAX_ATTEMPTS = 3

# Per-statement failures reported at WARNING before the pass falls back
# to a single summary line. A real prod failure is one or two
# statements; SQLite fails every one of them and would otherwise emit a
# warning per statement per pass.
_MAX_STATEMENT_WARNINGS = 5

# label -> consecutive failed passes.
_attempts: Dict[str, int] = {}


async def apply_ddl_statements(
    statements: Sequence[str],
    *,
    label: str,
    logger: logging.Logger,
    execute: Callable[[str], Awaitable[object]],
) -> bool:
    """Run every statement, tolerating per-statement failure.

    Returns True when the caller may set its `_DDL_READY` flag: either
    every statement succeeded, or the pass has failed
    `DDL_MAX_ATTEMPTS` times and further retries would just re-run the
    DDL on every call.
    """
    failed_stmts = 0
    for stmt in statements:
        try:
            await execute(stmt)
        except Exception as exc:  # noqa: BLE001 — per-statement tolerance
            failed_stmts += 1
            if failed_stmts <= _MAX_STATEMENT_WARNINGS:
                logger.warning(
                    "%s skip stmt: %s | %s",
                    label, str(exc)[:120], stmt[:80],
                )

    if failed_stmts == 0:
        _attempts.pop(label, None)
        return True

    attempts = _attempts.get(label, 0) + 1
    _attempts[label] = attempts

    if attempts >= DDL_MAX_ATTEMPTS:
        logger.warning(
            "%s: %d/%d statement(s) still failing after %d attempts — "
            "memoizing anyway, schema may be incomplete",
            label, failed_stmts, len(statements), attempts,
        )
        return True

    logger.warning(
        "%s: %d/%d statement(s) failed (attempt %d/%d) — not marking ready, "
        "will retry on next call",
        label, failed_stmts, len(statements), attempts, DDL_MAX_ATTEMPTS,
    )
    return False


def reset_ddl_attempts(label: Optional[str] = None) -> None:
    """Clear the retry budget. Test hook — a test that exercises the
    failure path must not leave a consumed budget behind for the next
    test that shares the label."""
    if label is None:
        _attempts.clear()
    else:
        _attempts.pop(label, None)
