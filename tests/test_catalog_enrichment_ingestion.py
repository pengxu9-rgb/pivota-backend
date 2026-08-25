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
    MERCHANT_ID_PREFIX,
    SKU_SUFFIX,
    canonical_product_name,
    derive_merchant_id,
    derive_offer_id,
    derive_product_key,
    derive_seed_id,
    derive_sku_key,
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
            # W2: the seller of record derives from the PDP's own source_domain
            # (real enrichment payloads always carry it — the crawler knows
            # where it fetched). Overridable via **extras.
            "source_domain": "maccosmetics.com",
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
    # A color cosmetic is not a contract category_kind — stays None, never
    # text-guessed into skincare.
    assert pdp["category_kind"] is None
    assert pdp["catalog_track"] == "external_referral"
    assert pdp["category_label_source"] == "enrichment_agent_v1"
    assert 0.0 <= pdp["category_confidence"] <= 1.0
    assert pdp["canonical_url"] == "https://maccosmetics.com/products/ruby-woo"
    assert pdp["pivota_signature_id"].startswith("sig_")
    assert pdp["pivota_canonical_url"].endswith(pdp["pivota_signature_id"])
    assert pdp["pivota_signature_minted_at"] is not None
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


def test_ingest_record_sets_category_kind_from_path():
    # Skincare path → durable skincare kind (drives claim-safety + serving gate).
    skin = ingest_validated_record(
        _record(product_name="Advanced Snail Mucin Serum",
                category_path="beauty/skincare/serum")
    )
    assert skin is not None
    assert skin["pdp"]["category_kind"] == "skincare"

    # Haircare path → haircare.
    hair = ingest_validated_record(
        _record(product_name="Repair Hair Mask", category_path="beauty/haircare/mask")
    )
    assert hair["pdp"]["category_kind"] == "haircare"

    # Ingestible inner-beauty (no discriminating path) → supplement via
    # conservative dosage-form + ingestible-active detection.
    supp = ingest_validated_record(
        _record(product_name="Low Molecular Collagen Sticks",
                category_path="beauty", attribute_summary="collagen powder, 30 sticks")
    )
    assert supp["pdp"]["category_kind"] == "supplement"


def test_runner_persists_canonical_signature_fields():
    # The upsert SQL lives in the shared FK-order executor (apply._PDP_UPSERT_SQL),
    # which both the CLI and the programmatic runner route through.
    from services.catalog_enrichment_agent.apply import _PDP_UPSERT_SQL

    assert "pivota_signature_id, pivota_canonical_url, pivota_signature_minted_at" in _PDP_UPSERT_SQL
    assert ":pivota_signature_id, :pivota_canonical_url, :pivota_signature_minted_at" in _PDP_UPSERT_SQL
    assert (
        "pivota_signature_id = COALESCE(catalog_products.pivota_signature_id, "
        "EXCLUDED.pivota_signature_id)"
    ) in _PDP_UPSERT_SQL

    runner_source = (
        Path(__file__).resolve().parents[1] / "scripts" / "run_catalog_enrichment.py"
    ).read_text()
    assert "from services.catalog_enrichment_agent.apply import apply_ingest_plan" in runner_source


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
    out = ingest_validated_jsonl([rec1, rec2])
    assert len(out["pdps"]) == 1
    assert len(out["skus"]) == 1
    assert len(out["seeds"]) == 2
    assert len(out["offers"]) == 2
    # W2: the offer-space merchants (agent_seed::mac, agent_seed::sephora) plus
    # ONE observed seller of record for the product row (deduped across both
    # records — same brand+domain resolves the same identity).
    offer_space = [m for m in out["merchants"] if not m.get("_ensure_only")]
    sellers_of_record = [m for m in out["merchants"] if m.get("_ensure_only")]
    assert len(offer_space) == 2
    assert len(sellers_of_record) == 1
    assert sellers_of_record[0]["merchant_id"] == out["pdps"][0]["merchant_id"]
    assert out["skipped"] == 0
    assert out["seeds"][0]["attached_product_key"] == out["pdps"][0]["product_key"]
    assert out["offers"][0]["product_key"] == out["pdps"][0]["product_key"]
    assert out["offers"][0]["sku_key"] == out["skus"][0]["sku_key"]


def test_ingest_jsonl_counts_skipped():
    out = ingest_validated_jsonl([
        _record(),
        {"pdp": {}, "offers": []},
        _record(brand="Charlotte Tilbury", product_name="Pillow Talk"),
    ])
    assert len(out["pdps"]) == 2
    assert out["skipped"] == 1


def test_ingest_jsonl_empty_input():
    out = ingest_validated_jsonl([])
    assert out["pdps"] == []
    assert out["skus"] == []
    assert out["seeds"] == []
    assert out["offers"] == []
    assert out["merchants"] == []
    assert out["skipped"] == 0


# --- Phase 7a builders: SKU + offer + merchant ---


def test_derive_sku_key_appends_canonical_suffix():
    pk = derive_product_key("MAC", "Ruby Woo")
    sku = derive_sku_key(pk)
    assert sku == f"{pk}{SKU_SUFFIX}"
    assert sku.endswith("::canonical")


def test_derive_offer_id_is_deterministic_per_destination():
    pk = derive_product_key("MAC", "Ruby Woo")
    sku = derive_sku_key(pk)
    a = derive_offer_id(pk, sku, "https://maccosmetics.com/p/ruby-woo")
    b = derive_offer_id(pk, sku, "https://maccosmetics.com/p/ruby-woo")
    c = derive_offer_id(pk, sku, "https://sephora.com/p/mac-ruby-woo")
    assert a == b
    assert a != c
    assert a.startswith(f"offer:{AGENT_VERSION}:")


def test_derive_merchant_id_slugifies():
    assert derive_merchant_id("Sephora", None) == f"{MERCHANT_ID_PREFIX}sephora"
    assert derive_merchant_id("MAC Cosmetics", None) == f"{MERCHANT_ID_PREFIX}mac-cosmetics"
    assert derive_merchant_id(None, "ulta.com") == f"{MERCHANT_ID_PREFIX}ulta-com"
    assert derive_merchant_id(None, None) == f"{MERCHANT_ID_PREFIX}unknown"


def test_derive_merchant_id_collapses_same_retailer_to_one_id():
    """Two offers from the same retailer must share one merchant_id —
    that's what makes Phase 6 seller_count meaningful."""
    a = derive_merchant_id("Sephora", "sephora.com")
    b = derive_merchant_id("Sephora", "sephora.com")
    assert a == b


def test_ingest_record_emits_complete_canonical_chain():
    """Phase 7a contract: one record yields PDP + SKU + N merchants +
    N offers + N seeds, with FK-consistent keys throughout."""
    result = ingest_validated_record(_record())
    assert result is not None
    pdp = result["pdp"]
    sku = result["sku"]
    merchants = result["merchants"]
    offers = result["offers"]
    seeds = result["seeds"]

    assert sku["product_key"] == pdp["product_key"]
    assert sku["sku_key"] == f"{pdp['product_key']}::canonical"
    assert sku["title"] == pdp["title"]

    assert len(offers) == 1
    offer = offers[0]
    assert offer["product_key"] == pdp["product_key"]
    assert offer["sku_key"] == sku["sku_key"]
    assert offer["merchant_id"].startswith(MERCHANT_ID_PREFIX)
    assert offer["catalog_track"] == "external_referral"
    assert offer["availability"] == "in_stock"
    assert offer["currency"] == "USD"
    assert offer["list_price"] == 21.0

    assert any(m["merchant_id"] == offer["merchant_id"] for m in merchants)

    assert len(seeds) == 1
    assert seeds[0]["attached_product_key"] == pdp["product_key"]


@pytest.mark.parametrize(
    ("pdp_fields", "expected"),
    [
        ({"gtin": "1234567890123"}, "1234567890123"),
        ({"upc": "123456789012"}, "123456789012"),
        ({"gtin": "12345678"}, "12345678"),
        ({"barcode": "0-12345-67890-5"}, "012345678905"),
    ],
)
def test_ingest_record_captures_strong_identifier_into_sku_barcode(pdp_fields, expected):
    result = ingest_validated_record(_record(**pdp_fields))

    assert result is not None
    assert result["sku"]["barcode"] == expected
    assert "no_strong_identifier" not in result["audit_reasons"]


def test_ingest_record_skips_missing_and_garbage_identifier_without_rejection():
    missing = ingest_validated_record(_record())
    assert missing is not None
    assert missing["sku"]["barcode"] is None
    assert missing["audit_reasons"] == {"no_strong_identifier": 1}

    garbage = ingest_validated_record(_record(gtin="N/A", barcode="0"))
    assert garbage is not None
    assert garbage["sku"]["barcode"] is None
    assert garbage["audit_reasons"] == {"no_strong_identifier": 1}


def test_ingest_record_captures_mpn_as_last_fallback_and_marks_audit():
    result = ingest_validated_record(_record(mpn=" MPN-ABC-123 "))

    assert result is not None
    assert result["sku"]["barcode"] == "MPN-ABC-123"
    payload = json.loads(result["sku"]["sku_payload"])
    assert payload["strong_identifier_kind"] == "mpn"
    assert result["audit_reasons"] == {"mpn_captured_as_barcode": 1}


def test_ingest_record_collapses_two_mac_offers_into_one_merchant():
    """Two offers from the same retailer → one merchant upsert, two
    offer rows. Phase 6 seller_count stays accurate."""
    record = _record(offers=[
        {
            "merchant_inferred": "MAC",
            "canonical_url": "https://maccosmetics.com/p/ruby-woo",
            "destination_url": "https://maccosmetics.com/p/ruby-woo",
            "image_url": "", "price": 21.0, "in_stock": True,
        },
        {
            "merchant_inferred": "MAC",
            "canonical_url": "https://maccosmetics.com/p/ruby-woo-mini",
            "destination_url": "https://maccosmetics.com/p/ruby-woo-mini",
            "image_url": "", "price": 12.0, "in_stock": True,
        },
    ])
    result = ingest_validated_record(record)
    assert result is not None
    assert len(result["offers"]) == 2
    # W2: one offer-space merchant plus the product's observed seller of record.
    offer_space = [m for m in result["merchants"] if not m.get("_ensure_only")]
    assert len(offer_space) == 1
    assert offer_space[0]["merchant_name"] == "MAC"
    sellers_of_record = [m for m in result["merchants"] if m.get("_ensure_only")]
    assert len(sellers_of_record) == 1
    assert sellers_of_record[0]["merchant_id"] == result["pdp"]["merchant_id"]


def test_ingest_record_routes_offers_to_distinct_merchants():
    """MAC + Sephora → two merchant upserts, two offer rows."""
    record = _record(offers=[
        {
            "merchant_inferred": "MAC",
            "canonical_url": "https://maccosmetics.com/p/ruby-woo",
            "destination_url": "https://maccosmetics.com/p/ruby-woo",
            "image_url": "", "price": 21.0, "in_stock": True,
        },
        {
            "merchant_inferred": "Sephora",
            "canonical_url": "https://sephora.com/p/mac-ruby-woo",
            "destination_url": "https://sephora.com/p/mac-ruby-woo",
            "image_url": "", "price": 22.0, "in_stock": True,
        },
    ])
    result = ingest_validated_record(record)
    assert result is not None
    # W2: the OFFER space still routes per retailer; the product row rides
    # separately under its observed seller of record.
    offer_space_ids = {m["merchant_id"] for m in result["merchants"] if not m.get("_ensure_only")}
    offer_merchant_ids = {o["merchant_id"] for o in result["offers"]}
    assert len(offer_space_ids) == 2
    assert offer_space_ids == offer_merchant_ids
    sellers_of_record = [m for m in result["merchants"] if m.get("_ensure_only")]
    assert len(sellers_of_record) == 1


def test_offer_handles_out_of_stock_and_missing_price():
    record = _record(offers=[
        {
            "merchant_inferred": "MAC",
            "canonical_url": "https://maccosmetics.com/p/ruby-woo",
            "destination_url": "https://maccosmetics.com/p/ruby-woo",
            "image_url": "",
            "price": None,
            "in_stock": False,
        },
    ])
    result = ingest_validated_record(record)
    assert result is not None
    offer = result["offers"][0]
    assert offer["availability"] == "unknown"
    assert offer["list_price"] is None
    assert offer["inventory_quantity"] == 0
    assert offer["price_confidence"] is None


# --- Phase 7c: synthetic variant + availability prevent zero_variants blocker ---


def test_seed_carries_synthetic_variant_in_seed_data():
    """Phase 7c: every agent seed must ship with a non-empty variants
    array inside seed_data and a column-level availability value.
    Without this, evaluate_external_referral_seed flags zero_variants
    (severity=blocker) and the recall loader silently drops the seed."""
    result = ingest_validated_record(_record())
    assert result is not None
    seed = result["seeds"][0]
    assert seed["availability"] in {"in_stock", "out_of_stock"}
    sd = json.loads(seed["seed_data"])
    variants = sd.get("variants")
    assert isinstance(variants, list) and len(variants) == 1, "exactly one synthetic variant per offer"
    v = variants[0]
    assert v["currency"] == "USD"
    assert v["price_amount"] == 21.0
    assert v["availability"] == "in_stock"
    assert v["variant_id"].endswith("::canonical")
    assert v["sku"] == seed["external_product_id"]


def test_seed_data_mirrors_title_and_image_urls_for_audit():
    """The audit's collect_seed_image_urls / title resolution paths
    look inside seed_data first. We mirror so the audit doesn't fall
    back to less reliable column reads."""
    result = ingest_validated_record(_record())
    seed = result["seeds"][0]
    sd = json.loads(seed["seed_data"])
    assert sd["title"] == "Ruby Woo Matte Lipstick"
    assert sd["image_urls"] == ["https://maccosmetics.com/img/ruby-woo.jpg"]


def test_seed_clears_every_audit_blocker_it_can_earn_at_mint_time():
    """End-to-end pin: a seed produced by the new builder earns NO content blocker.

    This is the regression test for the actual bug — pre-Phase-7c agent seeds were
    status='blocked' on zero_variants and dropped silently at recall time.

    It no longer asserts status='healthy'. A newly minted seed is now blocked on
    `destination_never_verified`, and correctly so: `validated_at` records when GEMINI
    asserted the offer (grounded search, see services/catalog_enrichment_agent/
    gemini_url_validator), and nothing in this pipeline ever fetches the PDP. An LLM's
    claim that a URL exists is not an observation that it resolves — and these are exactly
    the URLs most likely to be wrong. The seed serves once
    jobs/external_seed_destination_sweep has actually loaded it, which it does first,
    because the sweep queue is ordered `destination_checked_at NULLS FIRST`.

    So the assertion is the one this test was always about: every blocker the BUILDER can
    clear is cleared.
    """
    import asyncio as _asyncio
    from services.external_referral_readiness import evaluate_external_referral_seed

    result = ingest_validated_record(_record())
    seed = result["seeds"][0]

    async def _check():
        status = await evaluate_external_referral_seed(
            seed, matched_via="test", allowed_domains=["maccosmetics.com"]
        )
        return status

    status = _asyncio.run(_check())
    blockers = set(status.blocker_anomaly_types or [])
    assert blockers == {"destination_never_verified"}, (
        f"the builder must leave no blocker of its own; got {sorted(blockers)}"
    )
    assert "zero_variants" not in blockers, "the Phase-7c defect, pinned"


def test_a_minted_seed_serves_once_its_destination_is_actually_verified():
    """The other half: verification is the ONLY thing standing between mint and serving."""
    import asyncio as _asyncio
    from datetime import datetime, timezone

    from services.external_referral_readiness import evaluate_external_referral_seed

    result = ingest_validated_record(_record())
    seed = dict(result["seeds"][0])
    seed.update(
        destination_checked_at=datetime.now(timezone.utc).isoformat(),
        destination_http_status=200,
        destination_verdict="live",
        destination_failure_streak=0,
    )

    async def _check():
        return await evaluate_external_referral_seed(
            seed, matched_via="test", allowed_domains=["maccosmetics.com"]
        )

    status = _asyncio.run(_check())
    assert status.status == "healthy", (
        f"a verified agent seed must pass cleanly; got blockers="
        f"{list(status.blocker_anomaly_types or [])}"
    )


def test_seed_is_out_of_stock_when_offer_marked_oos():
    record = _record(offers=[
        {
            "merchant_inferred": "MAC",
            "canonical_url": "https://maccosmetics.com/p/ruby-woo",
            "destination_url": "https://maccosmetics.com/p/ruby-woo",
            "image_url": "https://maccosmetics.com/img/x.jpg",
            "price": 21.0,
            "in_stock": False,
        },
    ])
    result = ingest_validated_record(record)
    seed = result["seeds"][0]
    assert seed["availability"] == "out_of_stock"
    sd = json.loads(seed["seed_data"])
    assert sd["variants"][0]["availability"] == "out_of_stock"
    assert sd["variants"][0]["in_stock"] is False


def test_pdp_insert_writes_jsonl_tags_when_supplied():
    """Phase O-1 followup. Path C (catalog enrichment agent) JSONL may now
    carry an optional pdp.tags field — the ingestion path persists it
    into catalog_products.tags. Stringified because the runner wraps
    the bind value with CAST(:tags AS jsonb)."""

    # Tag list supplied
    record = _record(tags=["matte", "retro-red", "long-wear"])
    result = ingest_validated_record(record)
    pdp_row = result["pdp"]
    assert pdp_row["tags"] == json.dumps(
        ["matte", "retro-red", "long-wear"], ensure_ascii=False
    )

    # Tags as comma-separated string also supported (curator convenience)
    record = _record(tags="vegan, cruelty-free")
    result = ingest_validated_record(record)
    pdp_row = result["pdp"]
    assert pdp_row["tags"] == json.dumps(
        ["vegan", "cruelty-free"], ensure_ascii=False
    )

    # No tags supplied → still serializes to '[]', not None / missing.
    # Same semantic as ingest_standard_products on Path A: "we looked,
    # found no tags" instead of "field absent".
    record = _record()
    assert "tags" not in record["pdp"]
    result = ingest_validated_record(record)
    pdp_row = result["pdp"]
    assert pdp_row["tags"] == "[]"


def test_pdp_insert_writes_o2_taxonomy_columns():
    """Phase O-2: catalog enrichment agent ingestion must also populate
    price_tier / use_case_tags / lifestyle_tags / demographic. The
    helper at services.pdp_taxonomy is the same one Path A and Path B
    use, so we mainly assert wiring (column present, JSON serialized
    correctly) rather than re-testing the extractors."""

    # Record with attributes that trigger lifestyle + demographic + use-case
    record = _record(
        product_name="Vegan Daily Lipstick for Women",
        attribute_summary="cruelty-free, everyday wear, retro red",
        offers=[
            {
                "merchant_inferred": "MAC",
                "destination_url": "https://maccosmetics.com/p/x",
                "canonical_url": "https://maccosmetics.com/p/x",
                "image_url": "https://maccosmetics.com/img/x.jpg",
                "price": 28.0,
                "in_stock": True,
                "validated_at": "2026-05-08T00:00:00Z",
            },
        ],
    )
    result = ingest_validated_record(record)
    pdp_row = result["pdp"]
    # price_tier is a scalar; offers[0].price=28 → under_50
    assert pdp_row["price_tier"] == "under_50"
    # JSONB columns are JSON-stringified for CAST(:... AS jsonb)
    assert "vegan" in json.loads(pdp_row["lifestyle_tags"])
    assert "cruelty_free" in json.loads(pdp_row["lifestyle_tags"])
    assert "daily" in json.loads(pdp_row["use_case_tags"])
    assert pdp_row["demographic"] == "women"

    # Record with no signals → consistent empty / None shape
    record_blank = _record(
        product_name="Plain Item",
        attribute_summary="just a thing",
        offers=[
            {
                "merchant_inferred": "Brand",
                "destination_url": "https://brand.example/p",
                "canonical_url": "https://brand.example/p",
                "image_url": "https://brand.example/img.jpg",
                "price": 150.0,
                "in_stock": True,
            },
        ],
    )
    result = ingest_validated_record(record_blank)
    pdp_row = result["pdp"]
    assert pdp_row["price_tier"] == "100_200"
    assert json.loads(pdp_row["use_case_tags"]) == []
    assert json.loads(pdp_row["lifestyle_tags"]) == []
    assert pdp_row["demographic"] is None

    # Record with no offers → price_tier is None (no signal)
    record_no_price = _record(
        product_name="Vegan Cream",
        attribute_summary="cruelty-free",
        offers=[],
    )
    # offers=[] is filtered by ingest_validated_record (returns None)
    result = ingest_validated_record(record_no_price)
    assert result is None  # confirms no-offer records are dropped early


def test_pdp_insert_writes_o4_lifecycle_stage():
    """Phase O-4: Path C ingestion must populate pdp_lifecycle_stage on
    every PDP row. Path C rows ship with source_system='catalog_enrichment_agent_v1',
    which is treated as canonical evidence — so a fully-populated agent
    row reaches 'published'."""

    # Hand-curated agent record with full content + taxonomy signals.
    record = _record(
        product_name="Vegan Daily Lipstick for Women",
        attribute_summary=(
            "Cruelty-free retro-red lipstick designed for everyday "
            "long-wear comfort and bold pigment payoff."
        ),
        tags=["k-beauty"],
    )
    result = ingest_validated_record(record)
    pdp_row = result["pdp"]
    assert "pdp_lifecycle_stage" in pdp_row, (
        "Path C write must include pdp_lifecycle_stage column (Phase O-4)"
    )
    # Has title + image_url (offer) + long description + category_path +
    # taxonomy + canonical evidence (source_system) → published.
    assert pdp_row["pdp_lifecycle_stage"] == "published"

    # Thin record: short attribute_summary + no taxonomy hints → caps
    # at draft (description below candidate min length).
    thin = _record(
        product_name="X",
        attribute_summary="brief",
    )
    # canonical_product_name="x" so brand=MAC is still set; pdp passes
    # the required-fields gate but content is below candidate threshold.
    result = ingest_validated_record(thin)
    pdp_row = result["pdp"]
    # description=attribute_summary='brief' is too short → can't promote
    # past candidate; image_url present from offer; title present.
    assert pdp_row["pdp_lifecycle_stage"] in {"draft", "candidate"}
    # Specifically: 'brief' is 5 chars, well below CANDIDATE_DESCRIPTION_MIN_LEN
    assert pdp_row["pdp_lifecycle_stage"] == "draft"
