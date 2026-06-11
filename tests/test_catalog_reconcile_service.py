"""Tests for services/catalog_reconcile_service.py — the products_cache →
catalog_products backfill that makes front-end-visible products auditable."""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from services import catalog_reconcile_service as svc


def _cache_row(
    *, platform: str = "shopify", pid: str = "123", product_id: str | None = None,
    title: str = "Good Night Collagen",
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "id": pid,
        "merchant_id": "merch_x",
        "platform": platform,
        "title": title,
        "price": 19.0,
        "currency": "USD",
    }
    if product_id is not None:
        data["product_id"] = product_id
    return {"merchant_id": "merch_x", "platform": platform,
            "platform_product_id": pid, "product_data": data}


# --- _candidate_for_cache_row -------------------------------------------------

def test_candidate_key_matches_ingest_shape():
    cand = svc._candidate_for_cache_row("merch_x", _cache_row(pid="123"))
    assert cand is not None
    key, platform, payload = cand
    # Exactly the shape make_catalog_product_key / ingest_standard_products use.
    assert key == "prod::merch_x::shopify::123"
    assert platform == "shopify"
    assert payload["title"] == "Good Night Collagen"


def test_candidate_key_matches_ingest_even_with_stray_product_id():
    # StandardProduct forces product_id == id (set_product_id validator), and
    # ingest_standard_products keys on `product.product_id or product.id`. So
    # the reconcile must key on `id` too — a stray product_id in the payload
    # must NOT change the key, or the diff would never match what ingest writes.
    cand = svc._candidate_for_cache_row(
        "merch_x", _cache_row(pid="123", product_id="999")
    )
    assert cand is not None and cand[0] == "prod::merch_x::shopify::123"


def test_candidate_none_when_no_platform_or_id():
    row = _cache_row()
    row["platform"] = ""
    row["product_data"]["platform"] = ""
    assert svc._candidate_for_cache_row("merch_x", row) is None


# --- find_missing_catalog_products (DB mocked) --------------------------------

@pytest.mark.asyncio
async def test_find_missing_excludes_already_catalogued(monkeypatch):
    rows = [_cache_row(pid="1"), _cache_row(pid="2"), _cache_row(pid="3")]

    async def fake_fetch_all(query):
        # First call = products_cache select; subsequent = catalog_products IN().
        # Distinguish by a marker we can't read off the query easily, so use a
        # mutable counter via closure.
        fake_fetch_all.calls += 1
        if fake_fetch_all.calls == 1:
            return rows
        # catalog already has product 2 only.
        return [{"product_key": "prod::merch_x::shopify::2"}]
    fake_fetch_all.calls = 0
    monkeypatch.setattr(svc.database, "fetch_all", fake_fetch_all)

    missing = await svc.find_missing_catalog_products(merchant_id="merch_x")
    keys = sorted(k for k, _, _ in missing)
    assert keys == ["prod::merch_x::shopify::1", "prod::merch_x::shopify::3"]


@pytest.mark.asyncio
async def test_find_missing_dedupes_duplicate_cache_rows(monkeypatch):
    rows = [_cache_row(pid="1"), _cache_row(pid="1")]  # dup

    async def fake_fetch_all(query):
        fake_fetch_all.calls += 1
        return rows if fake_fetch_all.calls == 1 else []
    fake_fetch_all.calls = 0
    monkeypatch.setattr(svc.database, "fetch_all", fake_fetch_all)

    missing = await svc.find_missing_catalog_products(merchant_id="merch_x")
    assert [k for k, _, _ in missing] == ["prod::merch_x::shopify::1"]


# --- reconcile_catalog_from_cache (orchestration) -----------------------------

@pytest.mark.asyncio
async def test_reconcile_dry_run_does_not_ingest(monkeypatch):
    async def fake_missing(*, merchant_id, platform=None):
        return [("prod::m::shopify::1", "shopify", {"id": "1"})]
    monkeypatch.setattr(svc, "find_missing_catalog_products", fake_missing)

    called = {"n": 0}
    async def fake_ingest(**kwargs):
        called["n"] += 1
        return {"products_ingested": len(kwargs["product_payloads"])}
    monkeypatch.setattr(svc, "ingest_standard_products", fake_ingest)

    report = await svc.reconcile_catalog_from_cache(merchant_id="m", dry_run=True)
    assert report["missing_count"] == 1
    assert report["ingested"] == 0
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_reconcile_groups_by_platform_and_respects_limit(monkeypatch):
    missing = [
        ("prod::m::shopify::1", "shopify", {"id": "1"}),
        ("prod::m::shopify::2", "shopify", {"id": "2"}),
        ("prod::m::wix::3", "wix", {"id": "3"}),
    ]
    async def fake_missing(*, merchant_id, platform=None):
        return missing
    monkeypatch.setattr(svc, "find_missing_catalog_products", fake_missing)

    ingest_calls: List[Dict[str, Any]] = []
    async def fake_ingest(**kwargs):
        ingest_calls.append(kwargs)
        return {"products_ingested": len(kwargs["product_payloads"])}
    monkeypatch.setattr(svc, "ingest_standard_products", fake_ingest)

    # limit=2 → only first two candidates processed (both shopify).
    report = await svc.reconcile_catalog_from_cache(merchant_id="m", limit=2)
    assert report["missing_count"] == 3
    assert report["truncated"] is True
    assert report["ingested"] == 2
    assert len(ingest_calls) == 1  # one platform group (shopify)
    assert {c["platform"] for c in ingest_calls} == {"shopify"}


@pytest.mark.asyncio
async def test_reconcile_noop_when_nothing_missing(monkeypatch):
    async def fake_missing(*, merchant_id, platform=None):
        return []
    monkeypatch.setattr(svc, "find_missing_catalog_products", fake_missing)
    async def fake_ingest(**kwargs):  # pragma: no cover
        raise AssertionError("must not ingest when nothing missing")
    monkeypatch.setattr(svc, "ingest_standard_products", fake_ingest)

    report = await svc.reconcile_catalog_from_cache(merchant_id="m")
    assert report["missing_count"] == 0 and report["ingested"] == 0
