"""The external-seed → catalog_offers reconciler must be able to RUN, and must
keep telling the truth about how much drift exists while it does.

WHY THIS EXISTS. `scripts/reconcile_external_seed_offers.py` is the designed
repair for price / availability / currency drift between `external_product_seeds`
and its `catalog_offers` mirror. Measured on prod 2026-09-16 it had never
completed a single run: its FIRST query died on `canceling statement due to
statement timeout`. Nothing in the repo invokes it either, so the drift it exists
to repair had accumulated unchecked.

Two things had to change together, and only one of them is testable here:

  * the INDEX (migration 223) is the actual fix -- measured, a LIMIT-1000 form of
    the same query still took 30,173 ms against prod without it, returning 2 rows;
  * bounding the row fetch is the other half, and it must NOT be allowed to cap
    the reported totals, or the report would say "1000 drifted" forever no matter
    how bad the drift got. That property is what this file pins.
"""

from typing import Any, Dict, List

import pytest

import scripts.reconcile_external_seed_offers as rec


class _FakeDB:
    """Answers the reconciler's queries by SHAPE, recording the binds it was given."""

    def __init__(self, *, missing_rows: int, drift_rows: int, orphan_rows: int,
                 missing_total: int, drift_total: int, orphan_total: int):
        self.missing_rows = missing_rows
        self.drift_rows = drift_rows
        self.orphan_rows = orphan_rows
        self.totals = {"missing": missing_total, "drift": drift_total, "orphan": orphan_total}
        self.seen_binds: List[Dict[str, Any]] = []
        self.limited_queries = 0
        self.count_queries = 0

    def _kind(self, q: str) -> str:
        if "co.offer_id IS NULL" in q:
            return "missing"
        if "IS DISTINCT FROM" in q:
            return "drift"
        return "orphan"

    async def fetch_all(self, query, values=None):
        q = str(query)
        self.seen_binds.append(dict(values or {}))
        assert "LIMIT :row_limit" in q, "row fetches must be bounded at the database"
        self.limited_queries += 1
        n = {"missing": self.missing_rows, "drift": self.drift_rows, "orphan": self.orphan_rows}[self._kind(q)]
        return [{"seed_id": "s%d" % i, "offer_id": "o%d" % i} for i in range(n)]

    async def fetch_one(self, query, values=None):
        q = str(query)
        assert "COUNT(*)" in q, "totals must come from a COUNT, not from len(rows)"
        assert "LIMIT" not in q, "the COUNT must not be capped or the totals lie"
        self.count_queries += 1
        return {"n": self.totals[self._kind(q)]}


@pytest.fixture
def fake_db(monkeypatch):
    def _install(**kwargs):
        db = _FakeDB(**kwargs)
        monkeypatch.setattr(rec, "database", db)
        return db
    return _install


@pytest.mark.asyncio
async def test_totals_are_counted_not_inferred_from_the_capped_row_fetch(fake_db):
    """THE defect this guards: reporting len(rows) when rows are capped."""
    db = fake_db(
        missing_rows=5, drift_rows=5, orphan_rows=5,
        missing_total=2, drift_total=1762, orphan_total=9,
    )

    report = await rec.run_reconcile(apply=False, limit=5, sample_limit=3)

    # The drift total is 1762 even though only 5 rows were fetched. If these came
    # from len(rows) they would all read 5 and the report would under-state the
    # backlog by three orders of magnitude.
    assert report["missing_offers"] == 2
    assert report["drifted_offers"] == 1762
    assert report["orphan_offers"] == 9
    assert db.count_queries == 3

    # And the caller can still see how much was actually examined.
    assert report["rows_examined"] == {"missing": 5, "drift": 5, "orphan": 5}


@pytest.mark.asyncio
async def test_every_row_fetch_is_bounded_at_the_database(fake_db):
    db = fake_db(
        missing_rows=1, drift_rows=1, orphan_rows=1,
        missing_total=1, drift_total=1, orphan_total=1,
    )

    report = await rec.run_reconcile(apply=False, limit=250, sample_limit=10)

    assert db.limited_queries == 3
    # row_limit covers the larger of the repair limit and the sample size, so
    # neither is silently truncated by the other.
    assert report["row_limit"] == 250
    for binds in db.seen_binds:
        assert binds["row_limit"] == 250


@pytest.mark.asyncio
async def test_row_limit_never_collapses_to_zero(fake_db):
    """A 0/None limit must not turn into `LIMIT 0`, which would fetch nothing and
    report a clean system."""
    db = fake_db(
        missing_rows=1, drift_rows=1, orphan_rows=1,
        missing_total=7, drift_total=7, orphan_total=7,
    )

    report = await rec.run_reconcile(apply=False, limit=0, sample_limit=0)

    assert report["row_limit"] >= 1
    for binds in db.seen_binds:
        assert binds["row_limit"] >= 1
    # The totals are unaffected by the row cap -- still the real numbers.
    assert report["drifted_offers"] == 7


@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_writes_nothing(fake_db):
    """CONTROL: none of the above may quietly repair anything."""
    db = fake_db(
        missing_rows=3, drift_rows=3, orphan_rows=3,
        missing_total=3, drift_total=3, orphan_total=3,
    )

    report = await rec.run_reconcile(apply=False, limit=10, sample_limit=2)

    assert report["apply"] is False
    assert report["repaired"] == 0
    assert "remaining_drift" not in report, "a dry run must not re-measure as if it had repaired"
