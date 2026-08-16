"""A DDL pass that failed must not memoize itself as ready.

The `ensure_*_table()` backstops in db/* are per-statement tolerant: one
statement failing must not abort the rest, because on SQLite every
Postgres-flavored statement fails. They previously combined that with an
unconditional `_DDL_READY = True` after the loop, so a statement that
failed for a *transient* reason — a `builtins.TimeoutError` from the
`DB_COMMAND_TIMEOUT_SECONDS=600` ceiling cutting a `CREATE INDEX` that
was blocked on a lock — still marked the module ready, and every later
caller short-circuited on `_DDL_READY` for the process lifetime. When
the cut statement is one of the UNIQUE idempotency indexes, the
constraint stays silently absent while the write accessors keep
inserting.

These tests pin the contract the fix establishes:
  - a pass in which any statement failed does NOT set `_DDL_READY`
  - a later call retries, and retries only what actually failed
  - retries are paced by WALL TIME, so the realistic accessor call
    pattern (back-to-back inserts) cannot burn the retry budget before
    the contention has had a chance to clear
  - a clean pass memoizes, so the retry is bounded by success
"""

import asyncio
import importlib
import logging

import pytest

from db import _ddl_guard


# The five modules that shared the unconditional-ready shape, with the
# label each one reports to the guard. Driving all five keeps a future
# module from silently regressing to the old inline loop.
DDL_MODULES = [
    ("db.audit_evidence", "ensure_audit_evidence_tables"),
    ("db.citation_read_log", "ensure_citation_read_log_table"),
    ("db.executor_runs", "ensure_executor_runs_table"),
    ("db.llm_probe_runs", "ensure_llm_probe_runs_table"),
    ("db.merchant_audit_runs", "ensure_merchant_audit_runs_table"),
]


class _FakeClock:
    """Drives DDL_RETRY_COOLDOWN_SECONDS without sleeping."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance_past_cooldown(self):
        self.t += _ddl_guard.DDL_RETRY_COOLDOWN_SECONDS + 1.0


@pytest.fixture
def clock(monkeypatch):
    c = _FakeClock()
    monkeypatch.setattr(_ddl_guard, "_monotonic", c)
    return c


class _FakeDatabase:
    """Records every statement and fails the chosen one.

    `fail_on` is matched as a substring so a test can name the exact
    index it cares about (e.g. the UNIQUE idempotency index) without
    reproducing the whole statement text.
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
def ddl_module(request):
    """Import a db module fresh so its `_DDL_READY` starts False.

    The module is reloaded rather than merely reset because
    `_DDL_READY` is a module-global that other tests in the same
    session may already have flipped to True.
    """
    module_name, label = request.param
    module = importlib.reload(importlib.import_module(module_name))
    _ddl_guard.reset_ddl_state(label)
    try:
        yield module, label
    finally:
        # Leave no memoized state or live cooldown behind, even if the
        # test body failed part-way.
        _ddl_guard.reset_ddl_state(label)
        importlib.reload(module)


@pytest.fixture
def audit_evidence(clock):
    """db.audit_evidence, reloaded, with the cooldown clock faked."""
    import db.audit_evidence as ae

    module = importlib.reload(ae)
    _ddl_guard.reset_ddl_state("ensure_audit_evidence_tables")
    try:
        yield module
    finally:
        _ddl_guard.reset_ddl_state("ensure_audit_evidence_tables")
        importlib.reload(ae)


@pytest.mark.parametrize(
    "ddl_module", DDL_MODULES, indirect=True, ids=[m[0] for m in DDL_MODULES]
)
def test_failed_statement_does_not_mark_ready_and_a_later_call_retries(
    ddl_module, clock, monkeypatch
):
    """The core regression: one failing statement must leave the module
    un-ready, and a later call must genuinely re-run the DDL."""
    module, label = ddl_module
    ensure = getattr(module, label)

    statements = module._DDL_STATEMENTS
    assert len(statements) >= 2, (
        f"{module.__name__} needs >=2 _DDL_STATEMENTS for this test to tell "
        "'aborted the pass' apart from 'ran them all'"
    )

    # Fail a statement in the MIDDLE of this module's own list. Failing
    # the last one would make abort-on-first-failure indistinguishable
    # from per-statement tolerance.
    target = statements[len(statements) // 2]
    fake = _FakeDatabase(fail_on=target)
    monkeypatch.setattr(module, "database", fake)

    fake.start_pass()
    asyncio.run(ensure())

    assert module._DDL_READY is False, (
        f"{module.__name__} marked itself ready even though a statement failed — "
        "every later caller will short-circuit and never retry"
    )
    # Per-statement tolerance is preserved: the failure did not abort
    # the rest of the pass. Assert on the statement strictly AFTER the
    # failing one, which is what an abort would drop.
    assert fake.executed[-1] == str(statements[-1]), (
        "a failing statement aborted the rest of the pass; per-statement "
        "tolerance is the documented purpose of this loop"
    )
    assert len(fake.executed) == len(statements)

    # Once the cooldown expires the module must do real work again.
    clock.advance_past_cooldown()
    fake.start_pass()
    asyncio.run(ensure())

    assert fake.executed, f"{module.__name__} never retried the failed statement"
    assert module._DDL_READY is False


@pytest.mark.parametrize(
    "ddl_module", DDL_MODULES, indirect=True, ids=[m[0] for m in DDL_MODULES]
)
def test_clean_pass_marks_ready_and_stops_re_running(ddl_module, clock, monkeypatch):
    """The memoization still works when nothing fails — otherwise the
    fix would trade a silent-corruption bug for a per-call DDL storm."""
    module, label = ddl_module
    ensure = getattr(module, label)

    fake = _FakeDatabase(fail_on=None)
    monkeypatch.setattr(module, "database", fake)

    fake.start_pass()
    asyncio.run(ensure())
    assert module._DDL_READY is True
    assert len(fake.executed) == len(module._DDL_STATEMENTS)

    fake.start_pass()
    asyncio.run(ensure())
    assert fake.executed == [], "a ready module must not re-run its DDL"


def test_back_to_back_accessor_calls_do_not_burn_the_retry_budget(
    audit_evidence, clock, monkeypatch
):
    """The defect that a call-counted retry budget hides.

    Write accessors call `ensure_*` on EVERY invocation and callers
    insert in back-to-back loops, so a budget of N calls is spent in
    microseconds — long before a lock clears. A counted budget would be
    exhausted here and the module would memoize with the index still
    missing, which is the original bug one pass later.
    """
    module = audit_evidence
    target = "idx_evidence_items_idempotency"
    assert any(target in s for s in module._DDL_STATEMENTS), (
        "the UNIQUE idempotency index this test names is no longer in "
        "_DDL_STATEMENTS — re-point the test at the current index"
    )

    fake = _FakeDatabase(fail_on=target)
    monkeypatch.setattr(module, "database", fake)

    # Ten inserts in a tight loop, no wall-clock time passing — the
    # shape of persist_canonical_evidence.
    for _ in range(10):
        asyncio.run(module.insert_evidence_item(
            audit_run_id="run-1", evidence_type="custom", payload={},
        ))

    assert module._DDL_READY is False, (
        "the module memoized during a burst of accessor calls while the "
        "index was still missing — a call-counted retry budget reproduces "
        "the original bug one pass later"
    )

    # Only ONE DDL pass should have run for the whole burst; the rest
    # are inside the cooldown and must not re-run the statements.
    ddl_passes = fake.executed.count(str(module._DDL_STATEMENTS[0]))
    assert ddl_passes == 1, (
        f"{ddl_passes} DDL passes ran during a 10-call burst; callers "
        "inside the cooldown must not each re-run the statement list"
    )

    # The lock clears and enough wall time passes: the retry must land
    # and the module must finally go ready.
    fake.fail_on = None
    clock.advance_past_cooldown()
    fake.start_pass()
    asyncio.run(module.insert_evidence_item(
        audit_run_id="run-1", evidence_type="custom", payload={},
    ))

    assert module._DDL_READY is True, (
        "the module never recovered after the transient failure cleared"
    )
    assert any(target in s for s in fake.executed), (
        "the retry did not re-attempt the index that had failed"
    )


def test_retry_runs_only_the_statements_that_failed(audit_evidence, clock, monkeypatch):
    """Postgres takes the table lock before evaluating IF NOT EXISTS, so
    re-running already-applied statements re-parks the table. A retry
    must carry only the outstanding subset."""
    module = audit_evidence
    target = "idx_evidence_items_idempotency"

    fake = _FakeDatabase(fail_on=target)
    monkeypatch.setattr(module, "database", fake)

    asyncio.run(module.ensure_audit_evidence_tables())
    assert len(fake.executed) == len(module._DDL_STATEMENTS)

    clock.advance_past_cooldown()
    fake.start_pass()
    asyncio.run(module.ensure_audit_evidence_tables())

    assert len(fake.executed) == 1, (
        f"retry re-ran {len(fake.executed)} statements; only the failed one "
        "was outstanding"
    )
    assert target in fake.executed[0]


def test_cooldown_suppresses_the_retry_until_it_expires(audit_evidence, clock, monkeypatch):
    """Waiters arriving during the cooldown must not each run a pass —
    that is the #1684 shape, three callers serialized inside _DDL_LOCK
    each paying the full statement timeout."""
    module = audit_evidence

    fake = _FakeDatabase(fail_on="idx_evidence_items_idempotency")
    monkeypatch.setattr(module, "database", fake)

    asyncio.run(module.ensure_audit_evidence_tables())

    for _ in range(3):
        fake.start_pass()
        asyncio.run(module.ensure_audit_evidence_tables())
        assert fake.executed == [], (
            "a caller inside the cooldown ran the DDL anyway"
        )

    clock.advance_past_cooldown()
    fake.start_pass()
    asyncio.run(module.ensure_audit_evidence_tables())
    assert fake.executed, "the retry never ran after the cooldown expired"


def test_cancellation_mid_pass_still_starts_the_cooldown(audit_evidence, clock, monkeypatch):
    """A scheduler run deadline cancels the task mid-pass. CancelledError
    is a BaseException, so it bypasses the per-statement `except
    Exception`; the cooldown must still engage or the cancel/retry loop
    runs as fast as callers arrive."""
    module = audit_evidence

    class _CancellingDatabase(_FakeDatabase):
        async def execute(self, stmt):
            self.executed.append(str(stmt))
            if len(self.executed) == 3:
                raise asyncio.CancelledError()
            return None

    fake = _CancellingDatabase()
    monkeypatch.setattr(module, "database", fake)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(module.ensure_audit_evidence_tables())

    assert module._DDL_READY is False

    # The very next caller must be held off by the cooldown rather than
    # immediately re-running the whole pass.
    fake.start_pass()
    asyncio.run(module.ensure_audit_evidence_tables())
    assert fake.executed == [], (
        "a cancelled pass left no cooldown, so the next call re-ran the "
        "whole statement list immediately"
    )


def test_failure_is_logged_above_debug(audit_evidence, clock, caplog, monkeypatch):
    """Prod (Railway) drops INFO and below, so a swallowed DDL failure
    logged at debug is invisible. It must be at least WARNING."""
    module = audit_evidence

    fake = _FakeDatabase(fail_on="idx_evidence_items_idempotency")
    monkeypatch.setattr(module, "database", fake)

    with caplog.at_level(logging.WARNING):
        asyncio.run(module.ensure_audit_evidence_tables())

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "the swallowed DDL failure produced no WARNING-or-above record"
    assert any("idx_evidence_items_idempotency" in r.getMessage() for r in warnings), (
        "the log does not name the statement that failed, so prod cannot "
        "tell which object is missing"
    )
