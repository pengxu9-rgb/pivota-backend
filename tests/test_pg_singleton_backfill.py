"""ADR-009 decision 1 — pg-singleton backfill (scripts/backfill_pg_singleton.py), unit tests.

Proves, without a database:
  - DRY-RUN IS READ-ONLY: a full `run_backfill(execute=False)` performs ZERO
    execute() calls (only SELECT/fetch traffic) and still reports would_mint /
    distinct_pgs correctly (shared-content_key rows collapse onto ONE pg —
    correct grouping of one physical product, never a merge of distinct ones);
  - the EXECUTE path mints via `mint_mod.ensure_singleton_group_membership`
    (reuse, not reimplementation) with the deterministic singleton pg, writes
    idempotent checkpoint rows, and passes per-batch parity on the happy path;
  - a parity violation (membership count mismatch after the mint) raises
    RuntimeError — LOUD abort, never silently absorbed;
  - the plan SQL excludes rows already in product_group_members (NOT EXISTS)
    and NULL/blank-content_key rows; NULL-ck rows land in the
    review_null_content_key count, left pg-NULL (never force-minted).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import backfill_pg_singleton as bf  # noqa: E402
from services import product_group_autogrouper as mint_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeBackfillDB:
    """In-memory catalog_products + product_group_members. Routes queries by
    substring (like the A9-4 FakeDB) and APPLIES membership INSERTs so parity
    reads observe the writes."""

    def __init__(self, products: List[Dict[str, Any]],
                 memberships: Optional[Dict[Tuple[str, str, str], str]] = None) -> None:
        # products: product_key, merchant_id, platform, source_product_id,
        #           content_key, pivota_signature_id, pivota_canonical_url
        self.products = [dict(p) for p in products]
        self.memberships: Dict[Tuple[str, str, str], str] = dict(memberships or {})
        self.executed: List[Tuple[str, Dict[str, Any]]] = []

    # -- helpers -------------------------------------------------------------

    def _mintable(self) -> List[Dict[str, Any]]:
        out = []
        for p in self.products:
            ck = p.get("content_key")
            if not isinstance(ck, str) or not ck.strip():
                continue
            key = (p["merchant_id"], p["platform"], p["source_product_id"])
            if key in self.memberships:
                continue
            out.append(dict(p))
        return sorted(out, key=lambda r: r["product_key"])

    # -- async API -------------------------------------------------------------

    async def fetch_one(self, query: str, values: Optional[Dict[str, Any]] = None):
        q = " ".join(str(query).split())
        v = dict(values or {})
        if "count(*) AS c FROM product_group_members WHERE merchant_id=:m" in q:
            key = (v["m"], v["p"], v["s"])
            return {"c": 1 if key in self.memberships else 0}
        if "FROM catalog_products" in q and "NOT EXISTS" in q:
            return {"c": len(self._mintable())}
        if "FROM catalog_products" in q and "content_key IS NULL" in q:
            n = sum(
                1 for p in self.products
                if not isinstance(p.get("content_key"), str) or not str(p.get("content_key") or "").strip()
            )
            return {"c": n}
        if "FROM catalog_products cp WHERE EXISTS" in q:
            n = sum(
                1 for p in self.products
                if (p["merchant_id"], p["platform"], p["source_product_id"]) in self.memberships
            )
            return {"c": n}
        raise AssertionError(f"unrouted fetch_one: {q[:120]}")

    async def fetch_all(self, query: str, values: Optional[Dict[str, Any]] = None):
        q = " ".join(str(query).split())
        v = dict(values or {})
        if "NOT EXISTS" in q and "ORDER BY cp.product_key" in q:
            return self._mintable()
        if "FROM catalog_products WHERE product_key = ANY(:pks)" in q:
            pks = set(v.get("pks") or [])
            return [
                {
                    "product_key": p["product_key"],
                    "pivota_signature_id": p.get("pivota_signature_id"),
                    "pivota_canonical_url": p.get("pivota_canonical_url"),
                }
                for p in self.products if p["product_key"] in pks
            ]
        if "JOIN product_group_members m" in q:
            pks = set(v.get("pks") or [])
            out = []
            for p in self.products:
                if p["product_key"] not in pks:
                    continue
                key = (p["merchant_id"], p["platform"], p["source_product_id"])
                if key in self.memberships:
                    out.append({"product_key": p["product_key"],
                                "product_group_id": self.memberships[key]})
            return out
        raise AssertionError(f"unrouted fetch_all: {q[:120]}")

    async def execute(self, query: str, values: Optional[Dict[str, Any]] = None):
        q = " ".join(str(query).split())
        v = dict(values or {})
        self.executed.append((q, v))
        if "INSERT INTO product_group_members" in q:
            key = (v["merchant_id"], v["platform"], v["platform_product_id"])
            # ON CONFLICT DO NOTHING semantics — an existing membership is never
            # overwritten.
            self.memberships.setdefault(key, v["product_group_id"])
        return None

    def transaction(self):
        return _FakeTxn()


def _product(pk: str, ck: Optional[str], *, merchant: str = "merch_a",
             platform: str = "shopify", spid: Optional[str] = None) -> Dict[str, Any]:
    spid = spid or pk.rsplit("::", 1)[-1]
    return {
        "product_key": pk,
        "merchant_id": merchant,
        "platform": platform,
        "source_product_id": spid,
        "content_key": ck,
        "pivota_signature_id": f"sig_{pk[-6:]}",
        "pivota_canonical_url": f"https://agent.pivota.cc/products/sig_{pk[-6:]}",
    }


CK_1 = "ck_00000000000000000000000000000001"
CK_2 = "ck_00000000000000000000000000000002"
CK_SHARED = "ck_0000000000000000000000000000ffff"


# ---------------------------------------------------------------------------
# 1. Dry-run is READ-ONLY + reports correctly
# ---------------------------------------------------------------------------


async def test_dry_run_performs_zero_writes_and_reports_would_mint():
    db = FakeBackfillDB([
        _product("prod::merch_a::shopify::p1", CK_1),
        _product("prod::merch_b::shopify::p2", CK_2, merchant="merch_b"),
    ])
    report = await bf.run_backfill(database=db, mint_mod=mint_mod,
                                   execute=False, batch_size=100)
    assert report.mode == "dry_run"
    assert db.executed == [], "dry-run must perform ZERO execute() calls"
    assert db.memberships == {}
    assert report.planned == 2
    assert report.minted == 0
    assert report.batches == 1
    parity = report.parity[0]
    assert parity["mode"] == "dry_run"
    assert parity["would_mint"] == 2
    assert parity["distinct_pgs"] == 2  # distinct cks → distinct pgs


async def test_dry_run_shared_content_key_collapses_to_one_pg():
    """Two listings of ONE physical product (same content_key, different
    merchants) share the singleton — correct grouping, not a merge."""
    db = FakeBackfillDB([
        _product("prod::merch_a::shopify::x1", CK_SHARED, merchant="merch_a"),
        _product("prod::merch_b::shopify::x2", CK_SHARED, merchant="merch_b"),
    ])
    report = await bf.run_backfill(database=db, mint_mod=mint_mod,
                                   execute=False, batch_size=100)
    assert db.executed == []
    assert report.parity[0]["would_mint"] == 2
    assert report.parity[0]["distinct_pgs"] == 1


# ---------------------------------------------------------------------------
# 2. Execute path — mints via the REAL helper, checkpoints, parity ok
# ---------------------------------------------------------------------------


async def test_execute_mints_via_helper_with_checkpoints_and_parity(monkeypatch):
    minted_calls: List[Dict[str, Any]] = []
    real_mint = mint_mod.ensure_singleton_group_membership

    async def spying_mint(**kwargs):
        minted_calls.append(dict(kwargs))
        return await real_mint(**kwargs)

    monkeypatch.setattr(mint_mod, "ensure_singleton_group_membership", spying_mint)

    db = FakeBackfillDB([
        _product("prod::merch_a::shopify::p1", CK_1),
        _product("prod::merch_b::shopify::p2", CK_2, merchant="merch_b"),
    ])
    report = await bf.run_backfill(database=db, mint_mod=mint_mod,
                                   execute=True, batch_size=100)

    # minted THROUGH the helper (reuse, not reimplementation), db handle threaded
    assert len(minted_calls) == 2
    assert all(c["db"] is db for c in minted_calls)

    # memberships landed with the deterministic singleton pgs
    assert db.memberships[("merch_a", "shopify", "p1")] == mint_mod.make_singleton_product_group_id(CK_1)
    assert db.memberships[("merch_b", "shopify", "p2")] == mint_mod.make_singleton_product_group_id(CK_2)

    # parity happy path
    assert report.minted == 2
    parity = report.parity[0]
    assert parity["ok"] and parity["members_ok"] and parity["sigs_frozen"] and parity["pg_correct"]

    # checkpoint table ensured + one idempotent upsert per product
    joined = " ".join(q for q, _ in db.executed)
    assert "CREATE TABLE IF NOT EXISTS pg_singleton_backfill_checkpoint" in joined
    ckpt_writes = [
        (q, v) for q, v in db.executed
        if "INSERT INTO pg_singleton_backfill_checkpoint" in q
    ]
    assert len(ckpt_writes) == 2
    assert all("ON CONFLICT (product_key)" in q for q, _ in ckpt_writes)
    ckpt_by_pk = {v["pk"]: v["pg"] for _, v in ckpt_writes}
    assert ckpt_by_pk["prod::merch_a::shopify::p1"] == mint_mod.make_singleton_product_group_id(CK_1)


async def test_execute_parity_failure_raises_runtime_error():
    """Simulate a membership-count mismatch (the INSERT silently not landing):
    the batch must abort LOUDLY with RuntimeError, not log-and-continue."""

    class DroppingDB(FakeBackfillDB):
        async def execute(self, query: str, values: Optional[Dict[str, Any]] = None):
            q = " ".join(str(query).split())
            self.executed.append((q, dict(values or {})))
            # membership INSERT dropped on the floor -> after_members == before
            return None

    db = DroppingDB([_product("prod::merch_a::shopify::p1", CK_1)])
    with pytest.raises(RuntimeError, match="parity FAILED"):
        await bf.run_backfill(database=db, mint_mod=mint_mod,
                              execute=True, batch_size=100)


# ---------------------------------------------------------------------------
# 3. Plan scope — already-grouped and NULL-ck rows are excluded
# ---------------------------------------------------------------------------


def test_plan_sql_excludes_grouped_and_null_content_key_rows():
    """String pin on the plan SQL itself: membership NOT EXISTS + non-blank
    content_key are load-bearing filters (a singleton NEVER overwrites a real
    group; a pg is NEVER minted from nothing)."""
    q = " ".join(bf._PLAN_SQL.split())
    assert "cp.content_key IS NOT NULL" in q
    assert "btrim(cp.content_key) <> ''" in q
    assert "NOT EXISTS" in q and "FROM product_group_members" in q
    assert "ORDER BY cp.product_key" in q  # stable, resumable batch order


async def test_plan_and_review_routing_with_mixed_rows():
    grouped_key = ("merch_a", "shopify", "grouped")
    db = FakeBackfillDB(
        [
            # already in a (curated) group -> SKIPPED, membership untouched
            _product("prod::merch_a::shopify::grouped", CK_1, spid="grouped"),
            # mintable
            _product("prod::merch_a::shopify::lone", CK_2, spid="lone"),
            # NULL / blank content_key -> review, left pg-NULL
            _product("prod::merch_a::shopify::null1", None, spid="null1"),
            _product("prod::merch_a::shopify::null2", "   ", spid="null2"),
        ],
        memberships={grouped_key: "pg_curated_do_not_touch"},
    )
    report = await bf.run_backfill(database=db, mint_mod=mint_mod,
                                   execute=True, batch_size=100)

    assert report.planned == 1  # only the lone mintable row
    assert report.minted == 1
    assert report.review_null_content_key == 2
    assert report.skipped_already_grouped == 1
    assert report.counts["catalog_missing_pg"] == 1  # pre-run scan

    # the curated membership is byte-identical after the run
    assert db.memberships[grouped_key] == "pg_curated_do_not_touch"
    # NULL-ck rows never got a membership row
    assert ("merch_a", "shopify", "null1") not in db.memberships
    assert ("merch_a", "shopify", "null2") not in db.memberships
    # and the lone row carries its deterministic singleton
    assert db.memberships[("merch_a", "shopify", "lone")] == mint_mod.make_singleton_product_group_id(CK_2)
