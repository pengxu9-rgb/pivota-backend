"""Portal contract fix: /api/audits resolves `platform:source_product_id`
composites (sent by the merchant AI-Readiness page as `sku_keys`) to
product_keys — closes the self-serve launch 422.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import routes.audit_runs_routes as ar


async def _resolve(monkeypatch, *, rows, refs, owned=None):
    async def fake_fetch_all(query, *a, **k):
        return rows

    async def fake_missing(*, merchant_id, product_keys):
        owned_set = set(owned or [])
        return [k for k in product_keys if k not in owned_set]

    monkeypatch.setattr(ar.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(ar, "_missing_product_keys_for_merchant", fake_missing)
    return await ar._resolve_refs_to_product_keys(merchant_id="m1", refs=refs)


async def test_resolves_composites_to_product_keys(monkeypatch):
    rows = [
        {"product_key": "pk_a", "platform": "shopify", "source_product_id": "111"},
        {"product_key": "pk_b", "platform": "shopify", "source_product_id": "222"},
    ]
    out = await _resolve(monkeypatch, rows=rows, refs=["shopify:111", "shopify:222"])
    assert out == ["pk_a", "pk_b"]


async def test_missing_composite_raises_404(monkeypatch):
    from fastapi import HTTPException

    rows = [{"product_key": "pk_a", "platform": "shopify", "source_product_id": "111"}]
    with pytest.raises(HTTPException) as ei:
        await _resolve(monkeypatch, rows=rows, refs=["shopify:111", "shopify:999"])
    assert ei.value.status_code == 404
    assert "shopify:999" in str(ei.value.detail)


async def test_bare_product_keys_pass_through(monkeypatch):
    # No ':' -> treated as already-resolved product_keys, validated for ownership.
    out = await _resolve(monkeypatch, rows=[], refs=["pk_x", "pk_y"], owned=["pk_x", "pk_y"])
    assert out == ["pk_x", "pk_y"]


async def test_double_colon_sku_key_treated_as_product_key(monkeypatch):
    # A minted `<product_key>::v::<variant>` is NOT a composite; it falls to the
    # bare path and 404s if unowned (no caller sends these here today).
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await _resolve(monkeypatch, rows=[], refs=["pk::v::1"], owned=[])
    assert ei.value.status_code == 404


async def test_dedupes_resolved_keys(monkeypatch):
    rows = [
        {"product_key": "pk_a", "platform": "shopify", "source_product_id": "111"},
        {"product_key": "pk_a", "platform": "shopify", "source_product_id": "222"},
    ]
    out = await _resolve(monkeypatch, rows=rows, refs=["shopify:111", "shopify:222"])
    assert out == ["pk_a"]  # same product -> deduped


def test_model_requires_products_or_skus():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ar.CreateAuditRequest(merchant_id="m1")  # neither -> invalid

    req = ar.CreateAuditRequest(merchant_id="m1", sku_keys=["shopify:111"])
    assert req.sku_keys == ["shopify:111"]
    assert req.product_keys == []

    req2 = ar.CreateAuditRequest(merchant_id="m1", product_keys=["pk_a"])  # back-compat
    assert req2.product_keys == ["pk_a"]
