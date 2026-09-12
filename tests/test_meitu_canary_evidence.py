from datetime import datetime, timedelta, timezone

from scripts.validate_meitu_canary_evidence import evaluate


NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)
MANIFEST = {"cases": [{"case_id": "same_brand", "accepted_brands": ["3CE"],
    "seller_hosts": ["one.example", "two.example"], "market": "US", "currency": "USD",
    "inci_source": "reseller_listing"}]}


def evidence():
    return {"same_brand": {"observed_at": NOW.isoformat(), "backend_revision": "abc",
        "gateway_revision": "def", "source_artifacts": ["saved-live-responses.json"],
        "crawl": {"status": "complete", "selected_products": 2},
        "products": [{"product_key": f"pk_{host}", "content_key": "ck", "product_group_id": "pg", "brand": "3CE", "seller_host": host,
            "merchant_id": f"merchant_{host}", "currency": "USD", "market": "US", "variant_id": "123456",
            "variant_id_provenance": "merchant_issued", "gtin": "8809530070499",
            "category_path": "beauty/makeup/lip/lipstick", "inci_source": "reseller_listing"}
            for host in ["one.example", "two.example"]],
        "search_product_keys": ["pk_one.example", "pk_two.example"],
        "pdp_product_keys": ["pk_one.example", "pk_two.example"],
        "offer_product_keys": ["pk_one.example", "pk_two.example"],
        "offers": [{"product_key": f"pk_{host}", "merchant_id": f"merchant_{host}", "seller_host": host,
                    "variant_id": "123456", "currency": "USD", "market": "US",
                    "destination_url": f"https://{host}/products/lipstick"}
                   for host in ["one.example", "two.example"]],
        "second_ingest_added_product_keys": [], "second_ingest_added_sku_keys": [],
        "second_ingest_added_offer_keys": [], "identity_failures": []}}


def test_missing_observations_are_pending_never_success():
    result = evaluate(MANIFEST, {}, now=NOW)
    assert result["pending"] == 1 and result["passed"] == 0 and result["live_actions_performed"] is False


def test_complete_fresh_surface_evidence_passes():
    assert evaluate(MANIFEST, evidence(), now=NOW)["passed"] == 1


def test_collapsed_sellers_stale_or_missing_surface_cannot_pass():
    data = evidence()
    data["same_brand"]["products"][1]["merchant_id"] = data["same_brand"]["products"][0]["merchant_id"]
    assert evaluate(MANIFEST, data, now=NOW)["failed"] == 1
    data = evidence()
    data["same_brand"]["observed_at"] = (NOW - timedelta(days=2)).isoformat()
    assert evaluate(MANIFEST, data, now=NOW)["failed"] == 1
    data = evidence()
    data["same_brand"]["offer_product_keys"] = []
    assert evaluate(MANIFEST, data, now=NOW)["failed"] == 1


def test_sg_case_cannot_pass_us_currency_or_partition():
    case = dict(MANIFEST["cases"][0], market="SG", currency="SGD")
    assert evaluate({"cases": [case]}, evidence(), now=NOW)["failed"] == 1


def test_exact_gtin_canary_cannot_pass_a_different_same_brand_item():
    case = dict(MANIFEST["cases"][0], target_gtin="00036000291452")
    assert evaluate({"cases": [case]}, evidence(), now=NOW)["failed"] == 1


def test_shared_product_does_not_prove_second_retailer_offer():
    data = evidence()
    data["same_brand"]["offers"].pop()
    assert evaluate(MANIFEST, data, now=NOW)["failed"] == 1


def test_wrong_retailer_destination_cannot_pass():
    data = evidence()
    data["same_brand"]["offers"][1]["destination_url"] = "https://one.example/products/lipstick"
    assert evaluate(MANIFEST, data, now=NOW)["failed"] == 1


def test_reingest_must_not_duplicate_skus_or_offers():
    for field in ("second_ingest_added_sku_keys", "second_ingest_added_offer_keys"):
        data = evidence()
        data["same_brand"][field] = ["duplicate"]
        assert evaluate(MANIFEST, data, now=NOW)["failed"] == 1


def test_same_gtin_distinct_listings_share_observed_canonical_identity():
    data = evidence()
    assert len({p["product_key"] for p in data["same_brand"]["products"]}) == 2
    assert evaluate(MANIFEST, data, now=NOW)["passed"] == 1


def test_split_group_or_content_identity_cannot_pass():
    for field in ("product_group_id", "content_key"):
        data = evidence()
        data["same_brand"]["products"][1][field] = "split_identity"
        assert evaluate(MANIFEST, data, now=NOW)["failed"] == 1


def test_missing_canonical_identity_cannot_pass():
    for field in ("product_group_id", "content_key"):
        data = evidence()
        del data["same_brand"]["products"][1][field]
        assert evaluate(MANIFEST, data, now=NOW)["failed"] == 1


def test_gtin13_and_gtin14_share_identity_and_match_target():
    data = evidence()
    data["same_brand"]["products"][1]["gtin"] = "08809530070499"
    manifest = {"cases": [dict(MANIFEST["cases"][0], target_gtin="08809530070499")]}
    assert evaluate(manifest, data, now=NOW)["passed"] == 1


def test_bad_checksum_zero_or_malformed_gtin_cannot_pass():
    for bad in ("00000000000000", "8809530070498", "499", "bad8809530070499", ""):
        data = evidence()
        for p in data["same_brand"]["products"]:
            p["gtin"] = bad
        assert evaluate(MANIFEST, data, now=NOW)["failed"] == 1


def test_buyer_facing_canonical_key_does_not_replace_listing_offer_identity():
    data = evidence()
    case = data["same_brand"]
    for p in case["products"]:
        p["canonical_product_key"] = "sig_shared"
    for field in ("search_product_keys", "pdp_product_keys", "offer_product_keys"):
        case[field] = ["sig_shared"]
    assert evaluate(MANIFEST, data, now=NOW)["passed"] == 1
    # Canonical visibility cannot stand in for either real listing's offer.
    case["offers"][1]["product_key"] = "sig_shared"
    assert evaluate(MANIFEST, data, now=NOW)["failed"] == 1
