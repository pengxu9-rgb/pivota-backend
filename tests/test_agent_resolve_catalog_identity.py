"""`/agent/v1/products/resolve` must see identity that lives in the catalog, not
only in products_cache.

THE DEFECT, measured on prod 2026-09-16. `products_cache` is a merchant-sync
cache and the external_referral cohort is never written to it: ZERO rows for
merchant `merch_obs_88382424262f3e0f`, whose `catalog_skus` rows carry the real
Shopify variant ids. So resolve answered NO_CANDIDATES for every referral row,
for every caller, by construction -- the identity existed the whole time, in a
table this endpoint never read. A partner reported it after resolving the same
variant id successfully against the merchant's own storefront.

These tests drive the endpoint with products_cache deliberately EMPTY, which is
what prod looks like for this cohort.
"""

from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from main import app

MERCHANT = "merch_obs_88382424262f3e0f"
VARIANT_ID = "50856826536257"
MERCHANT_SKU = "32168999"
SOURCE_PRODUCT_ID = "jungsaemmool-lip-pression-metal-serum-gloss"
SIGNATURE = "sig_b978d922cbcb2f0c04ccf2e1a1e3b558"
PRODUCT_KEY = "ext:jungsaemmool-lip-pression-metal-serum-gloss::66a3c8a4"
TITLE = "LIP-PRESSION Metal Serum Gloss"

# Same anonymous-but-keyed channel the sibling resolve suite uses.
HEADERS = {"X-API-Key": "test-api-key"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _identity_for_projection(q: str) -> Optional[str]:
    """Return whatever identity column the query actually PROJECTED.

    This matters more than it looks. A stub that hands back a fixed
    `platform_product_id` no matter what the SELECT asked for cannot observe the
    id shape at all -- and the first version of this file did exactly that, so a
    mutant that projected `pivota_signature_id` instead of `source_product_id`
    passed every test. The stub has to model the one thing under test: a database
    returns the column you asked it for.
    """
    if "cp.source_product_id AS platform_product_id" in q:
        return SOURCE_PRODUCT_ID
    if "cp.pivota_signature_id AS platform_product_id" in q:
        return SIGNATURE
    if "cp.product_key AS platform_product_id" in q:
        return PRODUCT_KEY
    if "cp.content_key AS platform_product_id" in q:
        return "ck_74c837fde3c6e78aad504d05f2806910"
    return None


def _row_for(q: str) -> Dict[str, Any]:
    return {
        "platform_product_id": _identity_for_projection(q),
        "merchant_id": MERCHANT,
        "platform": "external_seed",
        "title": TITLE,
    }


def _pgm_row() -> Dict[str, Any]:
    # product_group_members stores source_product_id -- verified on prod, where all
    # 15,588 rows carry that shape and none carry a sig_/ext:/ck_ one.
    return {
        "product_group_id": "pg_74c837fde3c6e78aad504d05f2806910",
        "merchant_id": MERCHANT,
        "platform": "external_seed",
        "platform_product_id": SOURCE_PRODUCT_ID,
        "is_primary": True,
    }


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sku_rows: Optional[List[Dict[str, Any]]] = None,
    product_rows: Optional[List[Dict[str, Any]]] = None,
    pgm_rows: Optional[List[Dict[str, Any]]] = None,
    cache_rows: Optional[List[Dict[str, Any]]] = None,
    catalog_raises: bool = False,
    forbid_search: bool = True,
) -> Dict[str, int]:
    """Wire a fake DB. products_cache is EMPTY unless a caller says otherwise."""
    import routes.agent_api as agent_api

    calls = {"catalog_skus": 0, "catalog_products": 0, "products_cache": 0, "search": 0}

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM products_cache" in q:
            calls["products_cache"] += 1
            return list(cache_rows or [])
        if "FROM catalog_skus cs" in q:
            calls["catalog_skus"] += 1
            if catalog_raises:
                raise RuntimeError("catalog_skus unavailable")
            return [_row_for(q) for _ in (sku_rows or [])]
        if "FROM catalog_products cp" in q:
            calls["catalog_products"] += 1
            if catalog_raises:
                raise RuntimeError("catalog_products unavailable")
            return [_row_for(q) for _ in (product_rows or [])]
        if "FROM product_group_members" in q:
            # Prod stores source_product_id here and nothing else: all 15,588 rows
            # carry that shape. A candidate keyed on any other shape MISSES, which
            # is the whole reason the lane projects source_product_id.
            pids = set((values or {}).get("pids") or [])
            return [r for r in (pgm_rows or []) if r["platform_product_id"] in pids]
        return []

    async def fake_search(**kwargs):
        calls["search"] += 1
        if forbid_search:
            raise AssertionError("exact catalog identity must not fall through to search")
        return {"products": []}

    monkeypatch.setattr(agent_api.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api, "agent_search_products", fake_search)
    return calls


def test_shopify_variant_id_resolves_through_catalog_skus(client, monkeypatch):
    """The reported defect: products_cache empty, identity in catalog_skus."""
    calls = _install(monkeypatch, sku_rows=[{}], pgm_rows=[_pgm_row()])

    res = client.get(f"/agent/v1/products/resolve?sku_id={VARIANT_ID}&limit=10", headers=HEADERS)
    assert res.status_code == 200
    body = res.json()

    assert body["resolved"] is True, body
    assert body["reason_code"] == "OK"
    assert body["candidate_count"] >= 1
    top = body["candidates"][0]
    assert top["product_id"] == SOURCE_PRODUCT_ID
    assert top["merchant_id"] == MERCHANT
    assert top["source"] == "catalog_identity_exact"
    assert calls["catalog_skus"] == 1
    assert calls["search"] == 0


def test_merchant_sku_resolves_through_the_same_lane(client, monkeypatch):
    _install(monkeypatch, sku_rows=[{}], pgm_rows=[_pgm_row()])
    res = client.get(f"/agent/v1/products/resolve?sku_id={MERCHANT_SKU}&limit=10", headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["resolved"] is True


def test_signature_resolves_through_the_catalog_products_lane(client, monkeypatch):
    """A caller blocked on product_id is as stuck as one blocked on sku_id."""
    calls = _install(
        monkeypatch,
        product_rows=[{}],
        pgm_rows=[_pgm_row()],
    )
    res = client.get(f"/agent/v1/products/resolve?product_id={SIGNATURE}&limit=10", headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["resolved"] is True, body
    assert body["candidates"][0]["product_id"] == SOURCE_PRODUCT_ID
    assert calls["catalog_products"] == 1


def test_the_lane_emits_source_product_id_so_canonical_ref_survives(client, monkeypatch):
    """The emitted id shape is load-bearing, not cosmetic.

    The canonical lookup joins product_group_members.platform_product_id, which
    stores source_product_id. Emitting the signature or the product_key would
    resolve the candidate and then silently lose canonical_ref -- a caller would
    get a product back with no canonical identity and no error saying why.
    """
    _install(monkeypatch, sku_rows=[{}], pgm_rows=[_pgm_row()])
    body = client.get(f"/agent/v1/products/resolve?sku_id={VARIANT_ID}&limit=10", headers=HEADERS).json()

    assert body["canonical_ref"] == "pg:pg_74c837fde3c6e78aad504d05f2806910"
    assert body["canonical_identity_status"] == "resolved"
    assert body["canonical_product"]["product_id"] == SOURCE_PRODUCT_ID

    # The control that makes the assertion above mean something: a signature-shaped
    # id does NOT join product_group_members, so if the lane emitted one this test
    # would still pass on `resolved` but lose the ref.
    assert SIGNATURE != SOURCE_PRODUCT_ID
    assert not SOURCE_PRODUCT_ID.startswith("sig_")


def test_unknown_identity_still_reports_no_candidates(client, monkeypatch):
    """CONTROL: the lane must not resolve everything it is asked about.

    Without this, every test above would also pass if the lane returned a
    candidate unconditionally.
    """
    _install(monkeypatch, sku_rows=[], product_rows=[], forbid_search=False)
    body = client.get("/agent/v1/products/resolve?sku_id=00000000000000&limit=10", headers=HEADERS).json()

    assert body["resolved"] is False
    assert body["reason_code"] == "NO_CANDIDATES"
    assert body["candidate_count"] == 0


def test_products_cache_hit_keeps_the_fast_path_and_skips_the_catalog_lane(client, monkeypatch):
    """The cache lane is still first. The catalog lane is a fallback, not a tax."""
    cache_row = {
        "merchant_id": MERCHANT,
        "platform": "shopify",
        "platform_product_id": "9886499864904",
        "product_data": {"id": "9886499864904", "title": "Cached Product"},
    }
    calls = _install(monkeypatch, cache_rows=[cache_row], pgm_rows=[])

    body = client.get("/agent/v1/products/resolve?product_id=9886499864904&limit=10", headers=HEADERS).json()

    assert body["resolved"] is True
    assert body["candidates"][0]["source"].startswith("products_cache")
    assert calls["catalog_skus"] == 0
    assert calls["catalog_products"] == 0


def test_a_failing_catalog_lane_never_costs_the_turn(client, monkeypatch):
    """A raise in the new lane must degrade to the existing search fallback, the
    same way a products_cache failure already does -- not 500 the request."""
    calls = _install(monkeypatch, catalog_raises=True, forbid_search=False)

    res = client.get(f"/agent/v1/products/resolve?sku_id={VARIANT_ID}&limit=10", headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["resolved"] is False
    assert calls["search"] >= 1, "search fallback must still run after a catalog lane error"
    # The degradation is OBSERVABLE, not silent: the lane reports its own failure
    # in metadata rather than being indistinguishable from "found nothing".
    meta = body.get("metadata", {})
    sources = {s["source"]: s for s in meta.get("sources", [])}
    assert sources["catalog_identity_exact"]["status"] == "error"
    assert "catalog_identity_exact" in meta.get("failure_breakdown", {})
