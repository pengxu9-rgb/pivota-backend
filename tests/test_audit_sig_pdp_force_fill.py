from scripts.audit_sig_pdp_force_fill import _classify_row


def test_classifies_filled_external_seed_sig_row():
    row = {
        "product_key": "prod::external_seed::external_seed::ext_1",
        "merchant_id": "external_seed",
        "platform": "external_seed",
        "source_product_id": "ext_1",
        "pivota_signature_id": "sig_1",
        "catalog_title": "Filled Beauty Product",
        "catalog_description": (
            "A complete approved description long enough for PDP rendering with enough product "
            "specific detail to pass the force-fill description gate."
        ),
        "catalog_image_url": "https://cdn.example.com/p.jpg",
        "brand": "Demo",
        "seed_id": "seed_1",
        "seed_price_amount": 24,
        "seed_price_currency": "USD",
        "sku_count": 1,
        "offer_count": 1,
        "seed_data": {
            "external_seed_snapshot_contract": {"version": 1},
            "snapshot": {
                "image_urls": ["https://cdn.example.com/p.jpg"],
                "variants": [{"title": "50 ml"}],
                "details_sections": [{"heading": "Details", "body": "Approved detail."}],
                "pdp_how_to_use_raw": "Apply daily.",
                "raw_ingredient_text_clean": "Water, Glycerin",
            },
            "product_intel": {"status": "published", "summary": "Reviewed insight."},
            "review_summary": {"aggregate": {"rating": 4.6, "count": 120}},
        },
    }

    item = _classify_row(row)

    assert item["bucket"] == "filled"
    assert item["missing"] == []


def test_classifies_sig_row_with_missing_source_as_blocker():
    row = {
        "product_key": "prod::external_seed::external_seed::ext_missing",
        "merchant_id": "external_seed",
        "platform": "external_seed",
        "source_product_id": "ext_missing",
        "pivota_signature_id": "sig_missing",
        "catalog_title": "Missing Source",
        "catalog_description": "",
        "catalog_image_url": None,
        "seed_id": None,
        "sku_count": 0,
        "offer_count": 0,
        "seed_data": None,
    }

    item = _classify_row(row)

    assert item["bucket"] == "source_unrecoverable_blocker"
    assert "gallery" in item["missing"]
    assert "offers" in item["missing"]


def test_classifies_jsonb_strings_from_database():
    row = {
        "product_key": "prod::external_seed::external_seed::ext_json",
        "merchant_id": "external_seed",
        "platform": "external_seed",
        "source_product_id": "ext_json",
        "pivota_signature_id": "sig_json",
        "catalog_title": "JSON Beauty Product",
        "catalog_description": (
            "A complete approved description long enough for PDP rendering with enough product "
            "specific detail to pass the force-fill description gate."
        ),
        "catalog_image_url": None,
        "brand": "Demo",
        "seed_id": "seed_json",
        "seed_price_amount": 24,
        "seed_price_currency": "USD",
        "sku_count": 0,
        "offer_count": 1,
        "seed_data": """{
            "external_seed_snapshot_contract": {"version": 1},
            "snapshot": {
                "image_urls": ["https://cdn.example.com/p.jpg"],
                "variants": [{"title": "50 ml"}],
                "details_sections": [{"heading": "Details", "body": "Approved detail."}],
                "pdp_how_to_use_raw": "Apply daily.",
                "raw_ingredient_text_clean": "Water, Glycerin"
            },
            "product_intel": {"status": "published", "summary": "Reviewed insight."},
            "review_summary": {"aggregate": {"rating": 4.6, "count": 120}}
        }""",
    }

    item = _classify_row(row)

    assert item["bucket"] == "filled"
    assert item["missing"] == []


def test_counts_kb_intel_and_identity_reviews_as_mainline_evidence():
    row = {
        "product_key": "prod::external_seed::external_seed::ext_kb",
        "merchant_id": "external_seed",
        "platform": "external_seed",
        "source_product_id": "ext_kb",
        "pivota_signature_id": "sig_kb",
        "catalog_title": "KB Beauty Product",
        "catalog_description": (
            "A complete approved description long enough for PDP rendering with enough product "
            "specific detail to pass the force-fill description gate."
        ),
        "catalog_image_url": "https://cdn.example.com/p.jpg",
        "brand": "Demo",
        "seed_id": "seed_kb",
        "seed_price_amount": 24,
        "seed_price_currency": "USD",
        "sku_count": 0,
        "offer_count": 1,
        "seed_data": {
            "external_seed_snapshot_contract": {"version": 1},
            "snapshot": {
                "image_urls": ["https://cdn.example.com/p.jpg"],
                "variants": [{"title": "50 ml"}],
                "details_sections": [{"heading": "Details", "body": "Approved detail."}],
                "pdp_how_to_use_raw": "Apply daily.",
                "raw_ingredient_text_clean": "Water, Glycerin",
            },
        },
        "product_intel_kb_analysis": {
            "product_intel_v1": {
                "contract_version": "pivota.product_intel.v1",
                "product_intel_core": {"what_it_is": {"body": "Reviewed bundle."}},
            }
        },
        "identity_review_summary": {
            "aggregate": {"rating": 4.6, "review_count": 120},
            "distribution": [{"stars": 5, "count": 90}],
        },
    }

    item = _classify_row(row)

    assert item["bucket"] == "filled"
    assert item["has_product_intel_kb"] is True
    assert item["has_identity_review_summary"] is True
