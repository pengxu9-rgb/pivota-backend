"""`_ensure_external_seed_import_tasks_table()` must not memoize a failed pass.

Same shape as the db/* backstops covered by
tests/test_ddl_ready_retries_after_failure.py, one notch milder: the
swallowed statements are `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so a
failure leaves a *column* absent rather than a UNIQUE constraint. The run of
ALTERs sat inside a nested `try: ... except Exception: pass` — a bare pass,
no log at all — and execution then fell through to
`_EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY = True`. Every later caller
short-circuited on that flag for the rest of the process lifetime, so the
missing column was never retried and nothing said so in prod.

The contract pinned here:
  - a pass in which any backfill statement failed does NOT set the ready flag
  - the next call genuinely re-runs the DDL
  - per-statement tolerance survives: one failing ALTER does not abort the
    rest of the list, nor the CREATE INDEX statements after it
  - a clean pass memoizes (otherwise the fix trades silent drift for a
    per-call DDL storm), and retries are capped for a permanent failure
  - the failure is logged at WARNING and names the statement
"""

import asyncio
import logging

import pytest

from db import _ddl_guard

import routes.employee_products as employee_products


LABEL = "_ensure_external_seed_import_tasks_table"

# CREATE TABLE + the backfill ALTERs + the two CREATE INDEX statements.
_TOTAL_STATEMENTS = 1 + len(employee_products._EXTERNAL_SEED_IMPORT_TASKS_BACKFILL_STATEMENTS) + 2


class _FakeDatabase:
    """Records every statement and fails the chosen one.

    `fail_on` is matched as a substring so a test can name the exact column
    it cares about without reproducing the whole statement text.
    """

    def __init__(self, fail_on=None, exc=None):
        self.fail_on = fail_on
        self.exc = exc or TimeoutError("canceling statement due to statement timeout")
        self.executed = []

    async def execute(self, stmt):
        stmt_text = str(stmt)
        self.executed.append(stmt_text)
        if self.fail_on is not None and self.fail_on in stmt_text:
            raise self.exc
        return None

    def start_pass(self):
        """Delimit one ensure_*() call so per-pass counts are readable."""
        self.executed = []


@pytest.fixture
def fresh_module(monkeypatch):
    """Start from an un-ready module with an unconsumed retry budget, and
    leave neither behind. monkeypatch restores the ready flag even though
    the function writes to it via `global`."""
    monkeypatch.setattr(employee_products, "_EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY", False)
    _ddl_guard.reset_ddl_attempts(LABEL)
    yield employee_products
    _ddl_guard.reset_ddl_attempts(LABEL)


def _ensure():
    return asyncio.run(employee_products._ensure_external_seed_import_tasks_table())


def test_failed_backfill_does_not_mark_ready_and_next_call_retries(fresh_module, monkeypatch):
    """The core regression: one failing ALTER must leave the module un-ready,
    and the following call must genuinely re-run the DDL."""
    statements = employee_products._EXTERNAL_SEED_IMPORT_TASKS_BACKFILL_STATEMENTS
    assert len(statements) >= 2, (
        "needs >=2 backfill statements for this test to tell 'aborted the "
        "pass' apart from 'ran them all'"
    )

    # Fail a statement in the MIDDLE of the list. Failing the last one would
    # make abort-on-first-failure indistinguishable from per-statement
    # tolerance.
    target = statements[len(statements) // 2]
    fake = _FakeDatabase(fail_on=target)
    monkeypatch.setattr(employee_products, "database", fake)

    fake.start_pass()
    _ensure()

    assert employee_products._EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY is False, (
        "marked itself ready even though a backfill statement failed — every "
        "later caller will short-circuit and never retry the missing column"
    )
    # Per-statement tolerance is preserved: the failure aborted neither the
    # rest of the backfill nor the index statements that follow it.
    for stmt in statements:
        assert stmt in fake.executed, f"backfill statement was skipped: {stmt[:80]}"
    assert "idx_employee_external_seed_import_tasks_updated_at" in fake.executed[-1], (
        "a failing ALTER aborted the pass before the CREATE INDEX statements; "
        "per-statement tolerance is the documented purpose of this block"
    )
    assert len(fake.executed) == _TOTAL_STATEMENTS

    # The second call must do real work, not return early.
    fake.start_pass()
    _ensure()

    assert len(fake.executed) == _TOTAL_STATEMENTS, (
        "did not retry the DDL on the next call"
    )


def test_clean_pass_marks_ready_and_stops_re_running(fresh_module, monkeypatch):
    """Memoization still works when nothing fails — otherwise the fix would
    trade silent schema drift for a per-call DDL storm."""
    fake = _FakeDatabase(fail_on=None)
    monkeypatch.setattr(employee_products, "database", fake)

    fake.start_pass()
    _ensure()
    assert employee_products._EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY is True
    assert len(fake.executed) == _TOTAL_STATEMENTS

    fake.start_pass()
    _ensure()
    assert fake.executed == [], "a ready module must not re-run its DDL"


def test_retry_recovers_when_the_transient_failure_clears(fresh_module, monkeypatch):
    """The scenario the fix exists for: a statement cut by the
    DB_COMMAND_TIMEOUT_SECONDS ceiling succeeds on the retry."""
    # Match the ALTER, not the CREATE TABLE that also mentions the column
    # (that statement is unguarded and its failure propagates by design).
    target = "ADD COLUMN IF NOT EXISTS finished_at"
    assert any(
        target in s for s in employee_products._EXTERNAL_SEED_IMPORT_TASKS_BACKFILL_STATEMENTS
    ), "the column this test names is no longer backfilled — re-point the test"

    fake = _FakeDatabase(fail_on=target)
    monkeypatch.setattr(employee_products, "database", fake)

    _ensure()
    assert employee_products._EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY is False

    # Lock clears; the column is added.
    fake.fail_on = None
    fake.start_pass()
    _ensure()

    assert employee_products._EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY is True
    assert any(target in s for s in fake.executed), (
        "the retry did not re-attempt the statement that had failed"
    )


def test_permanent_failure_stops_retrying_after_the_cap(fresh_module, monkeypatch):
    """A permanently-failing statement (`ADD COLUMN IF NOT EXISTS` is a
    syntax error on SQLite) must not re-run the whole DDL list on every
    accessor call forever."""
    fake = _FakeDatabase(fail_on="ADD COLUMN IF NOT EXISTS")
    monkeypatch.setattr(employee_products, "database", fake)

    for attempt in range(1, _ddl_guard.DDL_MAX_ATTEMPTS):
        fake.start_pass()
        _ensure()
        assert employee_products._EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY is False, (
            f"gave up after {attempt} attempt(s); the budget is "
            f"{_ddl_guard.DDL_MAX_ATTEMPTS}"
        )
        assert fake.executed, "an un-ready module must keep retrying"

    # The final attempt exhausts the budget and memoizes.
    fake.start_pass()
    _ensure()
    assert employee_products._EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY is True

    fake.start_pass()
    _ensure()
    assert fake.executed == [], (
        "past the retry cap the module must stop re-running its DDL"
    )


def test_failure_is_logged_and_names_the_statement(fresh_module, monkeypatch, caplog):
    """The original swallow was a bare `pass` — no log at all. Prod (Railway)
    also drops INFO and below, so the record must be at least WARNING and
    must say which column is missing."""
    fake = _FakeDatabase(fail_on="ADD COLUMN IF NOT EXISTS finished_at")
    monkeypatch.setattr(employee_products, "database", fake)

    with caplog.at_level(logging.WARNING):
        _ensure()

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "the swallowed DDL failure produced no WARNING-or-above record"
    # The column name is what identifies the statement, and it sits at the
    # end: all eight ALTERs share a 73-character prefix, so a short truncation
    # would log them all as the same line.
    assert any("finished_at" in r.getMessage() for r in warnings), (
        "the log does not name the column that failed, so prod cannot tell "
        "which column is missing"
    )
