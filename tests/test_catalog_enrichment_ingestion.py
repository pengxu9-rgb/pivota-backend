"""Pure-function tests for services/catalog_enrichment_agent/ingestion.py.

No DB required — exercises the row-building logic, idempotency keys, and
edge cases that the Stage 3 ingestion runner will execute against the DB.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.catalog_enrichment_agent.ingestion import (  # noqa: E402
    AGENT_VERSION,
    canonical_product_name,
    derive_product_key,
    derive_seed_id,
    ingest_validated_jsonl,
    ingest_validated_record,
)


def _record(*, brand: str = "MAC", product_name: str = "Ruby Woo Matte Lipstick", offers=None, **extras):
    return {
        "pdp": {
            "brand": brand,
            "product_name": product_name,
            "category_path": "beauty/makeup/lip/lipstick",
            "attribute_summary": "matte, retro red",
            **extras,
        },
        "offers": offers if offers is not None else [
            {
                "merchant_inferred": "MAC",
                "destination_url": "https://maccosmetics.com/products/ruby-woo",
                "canonical_url": "https://maccosmetics.com/products/ruby-woo",
                "image_url": "https://maccosmetics.com/img/ruby-woo.jpg",
                "price": 21.0,
                "in_stock": True,
                "validated_at": "2026-05-06T10:00:00Z",
            },
        ],
    }


def test_canonical_product_name_normalizes_punctuation_and_case():
    # MAC → "mac"; whitespace/case/punct collapse to single hyphens.
    assert canonical_product_name("MAC", "Ruby Woo Matte Lipstick") == "mac-ruby-woo-matte-lipstick"
    assert canonical_product_name("MAC", "ruby   woo!  matte  lipstick") == "mac-ruby-woo-matte-lipstick"
    # Periods between letters split into separate tokens (M.A.C. → "m-a-c").
    # Same key is produced consistently — downstream is stable as long as
    # ingest sees the same brand spelling each run.
    assert canonical_product_name("M.A.C.", "Ruby Woo Matte Lipstick") == "m-a-c-ruby-woo-matte-lipstick"


def test_canonical_product_name_handles_blank():
    assert canonical_product_name(None, None) == "unknown"
    assert canonical_product_name("", "") == "unknown"


def test_derive_product_key_is_deterministic():
    a = derive_product_key("MAC", "Ruby Woo Matte Lipstick")
    b = derive_product_key("MAC", "Ruby Woo Matte Lipstick")
    assert a == b
    assert a.startswith("ext:mac-ruby-woo-matte-lipstick")
    assert "::" in a


def test_derive_product_key_differs_by_content():
    a = derive_product_key("MAC", "Ruby Woo Matte Lipstick")
    b = derive_product_key("MAC", "Velvet Teddy Matte Lipstick")
    assert a != b


def test_derive_product_key_under_255_chars():
    a = derive_product_key("X" * 300, "Y" * 300)
    assert len(a) <= 255


def test_derive_seed_id_is_deterministic():
    pk = derive_product_key("MAC", "Ruby Woo")
    a = derive_seed_id(pk, "https://maccosmetics.com/p/ruby-woo")
    b = derive_seed_id(pk, "https://maccosmetics.com/p/ruby-woo")
    assert a == b
    assert a.startswith(f"seed:{AGENT_VERSION}:")


def test_derive_seed_id_differs_by_url():
    pk = derive_product_key("MAC", "Ruby Woo")
    a = derive_seed_id(pk, "https://maccosmetics.com/p/ruby-woo")
    b = derive_seed_id(pk, "https://sephora.com/p/mac-ruby-woo")
    assert a != b


def test_ingest_record_builds_pdp_and_seed_rows():
    result = ingest_validated_record(_record(), source_jsonl="data/x.jsonl")
    assert result is not None
    pdp = result["pdp"]
    seeds = result["seeds"]
    assert pdp["product_key"].startswith("ext:mac-ruby-woo-matte-lipstick")
    assert pdp["brand"] == "MAC"
    assert pdp["title"] == "Ruby Woo Matte Lipstick"
    assert pdp["category_path"] == "beauty/makeup/lip/lipstick"
    assert pdp["category"] == "lipstick"
    assert pdp["catalog_track"] == "external_referral"
    assert pdp["category_label_source"] == "enrichment_agent_v1"
    assert 0.0 <= pdp["category_confidence"] <= 1.0
    assert pdp["canonical_url"] == "https://maccosmetics.com/products/ruby-woo"
    payload = json.loads(pdp["product_payload"])
    assert payload["enrichment_meta"]["agent_version"] == AGENT_VERSION
    assert payload["enrichment_meta"]["source_jsonl"] == "data/x.jsonl"
    assert len(seeds) == 1
    seed = seeds[0]
    assert seed["attached_product_key"] == pdp["product_key"]
    assert seed["status"] == "active"
    assert seed["tool"] == AGENT_VERSION
    assert seed["domain"] == "maccosmetics.com"
    assert seed["price_amount"] == 21.0


def test_ingest_record_handles_multiple_offers():
    record = _record(offers=[
        {
            "merchant_inferred": "MAC",
            "canonical_url": "https://maccosmetics.com/products/ruby-woo",
            "destination_url": "https://maccosmetics.com/products/ruby-woo?ref=ig",
            "image_url": "https://maccosmetics.com/img/x.jpg",
            "price": 21.0,
            "in_stock": True,
            "validated_at": "2026-05-06T10:00:00Z",
        },
        {
            "merchant_inferred": "Sephora",
            "canonical_url": "https://sephora.com/products/mac-ruby-woo",
            "destination_url": "https://sephora.com/products/mac-ruby-woo",
            "image_url": "https://sephora.com/img/y.jpg",
            "price": 22.0,
            "in_stock": True,
            "validated_at": "2026-05-06T10:01:00Z",
        },
    ])
    result = ingest_validated_record(record)
    assert result is not None
    assert len(result["seeds"]) == 2
    domains = sorted(s["domain"] for s in result["seeds"])
    assert domains == ["maccosmetics.com", "sephora.com"]


def test_ingest_record_dedupes_offers_by_destination_url():
    record = _record(offers=[
        {
            "merchant_inferred": "MAC",
            "canonical_url": "https://maccosmetics.com/products/ruby-woo",
            "destination_url": "https://maccosmetics.com/products/ruby-woo",
            "image_url": "",
            "price": 21.0,
            "in_stock": True,
        },
        {
            "merchant_inferred": "MAC",
            "canonical_url": "https://maccosmetics.com/products/ruby-woo",
            "destination_url": "https://maccosmetics.com/products/ruby-woo",
            "image_url": "",
            "price": 22.0,
            "in_stock": False,
        },
    ])
    result = ingest_validated_record(record)
    assert result is not None
    assert len(result["seeds"]) == 1


def test_ingest_record_returns_none_for_missing_pdp():
    assert ingest_validated_record({"pdp": {}, "offers": []}) is None
    assert ingest_validated_record({"pdp": None, "offers": []}) is None
    assert ingest_validated_record({}) is None


def test_ingest_record_returns_none_for_no_offers():
    record = _record(offers=[])
    assert ingest_validated_record(record) is None


def test_ingest_record_returns_none_when_pdp_missing_required_fields():
    bad = _record()
    bad["pdp"]["brand"] = ""
    assert ingest_validated_record(bad) is None
    bad2 = _record()
    bad2["pdp"]["product_name"] = ""
    assert ingest_validated_record(bad2) is None


def test_ingest_record_skips_offer_with_no_url():
    record = _record(offers=[
        {"merchant_inferred": "MAC", "destination_url": "", "canonical_url": ""},
        {
            "merchant_inferred": "MAC",
            "destination_url": "https://maccosmetics.com/p/ruby-woo",
            "canonical_url": "https://maccosmetics.com/p/ruby-woo",
            "image_url": "",
            "price": 21.0,
        },
    ])
    result = ingest_validated_record(record)
    assert result is not None
    assert len(result["seeds"]) == 1


def test_ingest_jsonl_dedupes_pdps_across_records():
    rec1 = _record(offers=[{
        "merchant_inferred": "MAC",
        "canonical_url": "https://maccosmetics.com/p/ruby-woo",
        "destination_url": "https://maccosmetics.com/p/ruby-woo",
        "image_url": "", "price": 21.0, "in_stock": True,
    }])
    rec2 = _record(offers=[{
        "merchant_inferred": "Sephora",
        "canonical_url": "https://sephora.com/p/mac-ruby-woo",
        "destination_url": "https://sephora.com/p/mac-ruby-woo",
        "image_url": "", "price": 22.0, "in_stock": True,
    }])
    pdps, seeds, skipped = ingest_validated_jsonl([rec1, rec2])
    assert len(pdps) == 1
    assert len(seeds) == 2
    assert skipped == 0
    assert seeds[0]["attached_product_key"] == pdps[0]["product_key"]
    assert seeds[1]["attached_product_key"] == pdps[0]["product_key"]


def test_ingest_jsonl_counts_skipped():
    pdps, seeds, skipped = ingest_validated_jsonl([
        _record(),
        {"pdp": {}, "offers": []},
        _record(brand="Charlotte Tilbury", product_name="Pillow Talk"),
    ])
    assert len(pdps) == 2
    assert skipped == 1


def test_ingest_jsonl_empty_input():
    pdps, seeds, skipped = ingest_validated_jsonl([])
    assert pdps == []
    assert seeds == []
    assert skipped == 0
