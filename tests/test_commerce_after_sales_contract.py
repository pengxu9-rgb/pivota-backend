from datetime import datetime, timezone

import pytest

from services.commerce_after_sales_contract import normalize_after_sales_fact


def test_normalizes_return_policy_with_expiry():
    row = normalize_after_sales_fact({"merchant_id": "m1", "fact_kind": "return_policy", "source_url": "https://shop.test/policies/returns", "source_system": "public_policy", "confidence": 0.9, "value": {"return_window_days": 30}})
    assert row["review_required"] is True
    assert row["fresh_until"] > row["observed_at"]


def test_rejects_unattributable_after_sales_review_sample():
    with pytest.raises(ValueError, match="three attributable"):
        normalize_after_sales_fact({"merchant_id": "m1", "fact_kind": "after_sales_review_summary", "source_url": "https://shop.test/reviews", "source_system": "public_reviews", "confidence": 0.6, "value": {"sample_size": 2}})


def test_rejects_raw_review_body_field():
    with pytest.raises(ValueError, match="unsupported"):
        normalize_after_sales_fact({"merchant_id": "m1", "fact_kind": "after_sales_review_summary", "source_url": "https://shop.test/reviews", "source_system": "public_reviews", "confidence": 0.6, "value": {"sample_size": 3, "review_body": "secret"}})
