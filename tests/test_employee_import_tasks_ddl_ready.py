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
  - a pass in which any statement failed does NOT set the ready flag
  - a later call retries, and retries only what actually failed
  - per-statement tolerance survives: one failing ALTER does not abort the
    rest of the list
  - retries are paced by wall time, and NO statement runs during the
    cooldown — including the CREATE statements, which is why they live
    inside the guarded list rather than around it
  - a clean pass memoizes, so the retry is bounded by success
  - the failure is logged at WARNING and names the column
"""

import asyncio
import logging

import pytest

from db import _ddl_guard

import routes.employee_products as employee_products


LABEL = "_ensure_external_seed_import_tasks_table"

STATEMENTS = employee_products._EXTERNAL_SEED_IMPORT_TASKS_DDL_STATEMENTS

# Written out rather than derived from STATEMENTS. Asserting that a pass ran
# `len(STATEMENTS)` statements is self-referential: it stays green when a
# statement is deleted from the list, which is the failure this whole module
# is about.
EXPECTED_STATEMENT_COUNT = 11

# Every column the backfill adds. Each must also exist in the CREATE TABLE, or
# a fresh database and an upgraded one end up with different schemas.
BACKFILLED_COLUMNS = [
    "created_by_employee_id",
    "created_count",
    "updated_count",
    "errors",
    "seed_ids",
    "stats",
    "error",
    "finished_at",
]


class _FakeClock:
    """Drives DDL_RETRY_COOLDOWN_SECONDS without sleeping."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance_past_cooldown(self):
        self.t += _ddl_guard.DDL_RETRY_COOLDOWN_SECONDS + 1.0


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
def clock(monkeypatch):
    c = _FakeClock()
    monkeypatch.setattr(_ddl_guard, "_monotonic", c)
    return c


@pytest.fixture
def fresh_module(monkeypatch):
    """Start from an un-ready module with no live cooldown, and leave
    neither behind. monkeypatch restores the ready flag even though the
    function writes to it via `global`."""
    monkeypatch.setattr(employee_products, "_EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY", False)
    _ddl_guard.reset_ddl_state(LABEL)
    try:
        yield employee_products
    finally:
        _ddl_guard.reset_ddl_state(LABEL)


def _ensure():
    return asyncio.run(employee_products._ensure_external_seed_import_tasks_table())


def _ready():
    return employee_products._EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY


def test_statement_list_is_complete_and_ordered():
    """The list itself, pinned against absolute expectations.

    The runtime tests below count statements, and a count read back off
    STATEMENTS cannot notice a statement being deleted from STATEMENTS. The
    guard also runs the list in order, and on a fresh database every ALTER
    and every CREATE INDEX names a table the first statement creates.
    """
    assert len(STATEMENTS) == EXPECTED_STATEMENT_COUNT

    assert "CREATE TABLE IF NOT EXISTS employee_external_seed_import_tasks" in STATEMENTS[0], (
        "CREATE TABLE must be the FIRST statement — everything after it needs "
        "the table to exist"
    )
    assert not any("CREATE TABLE" in s for s in STATEMENTS[1:]), (
        "only the first statement may create the table"
    )

    for column in BACKFILLED_COLUMNS:
        assert any(f"ADD COLUMN IF NOT EXISTS {column} " in s for s in STATEMENTS[1:]), (
            f"the backfill for {column} is missing — an upgraded deployment "
            "will not get the column"
        )
        assert f"{column} " in STATEMENTS[0], (
            f"{column} is backfilled but absent from the CREATE TABLE, so a "
            "fresh database and an upgraded one get different schemas"
        )

    indexes = [s for s in STATEMENTS if s.lstrip().startswith("CREATE INDEX")]
    assert len(indexes) == 2, f"expected 2 indexes, found {len(indexes)}"
    assert indexes == STATEMENTS[-2:], (
        "the CREATE INDEX statements must come last — an index on a column "
        "the backfill has not added yet fails on an upgraded deployment"
    )
    for name in ("_status", "_updated_at"):
        assert any(
            f"idx_employee_external_seed_import_tasks{name} " in s for s in indexes
        ), f"index {name} is missing from the list"


def test_failed_backfill_does_not_mark_ready_and_a_later_call_retries(
    fresh_module, clock, monkeypatch
):
    """The core regression: one failing ALTER must leave the module un-ready,
    and a call after the cooldown must genuinely re-run it."""
    assert len(STATEMENTS) >= 2, (
        "needs >=2 statements for this test to tell 'aborted the pass' apart "
        "from 'ran them all'"
    )

    # Fail a statement in the MIDDLE of the list. Failing the last one would
    # make abort-on-first-failure indistinguishable from per-statement
    # tolerance.
    target = "ADD COLUMN IF NOT EXISTS seed_ids"
    assert any(target in s for s in STATEMENTS), "re-point the test at a live column"
    fake = _FakeDatabase(fail_on=target)
    monkeypatch.setattr(employee_products, "database", fake)

    fake.start_pass()
    _ensure()

    assert _ready() is False, (
        "marked itself ready even though a statement failed — every later "
        "caller will short-circuit and never retry the missing column"
    )
    # Per-statement tolerance is preserved: the failure did not abort the
    # rest of the pass. Assert on the statements strictly AFTER the failing
    # one, which is what an abort would drop.
    assert len(fake.executed) == EXPECTED_STATEMENT_COUNT, (
        f"expected all {EXPECTED_STATEMENT_COUNT} statements attempted, "
        f"got {len(fake.executed)}"
    )
    assert "CREATE TABLE" in fake.executed[0], (
        "the pass did not run CREATE TABLE first"
    )
    assert "idx_employee_external_seed_import_tasks_updated_at" in fake.executed[-1], (
        "a failing ALTER aborted the pass before the CREATE INDEX statements; "
        "per-statement tolerance is the documented purpose of this block"
    )

    clock.advance_past_cooldown()
    fake.start_pass()
    _ensure()

    assert fake.executed, "did not retry the DDL once the cooldown expired"


def test_retry_runs_only_the_statement_that_failed(fresh_module, clock, monkeypatch):
    """Postgres takes the table lock BEFORE evaluating IF NOT EXISTS, so
    re-running the already-applied CREATE statements would re-park the table
    on every retry."""
    target = "ADD COLUMN IF NOT EXISTS finished_at"
    fake = _FakeDatabase(fail_on=target)
    monkeypatch.setattr(employee_products, "database", fake)

    _ensure()
    assert _ready() is False

    clock.advance_past_cooldown()
    fake.start_pass()
    _ensure()

    assert len(fake.executed) == 1, (
        f"retry re-ran {len(fake.executed)} statements; only the failed one "
        "should be outstanding"
    )
    assert target in fake.executed[0]


def test_every_failed_statement_is_retried_before_the_module_memoizes(
    fresh_module, clock, monkeypatch
):
    """When MORE THAN ONE statement fails, the retry must re-attempt all of
    them — and the ready flag must not flip until it has.

    The other retry tests fail exactly one statement, so they cannot tell a
    complete outstanding-set apart from a truncated one. A guard that kept
    only the first failure would drop the other seven columns forever and
    then memoize as ready on the next pass: the original bug, one level
    down.
    """
    fake = _FakeDatabase(fail_on="ADD COLUMN IF NOT EXISTS")
    monkeypatch.setattr(employee_products, "database", fake)

    fake.start_pass()
    _ensure()
    assert _ready() is False
    assert len(fake.executed) == EXPECTED_STATEMENT_COUNT

    # Whatever was blocking the ALTERs clears.
    fake.fail_on = None
    clock.advance_past_cooldown()
    fake.start_pass()
    _ensure()

    retried = fake.executed
    assert len(retried) == len(BACKFILLED_COLUMNS), (
        f"retry re-ran {len(retried)} statement(s), expected all "
        f"{len(BACKFILLED_COLUMNS)} that failed"
    )
    for column in BACKFILLED_COLUMNS:
        assert any(f"ADD COLUMN IF NOT EXISTS {column} " in s for s in retried), (
            f"{column} failed in the first pass but was never retried — the "
            "column stays absent while the module reports ready"
        )
    assert _ready() is True, "everything applied, so the module must memoize"


def test_no_statement_runs_during_the_cooldown(fresh_module, clock, monkeypatch):
    """Back-to-back accessor calls with no wall-clock time passing must run
    NO DDL at all.

    This is what forces every statement into the guarded list. The CREATE
    TABLE and CREATE INDEX statements used to sit outside it, so they re-ran
    on every call — and since the guard retries without a cap, one
    permanently-failing ALTER meant taking the table lock three times per
    accessor call, forever.
    """
    fake = _FakeDatabase(fail_on="ADD COLUMN IF NOT EXISTS")
    monkeypatch.setattr(employee_products, "database", fake)

    fake.start_pass()
    _ensure()
    first_pass = len(fake.executed)
    assert first_pass == EXPECTED_STATEMENT_COUNT

    for call in range(10):
        fake.start_pass()
        _ensure()
        assert fake.executed == [], (
            f"call {call + 2} ran {len(fake.executed)} statement(s) inside the "
            "cooldown; a retry must be paced by wall time, not by call count"
        )
        assert _ready() is False, (
            "memoized while statements are still outstanding — that is the "
            "original bug one pass later"
        )


def test_permanent_failure_never_memoizes(fresh_module, clock, monkeypatch):
    """There is deliberately no retry cap: exhausting any bound would mean
    memoizing while the schema is still incomplete."""
    fake = _FakeDatabase(fail_on="ADD COLUMN IF NOT EXISTS")
    monkeypatch.setattr(employee_products, "database", fake)

    for _ in range(10):
        fake.start_pass()
        _ensure()
        assert _ready() is False, (
            "gave up and memoized while the backfill was still failing"
        )
        assert fake.executed, "an un-ready module must keep retrying after the cooldown"
        clock.advance_past_cooldown()


def test_clean_pass_marks_ready_and_stops_re_running(fresh_module, clock, monkeypatch):
    """Memoization still works when nothing fails — otherwise the fix would
    trade silent schema drift for a per-call DDL storm."""
    fake = _FakeDatabase(fail_on=None)
    monkeypatch.setattr(employee_products, "database", fake)

    fake.start_pass()
    _ensure()
    assert _ready() is True
    assert len(fake.executed) == EXPECTED_STATEMENT_COUNT

    clock.advance_past_cooldown()
    fake.start_pass()
    _ensure()
    assert fake.executed == [], "a ready module must not re-run its DDL"


def test_retry_recovers_when_the_transient_failure_clears(fresh_module, clock, monkeypatch):
    """The scenario the fix exists for: a statement cut by the
    DB_COMMAND_TIMEOUT_SECONDS ceiling succeeds once the lock clears."""
    target = "ADD COLUMN IF NOT EXISTS finished_at"
    fake = _FakeDatabase(fail_on=target)
    monkeypatch.setattr(employee_products, "database", fake)

    _ensure()
    assert _ready() is False

    # Lock clears; the column is added.
    fake.fail_on = None
    clock.advance_past_cooldown()
    fake.start_pass()
    _ensure()

    assert _ready() is True
    assert any(target in s for s in fake.executed), (
        "the retry did not re-attempt the statement that had failed"
    )


@pytest.mark.parametrize("column", ["finished_at", "created_by_employee_id"])
def test_failure_is_logged_and_names_the_column(
    fresh_module, clock, monkeypatch, caplog, column
):
    """The original swallow was a bare `pass` — no log at all. Prod (Railway)
    also drops INFO and below, so the record must be at least WARNING and
    must say which column is missing.

    Both columns are checked because the truncation width is what makes this
    work: the eight ALTERs share a 73-character prefix, and the longest
    column name here does not end until character 95. A cut anywhere short of
    that logs several distinct statements as the same line.
    """
    fake = _FakeDatabase(fail_on=f"ADD COLUMN IF NOT EXISTS {column}")
    monkeypatch.setattr(employee_products, "database", fake)

    with caplog.at_level(logging.WARNING):
        _ensure()

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "the swallowed DDL failure produced no WARNING-or-above record"
    assert any(column in r.getMessage() for r in warnings), (
        f"the log does not name {column}, so prod cannot tell which column "
        "is missing"
    )
