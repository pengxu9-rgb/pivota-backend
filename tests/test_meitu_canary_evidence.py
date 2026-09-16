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
            "category_path": "beauty/makeup/lip/lipstick", "inci_source": "reseller_listing",
            "inci_row": {"present": True, "source_system": "reseller_listing", "raw_inci_chars": 412}}
            for host in ["one.example", "two.example"]],
        "search_product_keys": ["pk_one.example", "pk_two.example"],
        "pdp_product_keys": ["pk_one.example", "pk_two.example"],
        "offer_product_keys": ["pk_one.example", "pk_two.example"],
        "offers": [{"product_key": f"pk_{host}", "merchant_id": f"merchant_{host}", "seller_host": host,
                    "variant_id": "123456", "currency": "USD", "market": "US",
                    "destination_url": f"https://{host}/products/lipstick"}
                   for host in ["one.example", "two.example"]],
        "second_ingest_added_product_keys": [], "second_ingest_added_sku_keys": [],
        "second_ingest_added_offer_keys": [], "identity_failures": [],
        "evidence_provenance": {"collector": "collect_curated_canary_evidence/v1",
                                "collected_at": NOW.isoformat(), "backend_revision": "abc"}}}


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


def _with_category(data, path):
    for product in data["same_brand"]["products"]:
        product["category_path"] = path
    return data


def test_a_case_that_declares_no_shelf_is_still_held_to_the_lip_shelf():
    """The Meitu cohort is lip-only. A case that forgets to declare a category must not
    thereby accept every category — the default has to be the strict one."""
    result = evaluate(MANIFEST, _with_category(evidence(), "beauty/skincare/cleanser"), now=NOW)
    assert result["failed"] == 1
    assert any("beauty/makeup/lip/" in reason for reason in result["cases"][0]["reasons"])


def test_a_case_may_declare_another_shelf_and_is_then_held_to_that_one():
    case = dict(MANIFEST["cases"][0], required_category_prefix="beauty/skincare/cleanse/")
    manifest = {"cases": [case]}
    passing = _with_category(evidence(), "beauty/skincare/cleanse/cleanser")
    assert evaluate(manifest, passing, now=NOW)["passed"] == 1
    # Strictly: declaring a cleanse shelf does not loosen the case into accepting anything
    # else — not even a sibling skincare leaf.
    assert evaluate(manifest, _with_category(evidence(), "beauty/makeup/lip/oil"), now=NOW)["failed"] == 1
    assert evaluate(manifest, _with_category(evidence(), "beauty/skincare/treat/serum"), now=NOW)["failed"] == 1


def test_a_blank_shelf_declaration_is_absent_not_permissive():
    """'' would make startswith() true for every path, turning a typo into a silent
    accept-all. Blank is treated as undeclared, so the lip default applies."""
    for blank in ("", "   ", None):
        case = dict(MANIFEST["cases"][0], required_category_prefix=blank)
        result = evaluate({"cases": [case]}, _with_category(evidence(), "beauty/skincare/cleanser"), now=NOW)
        assert result["failed"] == 1, f"blank prefix {blank!r} accepted a non-lip product"


def test_a_prefix_must_match_at_the_START_and_by_case():
    """`in` instead of startswith, or a casefolded compare, would accept a path that merely
    CONTAINS the shelf — e.g. a mis-rooted 'x/beauty/makeup/lip/oil'."""
    for path in ("x/beauty/makeup/lip/oil", "BEAUTY/MAKEUP/LIP/oil", "beauty/makeup/lipstick"):
        result = evaluate(MANIFEST, _with_category(evidence(), path), now=NOW)
        assert result["failed"] == 1, f"{path!r} was accepted for the lip shelf"


def test_a_declared_shelf_must_be_a_real_taxonomy_shelf():
    """Unbounded, 'beauty/' admits every leaf and 'b' reaches other verticals — a typo would
    switch this check off as surely as a blank value would."""
    for bogus in ("beauty/", "b", "/", "beauty/skincare", "beauty/skincare/cleanse"):
        case = dict(MANIFEST["cases"][0], required_category_prefix=bogus)
        result = evaluate({"cases": [case]}, evidence(), now=NOW)
        assert result["failed"] == 1, f"{bogus!r} was accepted as a shelf"
        assert any("not a taxonomy shelf" in r for r in result["cases"][0]["reasons"]), bogus


def test_the_shipped_manifest_is_evaluable_and_each_case_keeps_its_own_shelf():
    """A case missing a field the validator reads raises rather than returning a verdict, and a
    bad target_gtin makes a case permanently unpassable — so the shipped file is checked here,
    through evaluate(), not by restating the JSON."""
    import json
    from pathlib import Path

    from scripts.validate_meitu_canary_evidence import canonical_gtin

    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "data/review_canaries/meitu_brand_retailer_matrix.json").read_text())
    ids = [case["case_id"] for case in manifest["cases"]]
    assert len(ids) == len(set(ids)), "duplicate case_id"

    for case in manifest["cases"]:
        for field in ("accepted_brands", "seller_hosts", "market", "currency", "inci_source"):
            assert case.get(field), f"{case['case_id']} is missing {field}"
        if case.get("target_gtin"):
            assert canonical_gtin(case["target_gtin"]), f"{case['case_id']} target_gtin fails GS1"
        declared = case.get("required_category_prefix")
        assert declared is None or isinstance(declared, str) and declared.endswith("/"), case["case_id"]

    # Evaluated for real: the lip cohort still REJECTS a skincare product through the shipped file.
    assert evaluate({"cases": [c for c in manifest["cases"] if c["case_id"] == "apieu_two_us_retailers"]},
                    {}, now=NOW)["pending"] == 1
    by_id = {case["case_id"]: case for case in manifest["cases"]}
    assert "required_category_prefix" not in by_id["apieu_two_us_retailers"]
    assert by_id["pyunkang_yul_two_us_retailers"]["required_category_prefix"] == "beauty/skincare/cleanse/"
    assert by_id["pyunkang_yul_two_us_retailers"]["seller_hosts"] == ["eyurs.com", "ohlolly.com"]


def test_the_shipped_lip_case_rejects_a_skincare_product():
    """The regression that matters: declaring a shelf elsewhere must not loosen the lip cases."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "data/review_canaries/meitu_brand_retailer_matrix.json").read_text())
    lip_case = dict([c for c in manifest["cases"] if c["case_id"] == "apieu_two_us_retailers"][0],
                    case_id="same_brand", accepted_brands=["3CE"],
                    seller_hosts=["one.example", "two.example"], target_gtin=None)
    data = _with_category(evidence(), "beauty/skincare/cleanse/cleanser")
    result = evaluate({"cases": [lip_case]}, data, now=NOW)
    assert result["failed"] == 1
    assert any("beauty/makeup/lip/" in r for r in result["cases"][0]["reasons"])


def test_evidence_without_collector_provenance_cannot_pass():
    """Every field here is otherwise typeable by hand; provenance names who produced it."""
    data = evidence()
    del data["same_brand"]["evidence_provenance"]
    result = evaluate(MANIFEST, data, now=NOW)
    assert result["failed"] == 1
    assert any("provenance" in reason for reason in result["cases"][0]["reasons"])

    for blanked in ("collector", "collected_at", "backend_revision"):
        data = evidence()
        data["same_brand"]["evidence_provenance"][blanked] = "  "
        assert evaluate(MANIFEST, data, now=NOW)["failed"] == 1, blanked


def test_a_declared_ingredient_authority_needs_a_stored_row_behind_it():
    """The curated mapper stamps inci_source on EVERY retailer record, while the row is written
    only when the seller published ingredients. Without this, a product whose PDP carries no INCI
    passes — measured 2026-09-16, the A'PIEU lip oil at eyurs.com is exactly that product."""
    data = evidence()
    del data["same_brand"]["products"][0]["inci_row"]
    assert evaluate(MANIFEST, data, now=NOW)["failed"] == 1

    data = evidence()
    data["same_brand"]["products"][0]["inci_row"] = {
        "present": False, "source_system": None, "raw_inci_chars": 0}
    result = evaluate(MANIFEST, data, now=NOW)
    assert result["failed"] == 1
    assert any("no stored ingredient row" in r for r in result["cases"][0]["reasons"])


def test_a_stored_row_that_disagrees_with_the_case_cannot_pass():
    """Compared against the CASE, not a sibling field: the collector writes both the row and any
    declared source, so comparing those two to each other proves only self-consistency."""
    data = evidence()
    data["same_brand"]["products"][0]["inci_row"]["source_system"] = "brand_official"
    result = evaluate(MANIFEST, data, now=NOW)
    assert result["failed"] == 1
    assert any("not the case's authority" in r for r in result["cases"][0]["reasons"])

    # A file that agrees with ITSELF while disagreeing with the case is still refused.
    data = evidence()
    data["same_brand"]["products"][0]["inci_source"] = "brand_official"
    data["same_brand"]["products"][0]["inci_row"]["source_system"] = "brand_official"
    assert evaluate(MANIFEST, data, now=NOW)["failed"] == 1

    # A row that exists but holds no text is not authority either.
    data = evidence()
    data["same_brand"]["products"][0]["inci_row"]["raw_inci_chars"] = 0
    assert evaluate(MANIFEST, data, now=NOW)["failed"] == 1


def test_an_uncollected_surface_is_not_a_measured_absence():
    """null means the door was never asked; [] means it was asked and returned nothing. Collapsing
    them lets a never-measured surface read as a measured result."""
    data = evidence()
    data["same_brand"]["search_product_keys"] = None
    result = evaluate(MANIFEST, data, now=NOW)
    assert result["failed"] == 1
    reasons = result["cases"][0]["reasons"]
    assert any("was not collected" in r for r in reasons), reasons
    assert not any("product missing from search_product_keys" in r for r in reasons), \
        "an uncollected surface must not be reported as a measured absence"


def test_uncollected_idempotence_and_identity_failures_cannot_pass():
    """`null` here means the second ingest was never run / failures never measured. An empty list
    is the claim 'measured, and there were none'."""
    for field in ("second_ingest_added_product_keys", "identity_failures"):
        data = evidence()
        data["same_brand"][field] = None
        assert evaluate(MANIFEST, data, now=NOW)["failed"] == 1, field
