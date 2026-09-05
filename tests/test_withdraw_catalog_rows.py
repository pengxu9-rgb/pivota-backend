"""Named takedown of catalog rows (scripts/withdraw_catalog_rows.py).

Weighted, like the crawl-row takedown's tests, to the two silent failure modes:
a takedown that does not actually close the serving gate, and a takedown that
reports a state it did not read. The SQL itself is planned by the Postgres
PREPARE gate (tests/test_ops_script_sql_prepare_postgres.py); this file records
statements against a fake connection and pins WHAT is written, not that it
plans.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.withdraw_catalog_rows as withdraw  # noqa: E402


class _FakeDB:
    def __init__(self, serving_by_key=None):
        self.executed = []
        self._serving = serving_by_key or {}

    async def execute(self, query, values=None):
        self.executed.append((" ".join(str(query).split()), values or {}))

    async def fetch_all(self, query, values=None):
        return []

    async def fetch_one(self, query, values=None):
        ck = (values or {}).get("ck")
        if ck not in self._serving:
            return None
        return {"serving_eligible": self._serving[ck]}

    def touching(self, fragment):
        return [(q, v) for q, v in self.executed if fragment in q]


def _row(pk="ext:stila-promo::841db8be", ck="ck_promo", active_seeds=1, meta=None, suppressed=None):
    return {
        "product_key": pk, "content_key": ck, "title": "Free Travel Liner (TikTok Shop)",
        "brand": "Stila Cosmetics", "source_system": "catalog_enrichment_agent_v1",
        "source_domain": "stilacosmetics.com", "suppression_reason": None,
        "suppressed_at": suppressed, "suppression_metadata": meta,
        "skus": 1, "offers": 1, "seeds": 1, "active_seeds": active_seeds, "rows_on_key": 1,
    }


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
async def test_withdraw_writes_reason_and_gate_column_in_one_statement(monkeypatch):
    """The serving classifier reads suppressed_at; the label alone leaves the row
    serving. Both land in ONE product UPDATE so an abort cannot mint a reason-only
    row (the tombstoned-flag gate's whole subject)."""
    db = _FakeDB({"ck_promo": False})
    _patch(monkeypatch, db)
    await withdraw._withdraw([_row()], "token_price_promo")

    products = db.touching("UPDATE catalog_products")
    assert len(products) == 1
    q, v = products[0]
    assert "suppressed_at = NOW()" in q and "suppression_reason = COALESCE(suppression_reason" in q
    assert "jsonb_build_object" in q and v["script"] == withdraw.SCRIPT_NAME
    assert v["reason"] == "token_price_promo" and v["prior"] == 1


@pytest.mark.asyncio
async def test_withdraw_closes_skus_offers_and_seeds_too(monkeypatch):
    """A live offer or SKU under a suppressed product would still feed a price;
    an active seed would keep the servability lane re-promoting it."""
    db = _FakeDB({"ck_promo": False})
    _patch(monkeypatch, db)
    await withdraw._withdraw([_row()], "token_price_promo")

    assert db.touching("UPDATE catalog_skus") and db.touching("UPDATE catalog_offers")
    seeds = db.touching("UPDATE external_product_seeds")
    assert seeds and "status = 'inactive'" in seeds[0][0] and "status = 'active'" in seeds[0][0]


@pytest.mark.asyncio
async def test_every_write_is_idempotency_guarded(monkeypatch):
    db = _FakeDB({"ck_promo": False})
    _patch(monkeypatch, db)
    await withdraw._withdraw([_row()], "token_price_promo")
    for q, _ in db.executed:
        if q.startswith("UPDATE"):
            assert "suppressed_at IS NULL" in q or "status = 'active'" in q, q


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


@pytest.mark.asyncio
async def test_revert_only_touches_rows_this_script_suppressed(monkeypatch):
    db = _FakeDB({"ck_promo": True})
    _patch(monkeypatch, db)
    theirs = _row(pk="theirs", meta={"script": "remediate_unpublished_crawl_rows"}, suppressed="2026-01-01")
    ours = _row(pk="ours", meta={"script": withdraw.SCRIPT_NAME, "reason": "token_price_promo",
                                  "prior_active_seeds": 1}, suppressed="2026-01-01")
    await withdraw._revert([theirs, ours])

    touched = {v.get("pk") for _, v in db.executed if "pk" in v}
    assert touched == {"ours"}
    seeds = db.touching("UPDATE external_product_seeds")
    assert seeds and "status = 'active'" in seeds[0][0]


@pytest.mark.asyncio
async def test_revert_clears_only_our_reason_and_leaves_dead_seeds_dead(monkeypatch):
    db = _FakeDB({"ck_promo": True})
    _patch(monkeypatch, db)
    ours = _row(pk="ours", meta={"script": withdraw.SCRIPT_NAME, "reason": "token_price_promo",
                                  "prior_active_seeds": 0}, suppressed="2026-01-01")
    await withdraw._revert([ours])

    q, v = db.touching("UPDATE catalog_products")[0]
    assert "suppressed_at = NULL" in q
    assert "CASE WHEN suppression_reason = CAST(:reason AS text) THEN NULL" in q
    assert v["reason"] == "token_price_promo"
    assert not db.touching("UPDATE external_product_seeds")  # nothing was active before


@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_writes_nothing(monkeypatch):
    """No --apply: the rows are reported and NOT ONE UPDATE is sent."""
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
