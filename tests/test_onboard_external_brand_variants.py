"""Crawl-onboarded seeds must be born with a sellable variants array and an
explicit extraction timestamp (wrong-brand recall incident 2026-07-11: all
2,151 external_brand_crawl seeds had zero variants, so the zero_variants
runtime blocker dropped the whole cohort from every find_products_multi
response; and with no snapshot.extracted_at the 7d stale_snapshot gate keyed
off updated_at, which any metadata write silently extends).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.onboard_external_brand_from_crawl as onboard  # noqa: E402


class _FakeDB:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, params=None):
        self.calls.append((sql, params or {}))


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(onboard, "database", db)
    return db


def test_build_default_seed_variant_carries_row_price_and_identity():
    variant = onboard.build_default_seed_variant(
        {
            "external_product_id": "acropass_us_8941484015867",
            "title": "Retinol Micronone Patch",
            "price_amount": 26.9,
            "price_currency": "USD",
            "image_url": "https://cdn.example/x.png",
        }
    )
    assert variant["id"] == "acropass_us_8941484015867-default"
    assert variant["sku"] == variant["variant_id"] == variant["id"]
    assert variant["title"] == "Retinol Micronone Patch"
    assert variant["price_amount"] == 26.9
    assert variant["currency"] == "USD"
    assert variant["availability"] == "in_stock"
    assert variant["source"] == "crawl_default_variant_v1"


async def test_upsert_seed_authors_snapshot_variants_and_extracted_at(fake_db):
    await onboard._upsert_seed(
        {
            "external_product_id": "acropass_us_8941484015867",
            "title": "Retinol Micronone Patch",
            "price_amount": 26.9,
            "price_currency": "USD",
            "market": "US",
        },
        seller_ref="merch_obs_deadbeefdeadbeef",
        seed_kind="self",
    )
    _, params = fake_db.calls[0]
    seed_data = json.loads(params["data"])
    snapshot = seed_data["snapshot"]
    variants = snapshot["variants"]
    assert len(variants) == 1
    assert variants[0]["price_amount"] == 26.9
    assert variants[0]["currency"] == "USD"
    # zero_variants gate reads snapshot.variants; stale gate reads
    # snapshot.extracted_at — both must be present at birth.
    assert str(snapshot["extracted_at"]).strip()


async def test_upsert_seed_prefers_cohort_extracted_at(fake_db):
    await onboard._upsert_seed(
        {
            "external_product_id": "acropass_us_1",
            "title": "Patch",
            "extracted_at": "2026-07-04T00:48:51+00:00",
        },
        seller_ref=None,
        seed_kind=None,
    )
    _, params = fake_db.calls[0]
    seed_data = json.loads(params["data"])
    assert seed_data["snapshot"]["extracted_at"] == "2026-07-04T00:48:51+00:00"
