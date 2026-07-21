from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_repo_root))

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/pivota_test")
os.environ.setdefault("READINESS_ALLOW_UNAUTHED_DEV", "true")

# The (retired) alpha test-rig merchant the golden fixtures were captured
# from. Fixture-only: readiness/flags.py no longer bakes in a default (the
# runtime-hardcode guard forbids real merchant ids in runtime files); tests
# set READINESS_ALPHA_MERCHANT_ID to this value explicitly.
ALPHA_MERCHANT_ID = "merch_efbc46b4619cfbdf"


def load_real_merchant_fixture() -> dict:
    fixture_path = _repo_root / "readiness" / "fixtures" / "real_merchant_alpha_shopify.json"
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def build_live_shopify_products() -> list:
    from models.standard_product import StandardProduct

    fixture = load_real_merchant_fixture()
    products = []
    for row in fixture["products_cache_rows"]:
        product_data = deepcopy(row["product_data"])
        products.append(StandardProduct(**product_data))
    return products


def build_review_summaries() -> tuple[dict, dict, list[str], list[str]]:
    return (
        {
            "9886500749640": {
                "scope": "product",
                "source": "reviews_center.review_group.v1",
                "default_view": "group",
                "has_group": True,
                "has_reviews": True,
                "group_id": 2101,
                "group_key": "gtin:8801234567890",
                "group_confidence": 0.97,
                "membership_confidence": 0.91,
                "review_count": 27,
                "rating_count": 25,
                "average_rating": 4.72,
                "verified_review_count": 21,
                "featured_review_count": 6,
                "latest_review_at": "2026-03-16T10:00:00Z",
            },
            "9886500127048": {
                "scope": "product",
                "source": "reviews_center.product_reviews.v1",
                "default_view": "merchant",
                "has_group": False,
                "has_reviews": True,
                "group_id": None,
                "group_key": None,
                "group_confidence": None,
                "membership_confidence": None,
                "review_count": 8,
                "rating_count": 8,
                "average_rating": 4.5,
                "verified_review_count": 3,
                "featured_review_count": 0,
                "latest_review_at": "2026-03-14T08:30:00Z",
            },
        },
        {
            "integration_status": "ready",
            "observed_at": "2026-03-17T12:00:00Z",
            "products_with_reviews": 2,
            "grouped_products_with_reviews": 1,
            "products_without_reviews": 0,
        },
        [],
        [
            "Readiness alpha now projects product-level review summaries from Reviews Center using review-group matches first and merchant/global fallbacks second."
        ],
    )
