import os

import pytest


os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")


def _seed_row(**overrides):
    row = {
        "id": "eps_1",
        "external_product_id": "ext_1",
        "market": "US",
        "tool": "*",
        "utm_template": None,
        "partner_type": None,
        "disclosure_text": None,
        "destination_url": "https://example.com/en-us/product/referral-serum",
        "canonical_url": "https://example.com/en-us/product/referral-serum",
        "domain": "example.com",
        "title": "Referral Serum",
        "image_url": "https://cdn.example.com/referral-serum.jpg",
        "price_amount": 25.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "seed_data": {
            "title": "Referral Serum",
            "description": "A helpful daily serum.",
            "image_url": "https://cdn.example.com/referral-serum.jpg",
            "snapshot": {
                "canonical_url": "https://example.com/en-us/product/referral-serum",
                "title": "Referral Serum",
                "description": "A helpful daily serum.",
                "extracted_at": "2026-03-19T00:00:00+00:00",
            },
            "variants": [
                {
                    "variant_id": "v-1",
                    "sku": "SKU-1",
                    "title": "50ml",
                    "price_amount": 25.0,
                    "currency": "USD",
                    "availability": "in_stock",
                }
            ],
        },
        "status": "active",
        "notes": None,
        "created_by_employee_id": None,
        "attached_product_key": None,
        "attached_variant_id": None,
        "created_at": "2026-03-19T00:00:00+00:00",
        "updated_at": "2026-03-19T00:00:00+00:00",
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_fetch_merchant_referral_inventory_collects_attached_and_domain_unattached(monkeypatch):
    from services import external_referral_readiness as module

    attached = _seed_row(id="eps_attached", attached_product_key="merch_1|shopify|prod_1")
    domain_only = _seed_row(id="eps_domain", domain="merchant.example")

    async def fake_fetch_all(query, values=None):
        sql = str(query)
        if "FROM merchant_stores" in sql:
            return [{"domain": "merchant.example"}]
        if "attached_product_key LIKE" in sql:
            return [attached]
        if "attached_product_key IS NULL" in sql:
            return [domain_only]
        return []

    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)

    inventory = await module.fetch_merchant_referral_inventory(merchant_id="merch_1", status="active")

    assert inventory["matched_domains"] == ["merchant.example"]
    assert [row["id"] for row in inventory["rows"]] == ["eps_attached", "eps_domain"]
    assert inventory["matched_via_by_seed"]["eps_attached"] == "attached_product_key"
    assert inventory["matched_via_by_seed"]["eps_domain"] == "merchant_domain"


@pytest.mark.asyncio
async def test_evaluate_external_referral_seed_marks_blockers(monkeypatch):
    from services import external_referral_readiness as module

    stale_row = _seed_row(
        id="eps_stale",
        canonical_url="https://blocked.example/product",
        destination_url="https://blocked.example/product",
        seed_data={
            "title": "Blocked referral",
            "snapshot": {
                "canonical_url": "https://blocked.example/product",
                "title": "Blocked referral",
                "extracted_at": "2026-03-01T00:00:00+00:00",
            },
            "variants": [],
        },
    )

    async def fake_allowed_domains(*, market: str):
        assert market == "US"
        return ["example.com"]

    monkeypatch.setattr(module, "get_allowed_domains_for_market", fake_allowed_domains)

    status = await module.evaluate_external_referral_seed(stale_row, matched_via="agent_api")

    assert status.status == "blocked"
    assert "stale_snapshot" in status.blocker_anomaly_types
    assert "zero_variants" in status.blocker_anomaly_types
    assert "destination_domain_not_allowed" in status.blocker_anomaly_types


@pytest.mark.asyncio
async def test_build_external_referral_summary_counts_statuses(monkeypatch):
    from services import external_referral_readiness as module

    blocked = _seed_row(
        id="eps_blocked",
        seed_data={
            "title": "Blocked referral",
            "snapshot": {
                "canonical_url": "https://example.com/en-us/product/blocked",
                "title": "Blocked referral",
                "description": "Blocked referral",
                "extracted_at": "2026-03-01T00:00:00+00:00",
            },
            "variants": [],
        },
    )
    review = _seed_row(
        id="eps_review",
        image_url=None,
        seed_data={
            "title": "Review referral",
            "description": "Experience the ultimate luxury with Review referral.",
            "snapshot": {
                "canonical_url": "https://example.com/en-us/product/review",
                "title": "Review referral",
                "description": "Experience the ultimate luxury with Review referral.",
                "extracted_at": "2026-03-19T00:00:00+00:00",
            },
            "variants": [
                {
                    "variant_id": "v-1",
                    "sku": "SKU-2",
                    "title": "One size",
                    "price_amount": 22.0,
                    "currency": "USD",
                    "availability": "in_stock",
                }
            ],
        },
    )
    healthy = _seed_row(id="eps_healthy")

    async def fake_inventory(*, merchant_id: str, status: str):
        assert merchant_id == "merch_1"
        assert status == "active"
        return {
            "merchant_id": merchant_id,
            "matched_domains": ["example.com"],
            "attached_rows": [blocked],
            "domain_unattached_rows": [review, healthy],
            "rows": [blocked, review, healthy],
            "matched_via_by_seed": {
                "eps_blocked": "attached_product_key",
                "eps_review": "merchant_domain",
                "eps_healthy": "merchant_domain",
            },
        }

    async def fake_allowed_domains(*, market: str):
        return ["example.com"]

    monkeypatch.setattr(module, "fetch_merchant_referral_inventory", fake_inventory)
    monkeypatch.setattr(module, "get_allowed_domains_for_market", fake_allowed_domains)

    summary = await module.build_external_referral_summary("merch_1")

    assert summary["status"] == "red"
    assert summary["total_active_seeds"] == 3
    assert summary["blocked_seed_count"] == 1
    assert summary["review_seed_count"] == 1
    assert summary["healthy_seed_count"] == 1
    assert any(bucket["issue_type"] == "stale_snapshot" for bucket in summary["issue_buckets"])


@pytest.mark.asyncio
async def test_run_external_referral_refresh_batch_uses_candidate_order(monkeypatch):
    from services import external_referral_readiness as module

    async def fake_candidates(*, limit: int):
        assert limit == 5
        return ["eps_attached", "eps_domain"]

    refreshed = []

    async def fake_refresh(seed_id: str):
        refreshed.append(seed_id)
        return {"status": "success", "seed_id": seed_id}

    monkeypatch.setattr(module, "get_external_referral_refresh_candidate_seed_ids", fake_candidates)

    summary = await module.run_external_referral_refresh_batch(refresh_seed_by_id=fake_refresh, limit=5)

    assert summary["candidate_count"] == 2
    assert summary["refreshed"] == 2
    assert refreshed == ["eps_attached", "eps_domain"]


@pytest.mark.asyncio
async def test_build_external_referral_fleet_summary_classifies_coverage(monkeypatch):
    from services import external_referral_readiness as module

    async def fake_get_all_merchant_onboardings(include_deleted: bool = False):
        assert include_deleted is False
        return [
            {"merchant_id": "merch_covered", "business_name": "Covered Merchant"},
            {"merchant_id": "merch_backfill", "business_name": "Backfill Merchant"},
            {"merchant_id": "merch_sync", "business_name": "Sync Merchant"},
        ]

    async def fake_fetch_catalog_counts():
        return {
            "merch_covered": 740,
            "merch_backfill": 120,
            "merch_sync": 0,
        }

    async def fake_fetch_store_domains():
        return {
            "merch_covered": ["covered.example"],
            "merch_backfill": ["backfill.example"],
        }

    async def fake_build_summary(merchant_id: str):
        if merchant_id == "merch_covered":
            return {
                "merchant_id": merchant_id,
                "status": "green",
                "matched_domains": ["covered.example"],
                "total_active_seeds": 50,
                "attached_seed_count": 50,
                "healthy_seed_count": 50,
                "blocked_seed_count": 0,
                "review_seed_count": 0,
            }
        if merchant_id == "merch_backfill":
            return {
                "merchant_id": merchant_id,
                "status": "red",
                "matched_domains": ["backfill.example"],
                "total_active_seeds": 0,
                "attached_seed_count": 0,
                "healthy_seed_count": 0,
                "blocked_seed_count": 0,
                "review_seed_count": 0,
            }
        return {
            "merchant_id": merchant_id,
            "status": "red",
            "matched_domains": [],
            "total_active_seeds": 0,
            "attached_seed_count": 0,
            "healthy_seed_count": 0,
            "blocked_seed_count": 0,
            "review_seed_count": 0,
        }

    async def fake_backfill_count(merchant_id: str, *, limit: int = 50):
        assert limit == 50
        return 18 if merchant_id == "merch_backfill" else 0

    monkeypatch.setattr(module, "get_all_merchant_onboardings", fake_get_all_merchant_onboardings)
    monkeypatch.setattr(module, "_fetch_catalog_product_counts_by_merchant", fake_fetch_catalog_counts)
    monkeypatch.setattr(module, "_fetch_store_domains_by_merchant", fake_fetch_store_domains)
    monkeypatch.setattr(module, "build_external_referral_summary", fake_build_summary)
    monkeypatch.setattr(module, "estimate_storefront_backfill_candidate_count", fake_backfill_count)

    summary = await module.build_external_referral_fleet_summary()

    assert summary["status"] == "red"
    assert summary["total_merchants"] == 3
    assert summary["merchants_with_attached_referral_seeds"] == 1
    assert summary["merchants_backfill_ready"] == 1
    assert summary["merchants_needing_catalog_sync"] == 1
    assert summary["coverage_rate_pct"] == pytest.approx(33.3, abs=0.1)
    assert summary["actionable_merchants"][0]["merchant_id"] == "merch_backfill"
    assert summary["actionable_merchants"][0]["coverage_state"] == "backfill_ready"
    assert summary["covered_merchants_sample"][0]["merchant_id"] == "merch_covered"
