"""Named takedown of catalog rows (scripts/withdraw_catalog_rows.py).

Weighted, like the crawl-row takedown's tests, to the silent failure modes:
a takedown that does not actually close the serving gate, a revert that
resurrects rows or seeds ANOTHER lane retired, and a report that states
counts it did not read. The SQL itself is planned by the Postgres PREPARE gate
(tests/test_ops_script_sql_prepare_postgres.py); this file records statements
against a fake connection and pins WHAT is written, not that it plans.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.withdraw_catalog_rows as withdraw  # noqa: E402


class _FakeDB:
    def __init__(self, serving_by_key=None, active_seed_ids=None, ours=None):
        self.executed = []
        self._serving = serving_by_key or {}
        self._active_seed_ids = active_seed_ids or []
        self._ours = ours or []

    async def execute(self, query, values=None):
        self.executed.append((" ".join(str(query).split()), values or {}))

    async def fetch_all(self, query, values=None):
        q = " ".join(str(query).split())
        if "SELECT id FROM external_product_seeds" in q:
            return [{"id": s} for s in self._active_seed_ids]
        if "suppression_metadata->>'script'" in q:
            return list(self._ours)
        return []

    async def fetch_one(self, query, values=None):
        ck = (values or {}).get("ck")
        if ck not in self._serving:
            return None
        return {"serving_eligible": self._serving[ck]}

    def touching(self, fragment):
        return [(q, v) for q, v in self.executed if fragment in q]


def _row(pk="ext:stila-promo::841db8be", ck="ck_promo", meta=None, suppressed=None, **over):
    row = {
        "product_key": pk, "content_key": ck, "title": "Free Travel Liner (TikTok Shop)",
        "brand": "Stila Cosmetics", "source_system": "catalog_enrichment_agent_v1",
        "source_domain": "stilacosmetics.com", "suppression_reason": None,
        "suppressed_at": suppressed, "suppression_metadata": meta,
        "skus": 1, "offers": 1, "seeds": 2, "active_seeds": 1, "rows_on_key": 1,
    }
    row.update(over)
    return row


async def _async(v):
    return v


def _patch(monkeypatch, db):
    monkeypatch.setattr(withdraw, "database", db)
    monkeypatch.setattr(withdraw, "recompute_serving_eligibility", lambda ck, reason=None: _async(True))
    trusted = []
    monkeypatch.setattr(
        withdraw, "upsert_catalog_row_trust_many",
        lambda *, db, product_keys, **kw: (trusted.extend(product_keys), _async(len(product_keys)))[1],
    )
    return trusted


# --- withdraw -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_withdraw_stamps_reason_gate_and_ownership_in_one_statement_per_table(monkeypatch):
    """The serving classifier reads suppressed_at; the label alone leaves the row
    serving. Reason, gate column and the ownership stamp land in ONE UPDATE on
    each of the three tables, so an abort cannot mint a reason-only row and a
    later --revert can tell our suppression from another lane's."""
    db = _FakeDB({"ck_promo": False}, active_seed_ids=["seed_a"])
    _patch(monkeypatch, db)
    await withdraw._withdraw([_row()], "token_price_promo")

    for table in ("catalog_products", "catalog_skus", "catalog_offers"):
        stmts = db.touching(f"UPDATE {table} ")
        assert len(stmts) == 1, table
        q, v = stmts[0]
        assert "suppressed_at = NOW()" in q
        assert "suppression_reason = COALESCE(suppression_reason, CAST(:reason AS text))" in q
        assert "'script', CAST(:script AS text)" in q and v["script"] == withdraw.SCRIPT_NAME
        assert v["reason"] == "token_price_promo"
        assert "suppressed_at IS NULL" in q  # idempotent; never re-gates a foreign suppression


@pytest.mark.asyncio
async def test_withdraw_deactivates_only_the_active_seeds_and_records_their_ids(monkeypatch):
    """Two seeds on the key, one already retired by the destination sweep: only
    the active one is touched, and exactly its id is recorded for --revert."""
    db = _FakeDB({"ck_promo": False}, active_seed_ids=["seed_active"])
    _patch(monkeypatch, db)
    await withdraw._withdraw([_row(seeds=2, active_seeds=1)], "token_price_promo")

    seeds = db.touching("UPDATE external_product_seeds")
    assert len(seeds) == 1
    q, v = seeds[0]
    assert "status = 'inactive'" in q and "id = ANY(:ids)" in q and "status = 'active'" in q
    assert v["ids"] == ["seed_active"]
    q, v = db.touching("UPDATE catalog_products ")[0]
    assert "'deactivated_seed_ids', CAST(:seed_ids AS jsonb)" in q
    assert json.loads(v["seed_ids"]) == ["seed_active"]


@pytest.mark.asyncio
async def test_withdraw_reads_state_back_and_upserts_trust(monkeypatch):
    """'dark' must come from index_pipeline_state, not from the recompute's
    return value; and the trust column public readers gate on is refreshed."""
    db = _FakeDB({"ck_dark": False, "ck_live": True})  # ck_missing absent entirely
    trusted = _patch(monkeypatch, db)
    states = await withdraw._withdraw(
        [_row(pk="p1", ck="ck_dark"), _row(pk="p2", ck="ck_live"), _row(pk="p3", ck="ck_missing")],
        "x",
    )
    assert states == {"ck_dark": "dark", "ck_live": "serving", "ck_missing": "unknown"}
    assert sorted(trusted) == ["p1", "p2", "p3"]


# --- revert -------------------------------------------------------------------


def _ours(pk="ours", seed_ids=("seed_active",)):
    return _row(pk=pk, suppressed="2026-01-01",
                meta={"script": withdraw.SCRIPT_NAME, "reason": "token_price_promo",
                      "deactivated_seed_ids": list(seed_ids)})


@pytest.mark.asyncio
async def test_revert_guards_every_table_on_our_ownership_stamp(monkeypatch):
    """A SKU/offer another lane tombstoned before we withdrew the product must
    keep its gate column AND its reason: the child-table reverts are guarded on
    suppression_metadata->>'script' = ours, not merely on suppressed_at."""
    db = _FakeDB({"ck_promo": True})
    _patch(monkeypatch, db)
    await withdraw._revert([_ours()])

    for table in ("catalog_products", "catalog_skus", "catalog_offers"):
        q, v = db.touching(f"UPDATE {table} ")[0]
        assert "suppressed_at = NULL" in q
        assert "suppression_metadata->>'script' = CAST(:script AS text)" in q
        assert "CASE WHEN suppression_reason = CAST(:reason AS text) THEN NULL" in q
        assert v["script"] == withdraw.SCRIPT_NAME and v["reason"] == "token_price_promo"


@pytest.mark.asyncio
async def test_revert_reactivates_exactly_the_recorded_seeds(monkeypatch):
    """Never `WHERE attached_product_key = :pk AND status='inactive'` — that
    would resurrect a seed the 404 sweep retired. Only the ids we put to sleep."""
    db = _FakeDB({"ck_promo": True})
    _patch(monkeypatch, db)
    await withdraw._revert([_ours(seed_ids=("seed_active",))])

    q, v = db.touching("UPDATE external_product_seeds")[0]
    assert "id = ANY(:ids)" in q and "attached_product_key" not in q
    assert v["ids"] == ["seed_active"]


@pytest.mark.asyncio
async def test_revert_skips_rows_another_lane_suppressed(monkeypatch):
    db = _FakeDB({"ck_promo": True})
    _patch(monkeypatch, db)
    theirs = _row(pk="theirs", suppressed="2026-01-01", meta={"script": "remediate_unpublished_crawl_rows"})
    await withdraw._revert([theirs, _ours()])

    touched = {v.get("pk") for _, v in db.executed if "pk" in v}
    assert touched == {"ours"}


@pytest.mark.asyncio
async def test_revert_report_carries_real_counts_not_zeros(monkeypatch):
    """--revert (dry-run) prints the same child/sibling counts as withdraw does:
    the ours-loader shares the counting SELECT, it does not hardcode zeros."""
    import argparse

    class _DB(_FakeDB):
        is_connected = True

        async def disconnect(self):
            self.is_connected = False

    db = _DB({"ck_promo": True}, ours=[_ours(pk="ours") | {"skus": 3, "offers": 2, "seeds": 2, "rows_on_key": 4}])
    _patch(monkeypatch, db)
    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))
    rc = await withdraw._run(argparse.Namespace(product_key=None, reason="x", apply=False, revert=True))
    assert rc == 0
    assert any("skus=3 offers=2 seeds=2" in line and "live rows on key=4" in line for line in printed)
    assert withdraw._LOAD_OURS_SQL.startswith(withdraw._ROW_COLUMNS_SQL)
    assert not [q for q, _ in db.executed if q.startswith("UPDATE")]


# --- CLI contract -------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_writes_nothing(monkeypatch):
    import argparse

    class _DB(_FakeDB):
        is_connected = True

        async def fetch_all(self, query, values=None):
            return [_row()] if "product_key = ANY" in " ".join(str(query).split()) else []

        async def disconnect(self):
            self.is_connected = False

    db = _DB({"ck_promo": True})
    _patch(monkeypatch, db)
    rc = await withdraw._run(argparse.Namespace(
        product_key=["ext:stila-promo::841db8be"], reason="token_price_promo", apply=False, revert=False))
    assert rc == 0
    assert not [q for q, _ in db.executed if q.startswith("UPDATE")]


@pytest.mark.asyncio
async def test_withdraw_requires_a_product_key(monkeypatch):
    import argparse

    class _DB(_FakeDB):
        is_connected = True

        async def disconnect(self):
            self.is_connected = False

    db = _DB()
    _patch(monkeypatch, db)
    rc = await withdraw._run(argparse.Namespace(product_key=None, reason="x", apply=True, revert=False))
    assert rc == 2 and not db.executed
