"""The claimed-funnel-checks QUERY against REAL Postgres.

WHY A POSTGRES GATE. The route tests stub the reader entirely, so mutants that
dropped the `merchant_id` filter and the `merchant_claimed_at` filter both
SURVIVED them — a tenancy filter with no coverage. It cannot be tested on
SQLite either: the reader selects DateTime(timezone=True) columns and SQLite
cannot round-trip those, so a SQLite version fails for a dialect reason and
proves nothing about the filter it exists to guard.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_claimed_funnel_checks_postgres.py
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop merchant_audit_runs in {dbname!r}")


_SHAPE = """
CREATE TABLE merchant_audit_runs (
  run_id               UUID PRIMARY KEY,
  merchant_id          TEXT NULL,
  subject_type         TEXT NOT NULL DEFAULT 'merchant',
  status               TEXT NOT NULL,
  requested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  merchant_claimed_at  TIMESTAMPTZ NULL,
  product_keys         TEXT[] NOT NULL DEFAULT '{}',
  partial_result_jsonb JSONB NULL
)
"""


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database
    _assert_throwaway_database()
    was = database.is_connected
    if not was:
        await database.connect()
    await database.execute("DROP TABLE IF EXISTS merchant_audit_runs")
    await database.execute(_SHAPE)
    import db.merchant_audit_runs as mar
    mar._DDL_READY = True
    yield
    mar._DDL_READY = False
    if not was and database.is_connected:
        await database.disconnect()


def _now():
    return datetime.now(timezone.utc)


async def _insert(*, merchant_id, subject_type, claimed_at, domain="anua.com"):
    from db.database import database
    run_id = str(uuid.uuid4())
    await database.execute(
        "INSERT INTO merchant_audit_runs "
        "(run_id, merchant_id, subject_type, status, merchant_claimed_at, "
        " partial_result_jsonb) "
        "VALUES (:r, :m, :s, 'succeeded', :c, CAST(:p AS JSONB))",
        {"r": run_id, "m": merchant_id, "s": subject_type, "c": claimed_at,
         "p": '{"funnel": {"domain": "%s"}}' % domain},
    )
    return run_id


async def test_only_this_merchants_claimed_funnel_runs_come_back():
    """The tenancy filter both route-level mutants walked straight through."""
    import db.merchant_audit_runs as mar

    mine = await _insert(merchant_id="m-1", subject_type="public_funnel",
                         claimed_at=_now())
    await _insert(merchant_id="m-2", subject_type="public_funnel",
                  claimed_at=_now())
    await _insert(merchant_id=None, subject_type="public_funnel",
                  claimed_at=None)
    await _insert(merchant_id="m-1", subject_type="merchant_url",
                  claimed_at=_now())

    rows = await mar.list_claimed_funnel_runs_for_merchant(merchant_id="m-1")
    assert [str(r["run_id"]) for r in rows] == [mine], (
        "must exclude other merchants, unclaimed runs, and other lanes"
    )


async def test_an_empty_merchant_id_selects_nothing():
    """A falsy caller must select nothing, not everything."""
    import db.merchant_audit_runs as mar
    await _insert(merchant_id="m-1", subject_type="public_funnel",
                  claimed_at=_now())
    assert await mar.list_claimed_funnel_runs_for_merchant(merchant_id="") == []


async def test_newest_claim_first():
    import db.merchant_audit_runs as mar
    older = await _insert(merchant_id="m-1", subject_type="public_funnel",
                          claimed_at=_now() - timedelta(hours=5))
    newer = await _insert(merchant_id="m-1", subject_type="public_funnel",
                          claimed_at=_now())
    rows = await mar.list_claimed_funnel_runs_for_merchant(merchant_id="m-1")
    assert [str(r["run_id"]) for r in rows] == [newer, older]


async def test_the_limit_is_honoured():
    import db.merchant_audit_runs as mar
    for i in range(5):
        await _insert(merchant_id="m-1", subject_type="public_funnel",
                      claimed_at=_now() - timedelta(minutes=i))
    rows = await mar.list_claimed_funnel_runs_for_merchant(
        merchant_id="m-1", limit=2)
    assert len(rows) == 2


async def test_the_reader_survives_a_table_missing_a_modeled_column():
    """Why it names its columns instead of select()ing the Table: a select()
    over the full model breaks on any environment missing a modeled column,
    and this function swallows — so the merchant would be told they have no
    claimed checks, a wrong answer wearing the shape of a right one."""
    import db.merchant_audit_runs as mar
    rows = await mar.list_claimed_funnel_runs_for_merchant(merchant_id="m-1")
    assert rows == []  # the minimal table above lacks most modeled columns
    await _insert(merchant_id="m-1", subject_type="public_funnel",
                  claimed_at=_now())
    assert len(await mar.list_claimed_funnel_runs_for_merchant(
        merchant_id="m-1")) == 1


async def test_a_funnel_run_owned_but_never_claimed_is_excluded():
    """The `merchant_claimed_at IS NOT NULL` conjunct.

    The claim sets owner and timestamp together, so this state should not
    arise from the code path — but a manual UPDATE, a partial restore, or a
    future writer that sets merchant_id alone would produce it, and the row
    would then be reported to a merchant as a check they claimed. The filter
    is what makes 'claimed' mean claimed rather than merely owned.
    """
    import db.merchant_audit_runs as mar
    from db.database import database

    claimed = await _insert(merchant_id="m-1", subject_type="public_funnel",
                            claimed_at=_now())
    await database.execute(
        "INSERT INTO merchant_audit_runs "
        "(run_id, merchant_id, subject_type, status, merchant_claimed_at) "
        "VALUES (:r, 'm-1', 'public_funnel', 'succeeded', NULL)",
        {"r": str(uuid.uuid4())},
    )
    rows = await mar.list_claimed_funnel_runs_for_merchant(merchant_id="m-1")
    assert [str(r["run_id"]) for r in rows] == [claimed]


async def test_a_falsy_merchant_id_never_reaches_the_database(monkeypatch):
    """The short-circuit, pinned as a short-circuit.

    An empty id happens to match no row, so deleting the guard changes no
    result — which is exactly how a guard rots into decoration. What it
    actually buys is not issuing the query at all, so that is what is
    asserted.
    """
    import db.merchant_audit_runs as mar
    from db.database import database

    called = {"n": 0}
    real = database.fetch_all

    async def counting(*a, **k):
        called["n"] += 1
        return await real(*a, **k)

    monkeypatch.setattr(database, "fetch_all", counting)
    assert await mar.list_claimed_funnel_runs_for_merchant(merchant_id="") == []
    assert await mar.list_claimed_funnel_runs_for_merchant(merchant_id=None) == []
    assert called["n"] == 0, "a falsy id must not issue a query"
