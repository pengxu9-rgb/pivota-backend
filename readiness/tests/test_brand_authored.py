"""Tests for the store-less brand-authored catalog MVP.

Covers:
  (a) the create endpoint is own-merchant-only (merchant_id from the JWT, never
      the body — cross-tenant is impossible),
  (b) load_brand_authored_merchant_dataset maps catalog_products rows to a
      MerchantSourceDataset,
  (c) scoring emits N/A (no commerce blockers) for brand_authored mode.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.standard_product import StandardProduct
from readiness.models import MerchantSourceDataset
from readiness.scoring import build_merchant_snapshot


# ---------------------------------------------------------------------------
# (c) scoring N/A — pure, no DB
# ---------------------------------------------------------------------------

def _brand_authored_dataset() -> MerchantSourceDataset:
    return MerchantSourceDataset(
        merchant_id="merch_brand_x",
        merchant_name="merch_brand_x",
        evaluation_reference_time="2026-06-23T12:00:00Z",
        merchant_alpha_mode="brand_authored",
        source_of_truth={"catalog": "pivota_brand_catalog.v1"},
        capability_status={},
        merchant_blockers=[],
        merchant_warnings=["brand_authored_mode"],
        merchant_policy={},
        payment_capabilities={},
        merchant_connection={},
        review_diagnostics={"integration_status": "blocked"},
        products=[
            StandardProduct(
                id="ba-glow-serum-abc123",
                platform="brand_authored",
                merchant_id="merch_brand_x",
                title="Glow Serum",
                description="A brightening serum.",
                vendor="Glow Co",
                product_type="serum",
                price=0.0,
                currency="USD",
                inventory_quantity=0,
                image_url="https://img.example/glow.jpg",
                images=["https://img.example/glow.jpg"],
                variants=[],
            )
        ],
    )


_COMMERCE_BLOCKERS = {
    "missing_price",
    "missing_currency",
    "out_of_stock",
    "inventory_stale",
    "missing_shipping_profile",
    "merchant_shipping_policy_missing",
    "merchant_return_policy_missing",
    "merchant_checkout_capability_missing",
    "checkout_stub_missing",
    "merchant_writeback_unavailable",
}


def test_brand_authored_scoring_emits_na_no_commerce_blockers():
    snapshot = build_merchant_snapshot(_brand_authored_dataset(), channel="ucp")
    assert snapshot.merchant_alpha_mode == "brand_authored"

    all_blockers: list[str] = []
    for product in snapshot.products:
        for variant in product.variants:
            for fam in ("price", "inventory", "fulfillment_policy", "checkout_capability", "order_status"):
                status = variant.source_of_truth[fam]
                assert status.status == "not_applicable", (fam, status.status)
                assert status.blockers == [], (fam, status.blockers)
                assert status.warnings == [], (fam, status.warnings)
            all_blockers.extend(variant.checkout.blockers)
            all_blockers.extend(variant.discovery.blockers)

    leaked = _COMMERCE_BLOCKERS.intersection(all_blockers)
    assert not leaked, f"commerce blockers leaked in brand_authored mode: {leaked}"


def test_brand_authored_preserves_content_discovery_family():
    # The catalog/content family is unaffected: a product missing a title/image
    # should still surface its catalog blockers. Here the product is well-formed,
    # so catalog is ready — but it must NOT be forced to N/A.
    snapshot = build_merchant_snapshot(_brand_authored_dataset(), channel="ucp")
    variant = snapshot.products[0].variants[0]
    assert variant.source_of_truth["catalog"].status in {"ready", "warning", "blocked"}
    assert variant.source_of_truth["catalog"].status != "not_applicable"


def test_real_merchant_mode_unaffected_still_blocks_commerce():
    # Same product but real_merchant_alpha mode → commerce blockers MUST still fire.
    ds = _brand_authored_dataset()
    ds.merchant_alpha_mode = "real_merchant_alpha"
    snapshot = build_merchant_snapshot(ds, channel="ucp")
    variant = snapshot.products[0].variants[0]
    assert variant.source_of_truth["price"].status != "not_applicable"
    assert "missing_price" in variant.source_of_truth["price"].blockers


# ---------------------------------------------------------------------------
# (b) load_brand_authored_merchant_dataset maps catalog rows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_brand_authored_dataset_maps_rows(monkeypatch):
    import readiness.sources.brand_authored as src

    rows = [
        {
            "source_product_id": "ba-glow-serum-abc123",
            "title": "Glow Serum",
            "brand": "Glow Co",
            "description": "A brightening serum.",
            "product_type": "serum",
            "category": "skincare",
            "image_url": "https://img.example/glow.jpg",
            "tags": ["brightening"],
            "content_key": "ck_deadbeef",
            "updated_at": None,
        },
        {
            "source_product_id": "ba-rich-cream-def456",
            "title": "Rich Cream",
            "brand": "Glow Co",
            "description": None,
            "product_type": None,
            "category": None,
            "image_url": None,
            "tags": None,
            "content_key": None,
            "updated_at": None,
        },
    ]

    class _FakeDB:
        async def fetch_all(self, *_a, **_k):
            return rows

    # Patch the lazily-imported database + enrichment loader inside the function's modules.
    import db.database as db_database
    import db.product_enrichment as db_enrichment

    monkeypatch.setattr(db_database, "database", _FakeDB())

    async def _fake_enrichments(merchant_id, *, product_keys=None, geo_code="default"):
        return {
            ("brand_authored", "ba-glow-serum-abc123"): {
                "summary_short": "Overlay summary",
                "extra_images": ["https://img.example/glow2.jpg"],
                "topic_tags": ["glow"],
            }
        }

    monkeypatch.setattr(db_enrichment, "get_enrichments_for_products", _fake_enrichments)

    dataset = await src.load_brand_authored_merchant_dataset("merch_brand_x")

    assert dataset.merchant_id == "merch_brand_x"
    assert dataset.merchant_alpha_mode == "brand_authored"
    assert dataset.source_of_truth.get("catalog") == "pivota_brand_catalog.v1"
    assert dataset.merchant_connection == {}
    assert len(dataset.products) == 2

    by_id = {p.id: p for p in dataset.products}
    glow = by_id["ba-glow-serum-abc123"]
    assert glow.platform == "brand_authored"
    assert glow.merchant_id == "merch_brand_x"
    assert glow.title == "Glow Serum"
    assert glow.vendor == "Glow Co"
    # Overlay merged: extra image + topic tag flow through.
    assert "https://img.example/glow2.jpg" in glow.images
    assert "glow" in glow.tags
    # Commerce defaults present (scorer N/As them).
    assert glow.price == 0.0
    assert glow.currency == "USD"


@pytest.mark.asyncio
async def test_count_brand_authored_products(monkeypatch):
    import readiness.sources.brand_authored as src
    import db.database as db_database

    class _FakeDB:
        async def fetch_one(self, *_a, **_k):
            return {"n": 3}

    monkeypatch.setattr(db_database, "database", _FakeDB())
    assert await src.count_brand_authored_products("merch_brand_x") == 3


# ---------------------------------------------------------------------------
# (a) create endpoint is own-merchant-only (cross-tenant impossible)
# ---------------------------------------------------------------------------

def _build_app_with_merchant(monkeypatch, *, merchant_id: str, role: str = "merchant"):
    """Mount the merchant_products router with auth + DB stubbed; record what
    merchant_id the catalog/enrichment upserts are called with."""
    monkeypatch.setenv("ENABLE_STORELESS_BRAND_CATALOG", "1")

    import routes.merchant_products as mp
    from utils.auth import get_current_user

    captured: dict = {}

    import services.brand_authored_intake as intake

    async def _fake_upsert_catalog(fields):
        captured["catalog_merchant_id"] = fields.get("merchant_id")
        captured["catalog_fields"] = fields
        return fields.get("product_key")

    monkeypatch.setattr(intake, "upsert_brand_authored_catalog_row", _fake_upsert_catalog)

    async def _fake_upsert_enrichment(**kwargs):
        captured["enrichment_merchant_id"] = kwargs.get("merchant_id")

    monkeypatch.setattr(mp, "upsert_enrichment", _fake_upsert_enrichment)

    async def _fake_detail(merchant_id, source_product_id):
        captured["detail_merchant_id"] = merchant_id
        return {"merchant_id": merchant_id, "source_product_id": source_product_id}

    monkeypatch.setattr(mp, "_brand_authored_detail", _fake_detail)

    app = FastAPI()
    app.include_router(mp.router)

    async def _override_user():
        return {"role": role, "merchant_id": merchant_id, "email": "m@x.com"}

    app.dependency_overrides[get_current_user] = _override_user
    return app, captured


def test_create_uses_jwt_merchant_not_body(monkeypatch):
    app, captured = _build_app_with_merchant(monkeypatch, merchant_id="merch_TRUE_OWNER")
    client = TestClient(app)

    # The body tries to inject a DIFFERENT merchant_id — it must be ignored.
    resp = client.post(
        "/merchant/products",
        json={
            "title": "Glow Serum",
            "brand": "Glow Co",
            "merchant_id": "merch_ATTACKER",  # extra field, must be ignored
            "description": "A brightening serum.",
        },
    )
    assert resp.status_code == 201, resp.text
    # Catalog + enrichment writes used the JWT merchant, never the body's.
    assert captured["catalog_merchant_id"] == "merch_TRUE_OWNER"
    assert captured.get("enrichment_merchant_id") == "merch_TRUE_OWNER"
    assert captured["catalog_fields"]["platform"] == "brand_authored"
    assert captured["catalog_fields"]["pdp_scope"] == "unverified"
    # The generated product_id is server-side, slug-derived.
    assert resp.json()["product_id"].startswith("ba-glow-serum-")


def test_create_rejects_non_merchant_role(monkeypatch):
    app, _ = _build_app_with_merchant(monkeypatch, merchant_id="m1", role="admin")
    client = TestClient(app)
    resp = client.post("/merchant/products", json={"title": "X"})
    assert resp.status_code == 403


def test_create_404_when_flag_off(monkeypatch):
    # Flag OFF ⇒ endpoint behaves as if it doesn't exist.
    monkeypatch.delenv("ENABLE_STORELESS_BRAND_CATALOG", raising=False)
    import routes.merchant_products as mp
    from utils.auth import get_current_user

    app = FastAPI()
    app.include_router(mp.router)

    async def _override_user():
        return {"role": "merchant", "merchant_id": "m1", "email": "m@x.com"}

    app.dependency_overrides[get_current_user] = _override_user
    client = TestClient(app)
    resp = client.post("/merchant/products", json={"title": "X"})
    assert resp.status_code == 404
