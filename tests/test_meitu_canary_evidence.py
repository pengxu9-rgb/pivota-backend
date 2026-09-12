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
        "products": [{"product_key": "pk", "brand": "3CE", "seller_host": host,
            "merchant_id": f"merchant_{host}", "currency": "USD", "market": "US", "variant_id": "123456",
            "variant_id_provenance": "merchant_issued", "gtin": "8801234567890",
            "category_path": "beauty/makeup/lip/lipstick", "inci_source": "reseller_listing"}
            for host in ["one.example", "two.example"]],
        "search_product_keys": ["pk"], "pdp_product_keys": ["pk"], "offer_product_keys": ["pk"],
        "second_ingest_added_product_keys": [], "identity_failures": []}}


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
