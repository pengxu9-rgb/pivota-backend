"""Tests for cross-audit competitor recurrence (fake db, no network)."""

import asyncio

import services.competitor_recurrence as cr
from services.audit_catalog_coverage import audit_to_candidates


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def fetch_all(self, sql):
        return self._rows


def _rows(*triples):
    return [{"merchant_id": m, "run_id": r, "competitor": c} for (m, r, c) in triples]


def test_ranks_by_distinct_merchants_then_mentions():
    db = _FakeDB(_rows(
        ("m1", "a1", "Vital Proteins"), ("m2", "a2", "Vital Proteins"), ("m3", "a3", "vital  proteins"),
        ("m1", "a1", "Olly Collagen"), ("m1", "a4", "Olly Collagen"),  # 1 merchant, 2 audits
        ("m1", "a1", "Solo Brand"),
    ))
    top = asyncio.run(cr.top_recurring_competitors(db=db, min_merchants=1))
    assert top[0]["brand"] == "Vital Proteins"          # 3 distinct merchants
    assert top[0]["distinct_merchants"] == 3 and top[0]["total_mentions"] == 3
    assert top[1]["normalized"] == "olly collagen"       # 1 merchant, 2 mentions > Solo (1)
    assert top[2]["normalized"] == "solo brand"


def test_min_merchants_floor():
    db = _FakeDB(_rows(("m1", "a1", "X"), ("m1", "a2", "X"), ("m1", "a1", "Y"), ("m2", "a3", "Y")))
    top = asyncio.run(cr.top_recurring_competitors(db=db, min_merchants=2))
    assert [d["normalized"] for d in top] == ["y"]  # only Y spans 2 merchants


def test_excludes_non_brand_marketplaces():
    db = _FakeDB(_rows(("m1", "a1", "eBay"), ("m2", "a2", "eBay"), ("m1", "a1", "Anua")))
    norms = [d["normalized"] for d in asyncio.run(cr.top_recurring_competitors(db=db))]
    assert "ebay" not in norms and "anua" in norms
    # opt-in to keep them
    norms2 = [d["normalized"] for d in asyncio.run(cr.top_recurring_competitors(db=db, exclude_non_brands=False))]
    assert "ebay" in norms2


def test_db_miss_is_empty():
    class Boom:
        async def fetch_all(self, sql):
            raise RuntimeError("down")
    assert asyncio.run(cr.top_recurring_competitors(db=Boom())) == []


def test_audit_candidates_priority_ordering():
    report = {"authority_map": {"skus": [{"authority_hosts": [
        {"competitors_named": ["Low Demand", "High Demand", "Mid Demand"]},
    ]}]}}
    rank = {"high demand": 9, "mid demand": 5, "low demand": 1}
    out = audit_to_candidates(report, category_path="x", priority_rank=rank)
    assert [c["product_name"] for c in out] == ["High Demand", "Mid Demand", "Low Demand"]
    # cap keeps the top-ranked
    capped = audit_to_candidates(report, category_path="x", priority_rank=rank, max_candidates=1)
    assert [c["product_name"] for c in capped] == ["High Demand"]
