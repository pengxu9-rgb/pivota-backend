from hashlib import sha256
import json

import pytest

from scripts.ops.partner_discovery import coverage, handoff


RETAILER_URL = "https://retailer.example/products/lip-pencil"
BRAND_URL = "https://maker.example/products/lip-pencil"
GTIN = "00012345600012"


def encoded(value):
    return json.dumps(value).encode()


def discovery_inputs():
    source = {
        "item_id": "pdp-retailer",
        "url": RETAILER_URL,
        "associations": [
            {"source": "tracker_relationship", "relation": "alternative", "meitu_line": 1, "meitu_id": "sample-id"},
        ],
    }
    intake = [
        {"source": "tracker_original", "meitu_line": 1, "status": "needs_product_url"},
        {"source": "tracker_relationship", "item_id": source["item_id"], "normalized_url": source["url"],
         "status": "queued_for_source_capture"},
        {"source": "tracker_channel", "status": "out_of_market", "market": "CA"},
    ]
    sources = {"market": "US", "items": [source]}
    official = {
        "scope": "meitu_official_rediscovery_query", "market": "US", "batch_id": "batch-sample-us",
        "meitu_line": 1, "meitu_id": "sample-id", "host": "maker.example",
        "status": "source_access_held", "candidate_count": 0, "coverage": {"status": "blocked"},
    }
    previous = encoded(sources)
    receipt = {"market": "US", "manifest_sha256": sha256(previous).hexdigest(), "tracked_source": source,
               "evidence": {"status": "shopify_us_quote_observed"}}
    return intake, sources, official, previous, receipt


def test_intake_reconciles_original_relationship_and_held_channel():
    intake, sources, official, previous, receipt = discovery_inputs()
    captures = coverage.previous_captures(previous, [receipt])

    result = coverage.reconcile(intake, sources, [official], captures)

    assert len(result["rows"]) == 3
    assert result["summary"] == {
        "official_results": {"source_access_held": 1},
        "tracker_original": {"needs_product_url": 1},
        "tracker_relationship": {"queued_for_source_capture": 1},
        "tracker_channel": {"out_of_market": 1},
    }
    original, relationship, channel = result["rows"]
    assert original["official"]["pending"] == {"status": "blocked"}
    assert original["source_associations"][0]["capture_status"] == "shopify_us_quote_observed"
    assert relationship["capture_status"] == "shopify_us_quote_observed"
    assert channel["market"] == "CA"


def test_intake_rejects_mismatched_capture_or_official_identity():
    intake, sources, official, previous, receipt = discovery_inputs()
    with pytest.raises(ValueError, match="previous source and manifest"):
        coverage.previous_captures(previous, [{**receipt, "manifest_sha256": "0" * 64}])
    with pytest.raises(ValueError, match="one official result batch"):
        coverage.reconcile(intake, sources, [], {})
    with pytest.raises(ValueError, match="original partner product"):
        coverage.reconcile(intake, sources, [{**official, "meitu_id": "different"}], {})


def audited_quote(variant, status, *, in_stock=True):
    return {
        "variant_id": variant, "gtin": GTIN, "disposition": status, "sku": "Sample",
        "host": "retailer.example", "in_stock": in_stock, "price_minor": 1299,
    }


def cohort_item(variant, relation, catalog_status, review_required=None):
    return {
        "item_id": f"item-{variant}", "market": "US", "relation": relation,
        "retailer_url": RETAILER_URL, "retailer_product_id": "3001", "retailer_variant_id": variant,
        "gtin": GTIN, "brand_url": BRAND_URL if relation == "exact_offer" else None,
        "brand_variant_id": "4001" if relation == "exact_offer" else None,
        "catalog_status": catalog_status, "known_conflict": review_required,
    }


def offer_inputs():
    execution = "example-us-read"
    offer_rows = [
        audited_quote("1001", "serving_same_store_exact_quote"),
        audited_quote("1002", "legacy_offer_owner_review"),
        audited_quote("1003", "one_serving_gtin_anchor_review"),
        audited_quote("1004", "legacy_offer_owner_review"),
        audited_quote("1005", "no_serving_gtin_anchor"),
        audited_quote("1006", "no_serving_gtin_anchor", in_stock=False),
    ]
    audit_source = {
        "execution": execution,
        "source": {"item_id": "pdp-retailer", "url": RETAILER_URL,
                   "host": "retailer.example", "shopify_product_id": "3001"},
        "quotes": offer_rows,
    }
    audit = {
        "market": "US", "production_writes": False,
        "production_read_only_reconciliation": {
            "summary": {"market": "US", "read_only": True, "execution": execution,
                        "candidate_quotes": len(offer_rows)},
            "sources": [audit_source],
        },
    }
    cohort = {
        "scope": "meitu_us_offer_pairing_readonly", "market": "US",
        "catalog_snapshot": {
            "executions": [execution], "audited_quotes": len(offer_rows),
            "document_sha256": sha256(encoded(audit)).hexdigest(),
        },
        "items": [
            cohort_item("1001", "exact_offer", "already_covered"),
            cohort_item("1002", "source_only", "unavailable", "legacy_offer_owner_review"),
            cohort_item("1099", "exact_offer", "unavailable", "outside_audited_source"),
        ],
    }
    return audit, cohort


def test_handoff_records_all_quotes_and_keeps_proposed_work_read_only():
    audit, cohort = offer_inputs()
    raw_cohort = encoded(cohort)
    conflict = {"quote_key": [RETAILER_URL, "1005"], "review_required": "distinct_native_gtin"}

    result = handoff.build(encoded(audit), raw_cohort, [conflict])

    assert result["offer_cohort_sha256"] == sha256(raw_cohort).hexdigest()
    assert result["counts"]["audited_quotes"] == 6
    assert result["counts"]["audited_keys_in_batch"] == 2
    assert result["counts"]["by_disposition"] == {"selected": 2, "deferred": 3, "held": 1}
    assert result["counts"]["deferred_by_review_requirement"]["distinct_native_gtin"] == 1
    assert result["next_source_only_page"] == {
        "per_source_limit": 2, "approval_authority": "none", "purpose": "source_evidence_and_catalog_identity_review",
        "quote_keys": [(RETAILER_URL, "1003"), (RETAILER_URL, "1004")],
    }
    assert result["batch_items_outside_audit"] == [
        {"item_id": "item-1099", "quote_key": (RETAILER_URL, "1099"), "reason": "outside_audited_source"},
    ]


def test_handoff_refuses_unbound_audit_and_duplicate_storefront_native_quote():
    audit, cohort = offer_inputs()
    with pytest.raises(ValueError, match="catalog snapshot"):
        handoff.build(encoded(audit) + b"\n", encoded(cohort))

    report = audit["production_read_only_reconciliation"]
    record = report["sources"][0]
    alternate = {**record["source"], "url": "https://www.retailer.example/products/other-path"}
    report["sources"].append({**record, "source": alternate, "quotes": record["quotes"][:1]})
    report["summary"]["candidate_quotes"] += 1
    cohort["catalog_snapshot"]["audited_quotes"] += 1
    cohort["catalog_snapshot"]["document_sha256"] = sha256(encoded(audit)).hexdigest()
    with pytest.raises(ValueError, match="duplicate storefront native quote"):
        handoff.build(encoded(audit), encoded(cohort))
