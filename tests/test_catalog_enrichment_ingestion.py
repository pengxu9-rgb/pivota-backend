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


def test_seed_variants_announce_the_axis_they_vary_on():
    """WHY THIS EXISTS. Real variant identity (#2073) was necessary and not
    sufficient. The renderer already exposed the variant LIST — a variant with a
    non-placeholder title is enough for that — but it renders the SELECTOR only
    when a variant announces what it VARIES ON, reading `options` first and an
    explicit `display_label` second. #2073's seven real MAC shades carried
    neither, so they arrived as an unlabelled list with no way to pick one
    (measured against the deployed gateway 2026-09-05: 0 of 3 displayable).

    The renderer additionally demands visual evidence on a shade axis, which the
    per-variant image_url supplies."""
    from services.catalog_enrichment_agent import ingestion as ing

    rec = _variant_record(3)
    for v in rec["pdp"]["variants"]:
        v["option_name"] = "Color"
    variants = json.loads(ing.ingest_validated_record(rec)["seeds"][0]["seed_data"])["variants"]

    assert [v["options"] for v in variants] == [
        [{"name": "Color", "value": "Ruby Woo"}],
        [{"name": "Color", "value": "Bronx"}],
        [{"name": "Color", "value": "Dangerous"}],
    ]
    # the shade axis is only displayable WITH visual evidence
    assert all(v["image_url"] for v in variants)


def test_a_variant_with_no_option_name_gets_no_axis_invented_for_it():
    """`_build_seed_inserts` runs on ANY record with two or more variants, not
    only folded shade lines — `run_catalog_enrichment ingest` feeds it
    hand-validated JSONL. Guessing "Shade" there published a volume axis as
    "Shade: 30 ml". Only the fold knows the axis, and it always names it."""
    from services.catalog_enrichment_agent import ingestion as ing

    rec = _variant_record(2)
    rec["pdp"]["variants"][0]["title"] = "30 ml"
    rec["pdp"]["variants"][1]["title"] = "50 ml"
    variants = json.loads(ing.ingest_validated_record(rec)["seeds"][0]["seed_data"])["variants"]
    assert [v["options"] for v in variants] == [[], []]
    # the variants still SERVE — they are just an unlabelled list, as before
    assert [v["title"] for v in variants] == ["30 ml", "50 ml"]


def test_an_axis_that_cannot_tell_two_variants_apart_is_dropped_entirely():
    """`option1` is only the FIRST axis. A lip gloss sold Full/Pink and
    Full/Sample yields two variants both labelled "Size: Full" — a picker with
    indistinguishable entries, which is a wrong page where there was previously
    a bare list. The per-element product-name check cannot see this."""
    from services.catalog_enrichment_agent import ingestion as ing

    rec = _variant_record(3)
    for v in rec["pdp"]["variants"]:
        v["option_name"] = "Size"
    rec["pdp"]["variants"][0]["title"] = "Full"
    rec["pdp"]["variants"][1]["title"] = "Full"
    rec["pdp"]["variants"][2]["title"] = "Travel"
    variants = json.loads(ing.ingest_validated_record(rec)["seeds"][0]["seed_data"])["variants"]
    assert [v["options"] for v in variants] == [[], [], []]


def test_a_distinguishing_axis_survives_the_aggregate_check():
    """The positive counterpart — the drop must not fire on a healthy product."""
    from services.catalog_enrichment_agent import ingestion as ing

    rec = _variant_record(3)
    for v in rec["pdp"]["variants"]:
        v["option_name"] = "Shade"
    variants = json.loads(ing.ingest_validated_record(rec)["seeds"][0]["seed_data"])["variants"]
    assert [v["options"][0]["value"] for v in variants] == ["Ruby Woo", "Bronx", "Dangerous"]


def test_a_placeholder_shade_yields_no_option_pair():
    """"Default Title" is what a shop with no option axis returns. Rendered as a
    selector it would read the same on every entry, which is worse than none."""
    from services.catalog_enrichment_agent import ingestion as ing

    rec = _variant_record(2)
    for v in rec["pdp"]["variants"]:
        v["option_name"] = "Shade"
    rec["pdp"]["variants"][0]["title"] = "Default Title"
    # variant 1 keeps its REAL shade, so the placeholder filter is the only thing
    # that can produce an unlabelled product here
    variants = json.loads(ing.ingest_validated_record(rec)["seeds"][0]["seed_data"])["variants"]
    assert [v["options"] for v in variants] == [[], []]
    # ...and both rows are still served — dropping a LABEL never drops a variant
    assert [v["title"] for v in variants] == ["Default Title", "Bronx"]


def test_a_shade_equal_to_the_product_name_yields_no_option_pair():
    """The mapper falls back to the product title when option1 is absent. A
    selector reading "Shade: Retro Matte Lipstick" names no choice."""
    from services.catalog_enrichment_agent import ingestion as ing

    rec = _variant_record(2)
    for v in rec["pdp"]["variants"]:
        v["option_name"] = "Shade"
    rec["pdp"]["variants"][0]["title"] = "retro matte LIPSTICK"
    variants = json.loads(ing.ingest_validated_record(rec)["seeds"][0]["seed_data"])["variants"]
    # ...and because that leaves the product only PARTLY labelled, the rest go too
    assert [v["options"] for v in variants] == [[], []]


def test_the_synthetic_canonical_variant_gains_no_options():
    """The non-folding lanes must stay byte-identical: one synthetic variant
    named for the product, carrying no axis."""
    from services.catalog_enrichment_agent import ingestion as ing

    variants = json.loads(
        ing.ingest_validated_record(_variant_record(1))["seeds"][0]["seed_data"]
    )["variants"]
    assert len(variants) == 1
    assert "options" not in variants[0]


def test_seed_data_does_not_publish_the_brands_onboarding_category():
    """A DELIBERATE OMISSION. The deployed renderer keyword-matches the seed's own
    category and tags to decide whether a shade axis is allowed, so writing them
    would carry the axis on products whose title says nothing cosmetic. It is the
    wrong fix: `category_path` on this lane is a PER-BRAND constant threaded from
    the onboarding row (`{"domain": "kosas.com", "category_path": "beauty/makeup"}`),
    so every product of a brand would claim "makeup" — publishing a fragrance's
    scents and a cleanser's sizes as shades, with a swatch demanded for each.

    It also reaches much further than the axis: `brand_category` is a binary
    component of the servability quality score, so minting a category out of a
    brand-wide path moves rows across the 71.4 gate on a value that describes the
    onboarding cohort rather than the product.

    A real per-product category belongs here; the brand's path does not.
    """
    from services.catalog_enrichment_agent import ingestion as ing

    rec = _variant_record(2)
    rec["pdp"]["tags"] = ["complexion"]
    seed = json.loads(ing.ingest_validated_record(rec)["seeds"][0]["seed_data"])
    assert "category" not in seed
    assert "tags" not in seed
    assert seed["category_path"] == "beauty/makeup"   # unchanged for existing readers


def test_a_partly_labelled_product_loses_every_label():
    """The renderer shows a picker as soon as ONE variant is displayable, and
    lists only the displayable ones. A base whose own variants sit on an axis the
    renderer does not recognise, with shades folded in beside them, rendered a
    shade picker offering 1 of 3 purchasable variants — the other two titled
    "Default". Before this lane wrote any options that product showed nothing, so
    a partial labelling is a regression, not a partial win."""
    from services.catalog_enrichment_agent import ingestion as ing

    rec = _variant_record(3)
    rec["pdp"]["variants"][0]["option_name"] = "Shade"
    rec["pdp"]["variants"][1]["option_name"] = ""       # the base's own, unnamed axis
    rec["pdp"]["variants"][2]["option_name"] = ""
    variants = json.loads(ing.ingest_validated_record(rec)["seeds"][0]["seed_data"])["variants"]
    assert [v["options"] for v in variants] == [[], [], []]
    # ...and every variant still SERVES, exactly as it did before any axis was named
    assert [v["title"] for v in variants] == ["Ruby Woo", "Bronx", "Dangerous"]


def test_a_product_on_two_axes_is_not_labelled_at_all():
    """We cannot label our way out of this from here. The renderer accepts a
    SHADE axis only on a product its own keyword gate reads as cosmetic, and
    drops the option otherwise — so on a foundation sold in two sizes with shades
    folded in, it keeps "Size", drops the shades, and renders a picker offering 2
    of 4. Emitting one axis per product is what makes that call all-or-nothing."""
    from services.catalog_enrichment_agent import ingestion as ing

    rec = _variant_record(3)
    rec["pdp"]["variants"][0]["option_name"] = "Size"
    rec["pdp"]["variants"][1]["option_name"] = "Size"
    rec["pdp"]["variants"][2]["option_name"] = "Shade"
    variants = json.loads(ing.ingest_validated_record(rec)["seeds"][0]["seed_data"])["variants"]
    assert [v["options"] for v in variants] == [[], [], []]


def test_a_single_axis_product_keeps_its_labels():
    """The positive counterpart — the single-axis rule must not fire on the very
    shape this lane exists to serve."""
    from services.catalog_enrichment_agent import ingestion as ing

    rec = _variant_record(3)
    for v in rec["pdp"]["variants"]:
        v["option_name"] = "Shade"
    variants = json.loads(ing.ingest_validated_record(rec)["seeds"][0]["seed_data"])["variants"]
    assert [v["options"][0]["name"] for v in variants] == ["Shade", "Shade", "Shade"]


@pytest.mark.parametrize("placeholder", ["Default", "Default Title", "Title", "Variant",
                                         "Single Item", "One Size", "N/A", "n/a", "  default  "])
def test_every_placeholder_shade_value_yields_no_pair(placeholder):
    """`_PLACEHOLDER_SHADE_VALUES` was pinned at 1 of its 7 entries — the other
    six could be deleted with the suite still green."""
    from services.catalog_enrichment_agent import ingestion as ing

    rec = _variant_record(2)
    for v in rec["pdp"]["variants"]:
        v["option_name"] = "Shade"
    rec["pdp"]["variants"][0]["title"] = placeholder
    variants = json.loads(ing.ingest_validated_record(rec)["seeds"][0]["seed_data"])["variants"]
    # unlabelled + labelled is a partial labelling, so the whole product drops
    assert [v["options"] for v in variants] == [[], []]


def test_the_mapper_and_the_ingestion_agree_on_the_seam_between_them():
    """END TO END, no hand-written record. The two modules each hardcode the
    string `option_name` independently, and nothing executed the seam — renaming
    one side is caught, but the contract itself was never run. This repo has a
    history of cross-module string contracts breaking in exactly that gap."""
    from services.catalog_enrichment_agent import ingestion as ing
    from services.curated_brand_feed import fold_shade_listings, shopify_product_to_record

    def prod(**over):
        base = {"title": "T", "handle": "h", "vendor": "MAC Cosmetics", "product_type": "Lipstick",
                "tags": [], "images": [], "options": [], "variants": []}
        base.update(over)
        return base

    stub = prod(title="Retro Matte Lipstick", handle="rml",
                options=[{"name": "Title", "values": ["Default Title"]}],
                variants=[{"id": 1, "sku": "P", "price": "24.00", "option1": "Default Title",
                           "available": True}])
    shades = [
        prod(title=f"Retro Matte Lipstick - {n}", handle=f"rml-{i}",
             images=[{"src": f"https://cdn.x/{n}.jpg"}],
             options=[{"name": "Title", "values": ["Default Title"]}],
             variants=[{"id": 100 + i, "sku": f"M0N90{i}", "price": "24.00", "available": True,
                        "featured_image": {"src": f"https://cdn.x/{n}.jpg"}}])
        for i, n in enumerate(["Ruby Woo", "Bronx", "Dangerous"])
    ]
    out, report = fold_shade_listings([stub] + shades)
    assert report["shades"] == 3, "fold refused — the rest of this test would be vacuous"

    rec = shopify_product_to_record(out[0], domain="maccosmetics.com",
                                    category_path="beauty/makeup", emit_variants=True)
    rec["offers"][0]["validated_at"] = "shopify_products_json"
    seed = json.loads(ing.ingest_validated_record(rec)["seeds"][0]["seed_data"])

    # "Color", not "Shade": the renderer rejects a shade-named axis outright when
    # its cosmetic keyword gate is closed, and accepts a colour-named one.
    assert [v["options"] for v in seed["variants"]] == [
        [{"name": "Color", "value": "Ruby Woo"}],
        [{"name": "Color", "value": "Bronx"}],
        [{"name": "Color", "value": "Dangerous"}],
    ]
    assert all(v["image_url"] for v in seed["variants"])   # the shade axis needs a swatch


def test_a_variant_with_no_shade_text_at_all_yields_no_pair():
    """The mapper emits `title: option1 or title or None`, so a variant with
    neither is an EMPTY value. Without the empty check that ships as
    `{"name": "Shade", "value": ""}` — and beside two real distinct shades the
    aggregate drop does not clear it (every variant labelled, keys distinct, one
    axis), so an entry naming no choice reaches the selector."""
    from services.catalog_enrichment_agent import ingestion as ing

    rec = _variant_record(3)
    for v in rec["pdp"]["variants"]:
        v["option_name"] = "Shade"
    rec["pdp"]["variants"][0]["title"] = ""
    variants = json.loads(ing.ingest_validated_record(rec)["seeds"][0]["seed_data"])["variants"]
    assert all(not v["options"] for v in variants), "an empty value must never be a label"


def test_a_whitespace_only_axis_name_is_not_published():
    """Hand-validated JSONL is human-authored — the lane this function's own
    docstring names — so a name of spaces is a shape it will see."""
    from services.catalog_enrichment_agent import ingestion as ing

    rec = _variant_record(2)
    for v in rec["pdp"]["variants"]:
        v["option_name"] = "   "
    variants = json.loads(ing.ingest_validated_record(rec)["seeds"][0]["seed_data"])["variants"]
    assert [v["options"] for v in variants] == [[], []]
