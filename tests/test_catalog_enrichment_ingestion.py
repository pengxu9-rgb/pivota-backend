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

    `stale_snapshot` is blocked for the SAME reason and it is a second, independent fact:
    the builder writes no `snapshot.extracted_at` because it never extracted anything from a
    page. Gemini asserted the price; nobody read it. A gate that let an LLM-asserted price
    serve as "fresh content" is the fabrication this lane exists to keep out, so both
    blockers standing here is the honest state, not a fixture gap.

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
    assert blockers == {"destination_never_verified", "stale_snapshot"}, (
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
    # BOTH facts, because they are two facts. The sweep proves the link resolves; it reads no
    # price. A content refresh is what clears `stale_snapshot`, and a seed needs both before
    # it may serve — that is precisely the pair a single `destination_checked_at` collapsed.
    import json as _json

    raw = seed.get("seed_data") or {}
    seed_data = _json.loads(raw) if isinstance(raw, str) else dict(raw)
    snapshot = dict(seed_data.get("snapshot") or {})
    snapshot["extracted_at"] = datetime.now(timezone.utc).isoformat()
    seed_data["snapshot"] = snapshot
    seed["seed_data"] = seed_data

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


# --- real variants ride beside the canonical SKU (folded shade lines) ---------


def _variant_record(n_variants=2):
    variants = [
        {"variant_id": f"5405734574509{i}", "sku": f"M0N90{i}", "barcode": f"77360204936{i}",
         "title": ["Ruby Woo", "Bronx", "Dangerous"][i], "price": 24.0 + i, "in_stock": i != 1,
         "image_url": f"https://cdn.x/{i}.jpg", "source_handle": f"retro-matte-lipstick-{i}"}
        for i in range(n_variants)
    ]
    return {
        "pdp": {"brand": "MAC Cosmetics", "product_name": "Retro Matte Lipstick",
                "category_path": "beauty/makeup", "attribute_summary": "matte lipstick",
                "source_domain": "maccosmetics.com", "variants": variants},
        "offers": [{"merchant_inferred": "MAC Cosmetics",
                    "canonical_url": "https://maccosmetics.com/products/retro-matte-lipstick",
                    "destination_url": "https://maccosmetics.com/products/retro-matte-lipstick",
                    "image_url": "https://cdn.x/base.jpg", "price": 24.0, "in_stock": True,
                    "validated_at": "shopify_products_json"}],
    }


def test_variants_become_shade_skus_and_offers_beside_the_canonical():
    from services.catalog_enrichment_agent import ingestion as ing

    result = ing.ingest_validated_record(_variant_record(3))
    assert result and not result.get("skipped_reason")
    pk = result["pdp"]["product_key"]
    assert result["sku"]["sku_key"] == pk + ing.SKU_SUFFIX
    vskus = result["variant_skus"]
    assert [s["title"] for s in vskus] == ["Ruby Woo", "Bronx", "Dangerous"]
    assert all(s["sku_key"].startswith(pk + ing.VARIANT_SKU_INFIX) for s in vskus)
    assert [s["source_variant_id"] for s in vskus] == ["54057345745090", "54057345745091", "54057345745092"]
    assert vskus[0]["sku"] == "M0N900" and vskus[0]["barcode"] == "773602049360"
    # the label shape catalog_sync_service's shade extractor keys on
    assert json.loads(vskus[0]["visible_option_labels"]) == ["shade_ruby_woo"]
    assert vskus[0]["image_url"] == "https://cdn.x/0.jpg"
    # one offer per variant SKU, priced and stocked per variant, plus the canonical one
    offers = result["offers"]
    assert len(offers) == 4
    by_sku = {o["sku_key"]: o for o in offers}
    assert by_sku[vskus[1]["sku_key"]]["list_price"] == 25.0
    assert by_sku[vskus[1]["sku_key"]]["availability"] == "unknown"   # in_stock False
    assert by_sku[vskus[0]["sku_key"]]["availability"] == "in_stock"
    assert len({o["offer_id"] for o in offers}) == 4


def test_single_variant_records_write_only_the_canonical_sku():
    from services.catalog_enrichment_agent import ingestion as ing

    result = ing.ingest_validated_record(_variant_record(1))
    assert result["variant_skus"] == []
    assert len(result["offers"]) == 1


def test_plan_counts_variant_skus():
    from services.catalog_enrichment_agent import ingestion as ing

    plan = ing.ingest_validated_jsonl([_variant_record(3)])
    assert len(plan["pdps"]) == 1
    assert len(plan["skus"]) == 4
    assert len(plan["offers"]) == 4
    assert plan["skipped"] == 0


def test_variant_sku_key_fits_the_column_for_a_long_product_key():
    from services.catalog_enrichment_agent import ingestion as ing

    long_pk = "ext:" + ("a" * 200) + "::deadbeef"          # 214 chars, the real ceiling
    key = ing.derive_variant_sku_key(long_pk, "5405734574509012345678901234567890")
    assert key.startswith(long_pk + ing.VARIANT_SKU_INFIX)
    assert len(key) <= 255
    # stable across calls — re-runs must UPSERT, not mint a second row
    assert key == ing.derive_variant_sku_key(long_pk, "5405734574509012345678901234567890")


# --- the seed carries the real variants the PDP renders from --------------------


def test_seed_data_carries_every_real_variant_for_the_pdp():
    """The external-seed serving lane builds its variant list from
    seed_data['variants'], NOT from catalog_skus, so a folded shade line whose
    shades exist as SKUs still rendered ONE synthetic variant and the shade
    identity was invisible on the page."""
    from services.catalog_enrichment_agent import ingestion as ing

    result = ing.ingest_validated_record(_variant_record(3))
    seed = result["seeds"][0]
    variants = json.loads(seed["seed_data"])["variants"]
    assert [v["title"] for v in variants] == ["Ruby Woo", "Bronx", "Dangerous"]
    # the keys routes/agent_api.py::_build_external_seed_product actually reads
    assert [v["variant_id"] for v in variants] == ["54057345745090", "54057345745091", "54057345745092"]
    assert [v["price_amount"] for v in variants] == [24.0, 25.0, 26.0]
    assert {v["price_currency"] for v in variants} == {"USD"}
    assert [v["availability"] for v in variants] == ["in_stock", "out_of_stock", "in_stock"]
    assert variants[0]["image_url"] == "https://cdn.x/0.jpg"
    assert variants[0]["sku"] == "M0N900"


def test_a_single_variant_record_still_writes_the_synthetic_canonical_variant():
    """Byte-identical for every lane that does not fold: one synthetic variant,
    named for the product, keyed on the canonical id."""
    from services.catalog_enrichment_agent import ingestion as ing

    result = ing.ingest_validated_record(_variant_record(1))
    variants = json.loads(result["seeds"][0]["seed_data"])["variants"]
    assert len(variants) == 1
    assert variants[0]["variant_id"].endswith("::canonical")
    assert variants[0]["title"] == "Retro Matte Lipstick"


def test_seed_variants_are_bounded():
    from services.catalog_enrichment_agent import ingestion as ing

    rec = _variant_record(2)
    rec["pdp"]["variants"] = [
        {"variant_id": f"v{i}", "sku": f"S{i}", "title": f"Shade {i}", "price": 24.0,
         "in_stock": True, "image_url": None}
        for i in range(ing.MAX_SEED_VARIANTS + 25)
    ]
    variants = json.loads(ing.ingest_validated_record(rec)["seeds"][0]["seed_data"])["variants"]
    assert len(variants) == ing.MAX_SEED_VARIANTS


# -- currency / market passthrough ------------------------------------------------------------
# Every currency in ingestion.py was the literal "USD" in seven places and nothing read one off
# the record, so a Singapore storefront pricing in SGD was ingested as USD. Measured 2026-09-06
# on jsmbeauty.sg: 170 offers, all USD, against a storefront whose /meta.json says SGD/SG and
# whose LIP-PRESSION Glowy Tint is SGD 30.00.



def _seed_variant_currencies(out, key=None):
    """Currencies on the variants nested inside every seed's `seed_data`.

    With no `key`, computes what the SERVING lane computes:
    `v.get("price_currency") or v.get("currency")` (routes/agent_api.py). Asserting only
    `currency` measures the surface the code reaches SECOND -- a mutant flipping `price_currency`
    alone survived a full review round for exactly that reason. The synthetic canonical variant
    legitimately sets only `currency`, which is what that `or` is for, so the effective value is
    the honest assertion for both branches.
    """
    import json as _json

    found = set()
    for seed in out["seeds"]:
        data = seed.get("seed_data")
        if isinstance(data, str):
            data = _json.loads(data)
        for v in (data or {}).get("variants") or []:
            found.add(v.get(key) if key else (v.get("price_currency") or v.get("currency")))
    return found


# THREE REAL VARIANTS, deliberately. The first draft of this fixture had none, so it never
# entered the real-variant branch -- and the eighth currency site (the variant `_build_offer_inserts`
# call) went unnoticed while a ten-mutant pass reported all-clear. A shade line is also the SHAPE
# that matters: `_HAS_US_OFFER_EXISTS` is an EXISTS over catalog_offers by PRODUCT_KEY, so a
# single USD-stamped variant offer keeps the whole product on the US surface.
_SGD_VARIANTS = [
    {"variant_id": "v1", "title": "Early Peach", "price": 30.0, "in_stock": True},
    {"variant_id": "v2", "title": "Rose Beige", "price": 30.0, "in_stock": True},
    {"variant_id": "v3", "title": "Coral", "price": 30.0, "in_stock": True},
]


def _sgd_record(**extras):
    extras.setdefault("variants", _SGD_VARIANTS)
    extras.setdefault("currency", "SGD")      # override with currency=None for the unknown case
    return _record(brand="JUNGSAEMMOOL", product_name="LIP-PRESSION Glowy Tint",
                   source_domain="jsmbeauty.sg", **extras)


def test_a_storefronts_own_currency_reaches_every_row_it_prices():
    """The whole point: offers, SKUs and seeds must all carry the record's currency.

    Asserted across ALL THREE collections rather than one, because they are built by three
    different functions -- `_build_offer_inserts` does not even receive the pdp payload -- and a
    fix that reached only the one under test would look complete.
    """
    from services.catalog_enrichment_agent.ingestion import ingest_validated_record

    out = ingest_validated_record(_sgd_record())

    # EVERY offer, canonical AND per-variant. Asserting a set means one USD straggler fails,
    # which is exactly the shape the eighth site produced: {SGD: 1, USD: 3}.
    assert len(out["offers"]) >= 4, "the fixture must produce variant offers, not just canonical"
    assert {o["currency"] for o in out["offers"]} == {"SGD"}
    assert out["sku"]["currency"] == "SGD"
    # THE VARIANT SKUs. Nothing else reads this column, which is exactly why a mutant flipping it
    # to "USD" survived two review rounds: no assertion reached it.
    assert {v["currency"] for v in out["variant_skus"]} == {"SGD"}
    assert {s["price_currency"] for s in out["seeds"]} == {"SGD"}
    # The seed's NESTED variant list, which is a separate site from every row above and was the
    # one replacement no assertion reached: reverting it alone to a literal "USD" left the whole
    # file green. This is what the serving lane renders, so it is not an internal detail.
    assert _seed_variant_currencies(out) == {"SGD"}
    # BOTH KEYS, on the REAL-VARIANT branch specifically. Two consumers disagree about which one
    # wins: serving reads `price_currency or currency`, while `external_seed_audit`'s
    # `normalize_seed_variants` reads `currency or price_currency` -- the OPPOSITE order -- and it
    # is the audit's reading that raises `price_currency_mismatch`, a BLOCKER anomaly that drops
    # the seed from the agent surface entirely. So the serving-order assertion above cannot see a
    # wrong `currency` while `price_currency` is right, which is exactly the mutant that survived
    # two rounds. A first attempt at this assertion was added to the SYNTHETIC-variant test by
    # mistake, where this branch never runs -- and the mutant survived a third time.
    assert _seed_variant_currencies(out, "currency") == {"SGD"}
    assert _seed_variant_currencies(out, "price_currency") == {"SGD"}


def test_the_pdp_payload_whitelist_does_not_drop_the_currency():
    """`_build_pdp_payload` is a WHITELIST -- a field absent from its dict literal is dropped no
    matter what the record carried. The first draft of this change threaded currency through the
    crawler and every consumer and was still a COMPLETE no-op, because these two keys were not in
    that literal. This pins the seam itself, so the passthrough cannot be silently severed while
    every consumer keeps reading a field that never arrives."""
    from services.catalog_enrichment_agent.ingestion import _build_pdp_payload

    payload = _build_pdp_payload(_sgd_record())

    assert payload["currency"] == "SGD"
    # market is deliberately NOT carried: `external_product_seeds.market` is a hard serving
    # partition (`external_seed_search` appends `market = :market`, defaulted to "US"), so
    # stamping a storefront's country there deletes it from US seed search.
    assert "market" not in payload


def test_a_record_with_no_currency_is_still_USD_and_US():
    """The POSITIVE counterpart and the backward-compatibility guarantee: every record built
    before this change carries no currency and must land exactly as it did. Verified against the
    live lane too -- flowerbeauty.com and maccosmetics.com both report USD/US."""
    from services.catalog_enrichment_agent.ingestion import ingest_validated_record

    out = ingest_validated_record(_record())

    assert {o["currency"] for o in out["offers"]} == {"USD"}
    assert out["sku"]["currency"] == "USD"
    assert {s["price_currency"] for s in out["seeds"]} == {"USD"}
    assert {s["market"] for s in out["seeds"]} == {"US"}   # unchanged: not this lane's axis
    assert _seed_variant_currencies(out) == {"USD"}
    assert _seed_variant_currencies(out, "currency") == {"USD"}


@pytest.mark.parametrize(
    "bad",
    ["", "  ", "sgd dollars", "S", "SGDX", "$", "12", None, 5, ["SGD"], {"c": "SGD"}, "SG D",
     "USD DROP TABLE catalog_offers", "US" + chr(0) + "D"],
)
def test_a_merchant_supplied_currency_that_is_not_ISO_falls_back_to_USD(bad):
    """`/meta.json` is MERCHANT-CONTROLLED and its value lands in a currency column that is a
    join key for price comparison. Anything that is not ISO-4217 alpha-3 is refused rather than
    written through -- including an embedded NUL, which Postgres rejects with 22P05, and a
    non-string, which would otherwise reach `.upper()`."""
    from services.catalog_enrichment_agent.ingestion import _currency_of

    assert _currency_of({"currency": bad}) == "USD"


@pytest.mark.parametrize("good,expected", [("sgd", "SGD"), ("SGD", "SGD"), (" jpy ", "JPY")])
def test_a_valid_currency_is_normalised_and_kept(good, expected):
    """The counterpart to the refusal test: a real code must survive, case- and
    space-insensitively -- a refusal test alone passes for a function that returns USD
    unconditionally."""
    from services.catalog_enrichment_agent.ingestion import _currency_of

    assert _currency_of({"currency": good}) == expected


def test_the_seed_market_is_NOT_taken_from_the_storefronts_country():
    """This lane writes CURRENCY and not market, on purpose.

    Measured: `external_seed_search` appends a hard `market = :market` conjunct and every serving
    caller passes DEFAULT_EXTERNAL_SEED_MARKET="US", so a seed stamped with the storefront's own
    country vanishes from US seed search entirely. `catalog_offers.market` by contrast only feeds
    a warn-only counter. An earlier draft stamped both; the harm was asymmetric and this is the
    half that hurt.
    """
    from services.catalog_enrichment_agent.ingestion import ingest_validated_record

    out = ingest_validated_record(_sgd_record())

    assert {s["market"] for s in out["seeds"]} == {"US"}
    assert {s["price_currency"] for s in out["seeds"]} == {"SGD"}



def test_the_synthetic_canonical_variant_also_carries_the_storefronts_currency():
    """A product with FEWER THAN TWO real variants takes the synthetic-canonical branch instead
    of the real-variant loop, and they are different code with their own currency literal.

    Adding `variants` to `_sgd_record` closed the real-variant gap and REMOVED this one -- net
    coverage moved sideways. Both branches now have a fixture.
    """
    from services.catalog_enrichment_agent.ingestion import ingest_validated_record

    out = ingest_validated_record(_sgd_record(variants=[]))

    assert _seed_variant_currencies(out) == {"SGD"}
    assert _seed_variant_currencies(out, "currency") == {"SGD"}
    assert {o["currency"] for o in out["offers"]} == {"SGD"}


# -- the offer upsert SQL itself ---------------------------------------------------------------
# The whole apply.py contribution of the currency change went untested for two review rounds. A
# mutant that dropped one bind while keeping its column produced a 21-column / 20-value INSERT --
# a hard runtime failure on the core catalog write path -- with the entire suite green, because
# every unit test drives a fake DB that never parses the statement.


def _insert_columns_and_binds(sql):
    """The INSERT column list and the VALUES bind list, as parallel sequences."""
    import re as _re

    cols = _re.search(r"INSERT INTO catalog_offers\s*\((.*?)\)\s*VALUES", sql, _re.S).group(1)
    vals = _re.search(r"VALUES\s*\((.*?)\)\s*ON CONFLICT", sql, _re.S).group(1)
    col_names = [c.strip() for c in cols.split(",") if c.strip()]
    bind_names = []
    for v in vals.split(","):
        v = v.strip()
        m = _re.search(r":([a-z_]+)", v)      # survives CAST(:x AS jsonb)
        if m:
            bind_names.append(m.group(1))
    return col_names, bind_names


def test_the_offer_upsert_columns_and_binds_line_up():
    """A column with no bind (or the reverse) is a runtime error on every ingest, and no fake-DB
    test can see it. Asserted positionally, not just by count, so a swap is caught too."""
    from services.catalog_enrichment_agent.apply import _OFFER_UPSERT_SQL

    cols, binds = _insert_columns_and_binds(_OFFER_UPSERT_SQL)

    assert len(cols) == len(binds), f"{len(cols)} columns vs {len(binds)} binds"
    assert cols == binds, [c for c, b in zip(cols, binds) if c != b]


def test_every_offer_row_the_builder_makes_supplies_every_bind():
    """The other half: the SQL and the row dict must agree. A bind the builder never emits is the
    same runtime failure from the opposite direction."""
    from services.catalog_enrichment_agent.apply import _OFFER_UPSERT_SQL
    from services.catalog_enrichment_agent.ingestion import ingest_validated_record

    _, binds = _insert_columns_and_binds(_OFFER_UPSERT_SQL)
    offers = ingest_validated_record(_sgd_record())["offers"]

    assert offers
    for row in offers:
        missing = [b for b in binds if b not in row]
        assert not missing, f"offer row is missing binds the SQL requires: {missing}"


def test_every_row_this_lane_builds_binds_against_the_real_upsert_SQL():
    """THE INVARIANT THAT WOULD HAVE CAUGHT A SILENT TOTAL WRITE OUTAGE.

    An earlier version of this branch added a `currency_known` key to every row so a SQL guard
    could read it. Production `databases.Database.execute` does `text(sql).bindparams(**values)`,
    which RAISES on a key the statement does not define -- and every write sits inside
    `except Exception: logger.exception(...)`. So the plan reported 4 skus / 4 offers / 1 seed and
    the counts came back 0 / 0 / 0: pdps and merchants landed, every child row was silently
    dropped, on the default ingest path.

    The whole suite stayed green through it, because every test drives a double that never binds.
    This asserts the one property those doubles cannot: a row this lane builds must carry no key
    the statement it is executed against does not define.
    """
    from sqlalchemy import text

    import services.catalog_enrichment_agent.apply as apply_mod
    from services.catalog_enrichment_agent.ingestion import ingest_validated_record

    out = ingest_validated_record(_sgd_record())

    for label, sql, rows in (
        ("sku", apply_mod._SKU_UPSERT_SQL, [out["sku"]]),
        ("variant_skus", apply_mod._SKU_UPSERT_SQL, out["variant_skus"]),
        ("offers", apply_mod._OFFER_UPSERT_SQL, out["offers"]),
        ("seeds", apply_mod._SEED_UPSERT_SQL, out["seeds"]),
    ):
        for row in rows:
            params = {k: v for k, v in row.items() if k != "_ensure_only"}
            try:
                text(sql).bindparams(**params)
            except Exception as exc:  # noqa: BLE001 - the failure IS the assertion
                raise AssertionError(f"{label} row will not bind: {exc}") from exc
