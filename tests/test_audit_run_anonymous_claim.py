"""C3: an audit run can exist before anyone has registered, and conversion
claims the SAME row.

The marketing funnel starts a run for a visitor with no account. Until this
change `merchant_audit_runs.merchant_id` was NOT NULL, so no such row could
exist and the public page could only ever show a protocol teaser.

WHY THE DECLARATION TESTS COME FIRST. The behavioural tests below drive the
claim against a hand-built SQLite table, and a hand-built table proves nothing
about production on its own — it is exactly the fixture-DDL trap. So the first
group pins the three places the real schema lives (the SQLAlchemy model, the
inline DDL backstop, and migration 210) and asserts they agree. `create_all`
runs BEFORE migrations in this codebase, so for a fresh database the MODEL is
what builds production: a migration alone would not be enough, and neither
would a model change alone for a database that already has the table.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import db.merchant_audit_runs as mar

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "db" / "migrations" / "210_audit_runs_anonymous_claim.sql"
)


# ---- 1. the three schema homes must agree ----------------------------------

def test_the_model_declares_merchant_id_nullable():
    """The model is what create_all builds production from."""
    col = mar.merchant_audit_runs.c.merchant_id
    assert col.nullable is True, (
        "merchant_id must be nullable or an anonymous run cannot be inserted"
    )


def test_the_model_carries_the_claim_timestamp():
    cols = mar.merchant_audit_runs.c
    assert "merchant_claimed_at" in cols
    assert cols.merchant_claimed_at.nullable is True
    # NOT `claimed_at`: claimed_by_worker / claimed_until on this same table
    # are the worker lease. A bare claimed_at would read as part of it.
    assert "claimed_at" not in cols


def test_the_inline_ddl_backstop_agrees_with_the_model():
    """The CREATE TABLE builds fresh databases; the ALTER fixes existing ones.
    Both are needed — CREATE TABLE IF NOT EXISTS silently does nothing when
    the table is already there, which is every real environment."""
    ddl = "\n".join(mar._DDL_STATEMENTS)
    create = next(s for s in mar._DDL_STATEMENTS if "CREATE TABLE" in s)
    assert re.search(r"merchant_id\s+TEXT NULL", create), (
        "the fresh-database CREATE TABLE still declares merchant_id NOT NULL"
    )
    assert "ALTER COLUMN merchant_id DROP NOT NULL" in ddl, (
        "an existing table keeps its NOT NULL unless the backstop drops it"
    )
    assert "ADD COLUMN IF NOT EXISTS merchant_claimed_at" in ddl


def test_the_runtime_self_heal_carries_BOTH_halves():
    """Deploys skip db/migrations/, so schema_guard is what actually reaches
    production. test_schema_guard_migration_coverage only checks ADD COLUMN —
    it would stay green with the DROP NOT NULL missing, and every anonymous
    insert would then fail against a column that exists under a constraint
    that was never lifted."""
    guard = (
        Path(__file__).resolve().parents[1] / "db" / "schema_guard.py"
    ).read_text()
    assert "ADD COLUMN IF NOT EXISTS merchant_claimed_at" in guard
    assert re.search(
        r"ALTER TABLE IF EXISTS merchant_audit_runs\s+"
        r"ALTER COLUMN merchant_id DROP NOT NULL",
        guard,
    ), "schema_guard must lift the NOT NULL, not only add the column"


def test_the_migration_agrees_with_the_model():
    sql = _MIGRATION.read_text()
    assert "ALTER COLUMN merchant_id DROP NOT NULL" in sql
    assert "ADD COLUMN IF NOT EXISTS merchant_claimed_at" in sql


# ---- 2. the claim itself ----------------------------------------------------

@pytest.fixture
async def sqlite_db(monkeypatch, tmp_path):
    """SQLite-shaped table. Its merchant_id nullability is NOT hand-asserted
    here — the declaration tests above own that claim; this fixture only makes
    the UPDATE runnable.

    File-backed, not `:memory:`: the `databases` driver opens a connection per
    operation and every in-memory connection gets its OWN empty database, so a
    table created in the fixture is invisible to the code under test. That is
    what makes the neighbouring round-trip test in test_merchant_audit_runs.py
    "fragile" enough to be skipped.
    """
    from databases import Database
    db = Database(f"sqlite:///{tmp_path}/runs.db")
    await db.connect()
    await db.execute(
        """
        CREATE TABLE merchant_audit_runs (
          run_id TEXT PRIMARY KEY,
          merchant_id TEXT,
          merchant_claimed_at TEXT,
          requested_at TEXT NOT NULL,
          status TEXT NOT NULL,
          product_keys TEXT NOT NULL,
          subject_type TEXT
        )
        """
    )
    monkeypatch.setattr("db.merchant_audit_runs.database", db)
    mar._DDL_READY = True
    yield db
    await db.disconnect()
    mar._DDL_READY = False


async def _insert(db, run_id, merchant_id):
    await db.execute(
        "INSERT INTO merchant_audit_runs "
        "(run_id, merchant_id, requested_at, status, product_keys) "
        "VALUES (:r, :m, '2026-09-02T00:00:00Z', 'running', '')",
        {"r": run_id, "m": merchant_id},
    )


async def _owner(db, run_id):
    row = await db.fetch_one(
        "SELECT merchant_id, merchant_claimed_at FROM merchant_audit_runs "
        "WHERE run_id = :r",
        {"r": run_id},
    )
    return dict(row) if row else {}


async def test_an_unclaimed_run_is_claimed_once(sqlite_db):
    await _insert(sqlite_db, "run-a", None)
    assert await mar.claim_audit_run_for_merchant(
        run_id="run-a", merchant_id="m-1"
    ) is True
    row = await _owner(sqlite_db, "run-a")
    assert row["merchant_id"] == "m-1"
    assert row["merchant_claimed_at"] is not None


async def test_a_second_claim_of_the_same_run_does_not_win(sqlite_db):
    """The guard is in the WHERE clause, so a concurrent second claimer is
    told it did not claim rather than silently overwriting the first."""
    await _insert(sqlite_db, "run-b", None)
    assert await mar.claim_audit_run_for_merchant(
        run_id="run-b", merchant_id="m-1"
    ) is True
    assert await mar.claim_audit_run_for_merchant(
        run_id="run-b", merchant_id="m-2"
    ) is False
    assert (await _owner(sqlite_db, "run-b"))["merchant_id"] == "m-1"


async def test_a_claim_never_takes_over_someone_elses_run(sqlite_db):
    """`merchant_id IS NULL` is what makes this a claim and not a takeover."""
    await _insert(sqlite_db, "run-c", "victim")
    assert await mar.claim_audit_run_for_merchant(
        run_id="run-c", merchant_id="attacker"
    ) is False
    assert (await _owner(sqlite_db, "run-c"))["merchant_id"] == "victim"


async def test_the_claim_never_duplicates_the_run(sqlite_db):
    await _insert(sqlite_db, "run-d", None)
    await mar.claim_audit_run_for_merchant(run_id="run-d", merchant_id="m-1")
    row = await sqlite_db.fetch_one(
        "SELECT COUNT(*) AS n FROM merchant_audit_runs"
    )
    assert dict(row)["n"] == 1


@pytest.mark.parametrize("bad", ["", "   ", None])
async def test_a_falsy_claimant_claims_nothing(sqlite_db, bad):
    """An empty claimant must not be able to 'claim' a run into a blank owner,
    which would leave the row looking claimed to a `merchant_id IS NULL` guard
    while belonging to nobody."""
    await _insert(sqlite_db, "run-e", None)
    assert await mar.claim_audit_run_for_merchant(
        run_id="run-e", merchant_id=bad
    ) is False
    assert (await _owner(sqlite_db, "run-e"))["merchant_id"] is None


async def test_an_empty_owner_is_written_as_null_not_blank(monkeypatch):
    """record_audit_run_started normalizes "" to NULL, so a falsy caller id
    cannot produce a row that is unclaimable (merchant_id IS NOT NULL) AND
    unowned. Asserted at the insert boundary: the ARRAY-typed product_keys
    column cannot round-trip on SQLite."""
    seen = {}

    class _Capture:
        async def execute(self, query):
            seen["values"] = dict(query.compile().params)
            return None

    monkeypatch.setattr(mar, "database", _Capture())
    monkeypatch.setattr(mar, "_DDL_READY", True)
    assert await mar.record_audit_run_started(merchant_id="  ", product_keys=[])
    assert seen["values"]["merchant_id"] is None


async def test_a_real_owner_is_written_through_unchanged(monkeypatch):
    """The positive counterpart: normalization must not blank a real id."""
    seen = {}

    class _Capture:
        async def execute(self, query):
            seen["values"] = dict(query.compile().params)
            return None

    monkeypatch.setattr(mar, "database", _Capture())
    monkeypatch.setattr(mar, "_DDL_READY", True)
    assert await mar.record_audit_run_started(merchant_id="m-7", product_keys=[])
    assert seen["values"]["merchant_id"] == "m-7"


# ---- 3. an unclaimed run must grant nothing --------------------------------

def test_no_authenticated_merchant_can_own_an_unclaimed_run():
    """Every ownership check in merchant_audit_routes compares
    `row.get("merchant_id") != merchant_id`, where the right-hand side comes
    from get_current_merchant — which raises 401 on a falsy claim and so can
    never be None. This pins that the comparison rejects, and pins the reason
    it is safe: there is no authenticated id that equals None.
    """
    import utils.auth as auth
    import inspect

    src = inspect.getsource(auth.get_current_merchant)
    assert "if not merchant_id:" in src and "HTTP_401_UNAUTHORIZED" in src, (
        "get_current_merchant must reject a falsy merchant_id, or an "
        "unclaimed run (merchant_id IS NULL) becomes readable by a token "
        "carrying no merchant_id claim"
    )
    unclaimed = {"merchant_id": None}
    for authenticated in ("m-1", "other", "0"):
        assert unclaimed.get("merchant_id") != authenticated


# ---- 4. findings from review -----------------------------------------------

async def test_a_failing_claim_reports_False_not_success(monkeypatch):
    """M2. The except branch is fail-closed, and nothing pinned it. The
    realistic trigger is concrete: on a database where the schema self-heal
    was starved, the UPDATE raises UndefinedColumn on merchant_claimed_at —
    exactly when a wrong 'you own this run' answer would be most damaging.
    """
    class _Boom:
        async def fetch_all(self, *a, **k):
            raise RuntimeError("UndefinedColumnError: merchant_claimed_at")

    monkeypatch.setattr(mar, "database", _Boom())
    monkeypatch.setattr(mar, "_DDL_READY", True)
    assert await mar.claim_audit_run_for_merchant(
        run_id="run-x", merchant_id="m-1"
    ) is False


async def test_claiming_a_run_you_already_own_does_not_win_again(sqlite_db):
    """M3. 'Claim, not takeover' was only tested against a DIFFERENT merchant,
    so a guard widened to `OR merchant_id = :merchant_id` survived. Re-claiming
    your own run must report False and must not move the timestamp."""
    await _insert(sqlite_db, "run-self", None)
    assert await mar.claim_audit_run_for_merchant(
        run_id="run-self", merchant_id="m-1") is True
    first = (await _owner(sqlite_db, "run-self"))["merchant_claimed_at"]
    assert await mar.claim_audit_run_for_merchant(
        run_id="run-self", merchant_id="m-1") is False
    assert (await _owner(sqlite_db, "run-self"))["merchant_claimed_at"] == first


async def test_the_replay_path_refuses_an_idempotency_key_without_an_owner():
    """F2. Postgres unique indexes are NULLS DISTINCT, so
    (merchant_id, idempotency_key) does not constrain unowned rows — measured
    on PG 15: three inserts of one key all landed. Refusing is what keeps an
    unauthenticated, cost-bearing insert path from silently duplicating."""
    with pytest.raises(ValueError, match="idempotency"):
        await mar.enqueue_audit_run_with_replay(
            merchant_id=None, product_keys=[], idempotency_key="k-1"
        )
    with pytest.raises(ValueError, match="idempotency"):
        await mar.enqueue_audit_run_with_replay(
            merchant_id="  ", product_keys=[], idempotency_key="k-1"
        )


def test_the_self_heal_is_positioned_where_it_can_actually_run():
    """F1. This block sat at the END of schema_guard's single try-block and
    NEVER RAN on a partial database: `CREATE INDEX ... ON commerce_interactions`
    ~1600 lines above has no IF EXISTS guard on its table, raises
    UndefinedTable, and abandons everything after it. Reproduced 2026-09-03 —
    merchant_id stayed NOT NULL and merchant_claimed_at was never added.

    Wrapping alone does not fix that (the abort is UPSTREAM), so this pins the
    POSITION: the block must sit before the first unguarded CREATE INDEX in
    the chain, not merely inside a try."""
    guard = (
        Path(__file__).resolve().parents[1] / "db" / "schema_guard.py"
    ).read_text()
    heal = guard.index("ADD COLUMN IF NOT EXISTS merchant_claimed_at")
    # The statement that starved it. If the self-heal ever drifts back below
    # this line, it stops running on exactly the databases it exists for.
    starver = guard.index("idx_commerce_interactions_store")
    assert heal < starver, (
        "the mig-210 self-heal must precede the unguarded commerce_interactions "
        "CREATE INDEX, or a partial database never reaches it"
    )

