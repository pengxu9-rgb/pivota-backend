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
  - the next call actually re-runs the statements
  - a clean pass memoizes, so the retry is bounded by success
  - retries are capped so a *permanent* failure cannot turn a
    once-per-process cost into a per-call one
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
        self.passes = 0

    async def execute(self, stmt):
        stmt_text = str(stmt)
        self.executed.append(stmt_text)
        if self.fail_on is not None and self.fail_on in stmt_text:
            raise self.exc
        return None

    def start_pass(self):
        """Delimit one ensure_*() call so per-pass counts are readable."""
        self.passes += 1
        self.executed = []


@pytest.fixture
def ddl_module(request, monkeypatch):
    """Import a db module fresh so its `_DDL_READY` starts False, and
    point its `database` global at a fake we control.

    The module is reloaded rather than merely reset because
    `_DDL_READY` is a module-global that other tests in the same
    session may already have flipped to True.
    """
    module_name, label = request.param
    module = importlib.import_module(module_name)
    module = importlib.reload(module)
    _ddl_guard.reset_ddl_attempts(label)
    yield module, label
    # Leave no memoized state or consumed retry budget behind.
    _ddl_guard.reset_ddl_attempts(label)
    importlib.reload(module)


def _ensure_fn(module, label):
    return getattr(module, label)


@pytest.mark.parametrize("ddl_module", DDL_MODULES, indirect=True, ids=[m[0] for m in DDL_MODULES])
def test_failed_statement_does_not_mark_ready_and_next_call_retries(ddl_module, monkeypatch):
    """The core regression: one failing statement must leave the module
    un-ready, and the following call must genuinely re-run the DDL."""
    module, label = ddl_module
    ensure = _ensure_fn(module, label)

    statements = module._DDL_STATEMENTS
    assert len(statements) >= 2, (
        f"{module.__name__} needs >=2 _DDL_STATEMENTS for this test to tell "
        "'aborted the pass' apart from 'ran them all'"
    )

    # Fail a statement in the MIDDLE of this module's own list. Failing
    # the last one would make abort-on-first-failure indistinguishable
    # from per-statement tolerance.
    target_idx = len(statements) // 2
    target = statements[target_idx]
    fake = _FakeDatabase(fail_on=target)
    monkeypatch.setattr(module, "database", fake)

    fake.start_pass()
    asyncio.run(ensure())

    assert module._DDL_READY is False, (
        f"{module.__name__} marked itself ready even though a statement failed — "
        "every later caller will short-circuit and never retry"
    )
    # Per-statement tolerance is preserved: the failure did not abort
    # the rest of the pass. Assert on the statements strictly AFTER the
    # failing one, which is what an abort would drop.
    assert fake.executed[-1] == str(statements[-1]), (
        "a failing statement aborted the rest of the pass; per-statement "
        "tolerance is the documented purpose of this loop"
    )
    assert len(fake.executed) == len(statements), (
        f"expected all {len(statements)} statements attempted, "
        f"got {len(fake.executed)}"
    )

    # The second call must do real work, not return early.
    fake.start_pass()
    asyncio.run(ensure())

    assert len(fake.executed) == len(statements), (
        f"{module.__name__} did not retry the DDL on the next call"
    )


@pytest.mark.parametrize("ddl_module", DDL_MODULES, indirect=True, ids=[m[0] for m in DDL_MODULES])
def test_clean_pass_marks_ready_and_stops_re_running(ddl_module, monkeypatch):
    """The memoization still works when nothing fails — otherwise the
    fix would trade a silent-corruption bug for a per-call DDL storm."""
    module, label = ddl_module
    ensure = _ensure_fn(module, label)

    fake = _FakeDatabase(fail_on=None)
    monkeypatch.setattr(module, "database", fake)

    fake.start_pass()
    asyncio.run(ensure())
    assert module._DDL_READY is True
    assert len(fake.executed) == len(module._DDL_STATEMENTS)

    fake.start_pass()
    asyncio.run(ensure())
    assert fake.executed == [], "a ready module must not re-run its DDL"


def test_retry_recovers_when_the_transient_failure_clears(monkeypatch):
    """The scenario the fix exists for: a CREATE INDEX cut by the
    statement timeout succeeds once the lock clears."""
    import db.audit_evidence as ae

    module = importlib.reload(ae)
    _ddl_guard.reset_ddl_attempts("ensure_audit_evidence_tables")

    target = "idx_evidence_items_idempotency"
    assert any(target in s for s in module._DDL_STATEMENTS), (
        "the UNIQUE idempotency index this test names is no longer in "
        "_DDL_STATEMENTS — re-point the test at the current index"
    )

    fake = _FakeDatabase(fail_on=target)
    monkeypatch.setattr(module, "database", fake)

    asyncio.run(module.ensure_audit_evidence_tables())
    assert module._DDL_READY is False

    # Lock clears; the index now builds.
    fake.fail_on = None
    fake.start_pass()
    asyncio.run(module.ensure_audit_evidence_tables())

    assert module._DDL_READY is True
    assert any(target in s for s in fake.executed), (
        "the retry did not re-attempt the index that had failed"
    )

    _ddl_guard.reset_ddl_attempts("ensure_audit_evidence_tables")
    importlib.reload(ae)


def test_permanent_failure_stops_retrying_after_the_cap(monkeypatch):
    """A permanently-failing statement (SQLite syntax, or a UNIQUE index
    over already-duplicated rows) must not re-run the whole DDL list on
    every accessor call forever."""
    import db.audit_evidence as ae

    module = importlib.reload(ae)
    _ddl_guard.reset_ddl_attempts("ensure_audit_evidence_tables")

    fake = _FakeDatabase(fail_on="CREATE")
    monkeypatch.setattr(module, "database", fake)

    for attempt in range(1, _ddl_guard.DDL_MAX_ATTEMPTS):
        fake.start_pass()
        asyncio.run(module.ensure_audit_evidence_tables())
        assert module._DDL_READY is False, (
            f"gave up after {attempt} attempt(s); the budget is "
            f"{_ddl_guard.DDL_MAX_ATTEMPTS}"
        )
        assert fake.executed, "an un-ready module must keep retrying"

    # The final attempt exhausts the budget and memoizes.
    fake.start_pass()
    asyncio.run(module.ensure_audit_evidence_tables())
    assert module._DDL_READY is True

    fake.start_pass()
    asyncio.run(module.ensure_audit_evidence_tables())
    assert fake.executed == [], (
        "past the retry cap the module must stop re-running its DDL"
    )

    _ddl_guard.reset_ddl_attempts("ensure_audit_evidence_tables")
    importlib.reload(ae)


def test_failure_is_logged_above_debug(caplog, monkeypatch):
    """Prod (Railway) drops INFO and below, so a swallowed DDL failure
    logged at debug is invisible. It must be at least WARNING."""
    import db.audit_evidence as ae

    module = importlib.reload(ae)
    _ddl_guard.reset_ddl_attempts("ensure_audit_evidence_tables")

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

    _ddl_guard.reset_ddl_attempts("ensure_audit_evidence_tables")
    importlib.reload(ae)
