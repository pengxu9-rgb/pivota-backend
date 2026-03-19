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
