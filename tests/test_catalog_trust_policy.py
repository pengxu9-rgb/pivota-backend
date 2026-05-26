"""Parity tests for ``services.catalog_trust_policy.derive_trust``.

Mirrors PIVOTA-Agent/tests/catalog_trust_policy.node.test.cjs case-for-case.
The fixture shapes are intentionally identical to the Node fixtures so the two
suites stay structurally aligned (see feedback_test_helper_masking_production_bug
— fixtures must not reshape data the real producers can't).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.catalog_trust_policy import (
    POLICY_VERSION,
    REASON_CODES,
    derive_trust,
)


NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)


def days_ago(n: int) -> datetime:
    return NOW - timedelta(days=n)


def active_merchant_product(**overrides):
    base = {
        "product_key": "pk_internal_1",
        "content_key": "ck_internal_1",
        "source_domain": "chydan.myshopify.com",
        "merchant_id": "merch_efbc46b4619cfbdf",
        "platform": "shopify",
        "source_system": "shopify",
        "source_ref": "gid://shopify/Product/1",
        "source_product_id": "1",
        "sync_status": "live",
        "suppression_reason": None,
        "last_seen_in_sync_at": days_ago(1),
    }
    base.update(overrides)
    return base


def approved_identity(**overrides):
    base = {
        "source_listing_ref": "merch_efbc46b4619cfbdf:1",
        "identity_status": "approved",
        "identity_confidence": 0.95,
        "live_read_enabled": True,
        "review_required": False,
        "sellable_item_group_id": "sig_1c7611cfd2520d64ad08f3c36b2ef016",
        "product_line_id": "pl_niacinamide",
        "review_family_id": "rf_niacinamide_10",
    }
    base.update(overrides)
    return base


def eligible_ips(**overrides):
    base = {
        "serving_eligible": True,
        "pipeline_stage": "serving",
        "blocker_code": None,
        "content_quality_score": 0.8,
        "quality_scored_at": days_ago(1),
        "last_extracted_at": days_ago(1),
    }
    base.update(overrides)
    return base


def active_merchant_store(**overrides):
    base = {
        "merchant_id": "merch_efbc46b4619cfbdf",
        "platform": "shopify",
        "domain": "chydan.myshopify.com",
        "status": "active",
        "last_sync": days_ago(1),
    }
    base.update(overrides)
    return base


def active_external_seed(**overrides):
    base = {
        "id": 4242,
        "status": "active",
        "domain": "theordinary.com",
        "attached_product_key": "pk_seed_1",
        "last_seen_at": days_ago(1),
    }
    base.update(overrides)
    return base


def call(**overrides):
    inputs = {
        "subject_type": "product",
        "subject_key": "pk_internal_1",
        "product": active_merchant_product(),
        "identity": approved_identity(),
        "ips": eligible_ips(),
        "merchant_store": active_merchant_store(),
        "now": NOW,
    }
    inputs.update(overrides)
    return derive_trust(inputs)


# ---- HAPPY PATH -------------------------------------------------------------


def test_approved_merchant_row_with_eligible_ips_resolves_to_public():
    trust = call()
    assert trust["serving_decision"] == "public"
    assert trust["source_lifecycle_state"] == "active"
    assert trust["identity_status"] == "approved"
    assert trust["freshness_state"] == "fresh"
    assert trust["serving_reason_codes"] == [REASON_CODES.PUBLIC_PASSTHROUGH]
    assert trust["policy_version"] == POLICY_VERSION


# ---- HARD BLOCKS ------------------------------------------------------------


def test_tombstoned_catalog_row_blocks_regardless_of_identity():
    trust = call(
        product=active_merchant_product(suppression_reason="stale_after_sync"),
    )
    assert trust["serving_decision"] == "blocked"
    assert trust["source_lifecycle_state"] == "tombstoned"
    assert REASON_CODES.ROW_TOMBSTONED in trust["serving_reason_codes"]


def test_inactive_external_seed_blocks_even_with_ips_eligible():
    trust = call(
        external_seed=active_external_seed(status="disabled"),
        merchant_store=None,
    )
    assert trust["serving_decision"] == "blocked"
    assert trust["source_lifecycle_state"] == "inactive"
    assert REASON_CODES.EXTERNAL_SEED_INACTIVE in trust["serving_reason_codes"]


def test_inactive_merchant_store_blocks():
    trust = call(
        merchant_store=active_merchant_store(status="inactive"),
    )
    assert trust["serving_decision"] == "blocked"
    assert trust["source_lifecycle_state"] == "inactive"
    assert REASON_CODES.MERCHANT_STORE_INACTIVE in trust["serving_reason_codes"]


def test_quarantined_domain_blocks():
    trust = call(
        active_quarantines=[
            {
                "match_type": "domain",
                "match_value": "CHYDAN.MyShopify.com",
                "state": "active",
                "expires_at": None,
            }
        ],
    )
    assert trust["serving_decision"] == "blocked"
    assert trust["source_lifecycle_state"] == "quarantined"
    assert REASON_CODES.SOURCE_QUARANTINED in trust["serving_reason_codes"]


def test_expired_quarantine_does_not_match():
    expired_at = NOW - timedelta(seconds=1)
    trust = call(
        now=NOW,
        active_quarantines=[
            {
                "match_type": "domain",
                "match_value": "chydan.myshopify.com",
                "state": "active",
                "expires_at": expired_at,
            }
        ],
    )
    assert trust["source_lifecycle_state"] != "quarantined"


def test_ips_not_serving_eligible_blocks():
    trust = call(ips=eligible_ips(serving_eligible=False))
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE in trust["serving_reason_codes"]


def test_sync_status_not_live_blocks():
    trust = call(product=active_merchant_product(sync_status="stale"))
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.PUBLISH_STATE_NOT_PUBLIC in trust["serving_reason_codes"]


def test_identity_conflict_blocks():
    trust = call(identity=approved_identity(identity_status="conflict"))
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.IDENTITY_CONFLICT in trust["serving_reason_codes"]


def test_suppressed_offer_blocks():
    trust = derive_trust(
        {
            "subject_type": "offer",
            "subject_key": "offer_1",
            "product": active_merchant_product(),
            "offer": {"suppression_reason": "price_anomaly"},
            "identity": approved_identity(),
            "ips": eligible_ips(),
            "merchant_store": active_merchant_store(),
            "now": NOW,
        }
    )
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.OFFER_SUPPRESSED in trust["serving_reason_codes"]


# ---- SHADOW (the 580-violation gate) ----------------------------------------


def test_no_identity_row_shadows_with_identity_confidence_null():
    # 504 of audit's 580 — IPS-eligible external mirror rows without identity row.
    trust = call(identity=None)
    assert trust["serving_decision"] == "shadow"
    assert trust["identity_status"] == "unknown"
    assert REASON_CODES.IDENTITY_CONFIDENCE_NULL in trust["serving_reason_codes"]


def test_review_required_with_live_read_shadows():
    # The 60 cases.
    trust = call(
        identity=approved_identity(
            identity_status="review_required",
            live_read_enabled=True,
            review_required=True,
        ),
    )
    assert trust["serving_decision"] == "shadow"
    assert trust["identity_status"] == "review_required"
    assert REASON_CODES.IDENTITY_REVIEW_REQUIRED_LIVE_READ in trust["serving_reason_codes"]


def test_approved_with_live_read_disabled_shadows():
    trust = call(
        identity=approved_identity(live_read_enabled=False),
    )
    assert trust["serving_decision"] == "shadow"
    assert REASON_CODES.IDENTITY_LIVE_READ_DISABLED in trust["serving_reason_codes"]


def test_approved_with_null_confidence_shadows():
    trust = call(
        identity=approved_identity(identity_confidence=None),
    )
    assert trust["serving_decision"] == "shadow"
    assert REASON_CODES.IDENTITY_CONFIDENCE_NULL in trust["serving_reason_codes"]


def test_approved_with_review_required_flag_set_degrades_to_shadow():
    trust = call(
        identity=approved_identity(review_required=True),
    )
    assert trust["serving_decision"] == "shadow"
    assert trust["identity_status"] == "review_required"


# ---- OVERRIDES --------------------------------------------------------------


def test_active_force_exact_group_override_forces_approved_confidence_1():
    trust = call(
        identity=approved_identity(
            identity_status="review_required", identity_confidence=0.2
        ),
        override={"id": "ov_99", "action_type": "force_exact_group", "active": True},
    )
    assert trust["identity_status"] == "approved"
    assert trust["identity_confidence"] == 1.0
    assert trust["serving_decision"] == "public"
    assert trust["manual_override_id"] == "ov_99"


def test_active_force_review_required_override_degrades_to_shadow():
    trust = call(
        override={"id": "ov_100", "action_type": "force_review_required", "active": True},
    )
    assert trust["identity_status"] == "review_required"
    assert trust["serving_decision"] == "shadow"


def test_inactive_override_is_ignored():
    trust = call(
        override={"id": "ov_1", "action_type": "force_review_required", "active": False},
    )
    assert trust["serving_decision"] == "public"


# ---- FRESHNESS --------------------------------------------------------------


def test_last_seen_1d_ago_is_fresh():
    trust = call(
        product=active_merchant_product(last_seen_in_sync_at=days_ago(1)),
        ips=eligible_ips(last_extracted_at=None, quality_scored_at=None),
    )
    assert trust["freshness_state"] == "fresh"


def test_last_seen_14d_ago_is_stale():
    trust = call(
        product=active_merchant_product(last_seen_in_sync_at=days_ago(14)),
        ips=eligible_ips(last_extracted_at=None, quality_scored_at=None),
    )
    assert trust["freshness_state"] == "stale"


def test_last_seen_60d_ago_is_expired():
    trust = call(
        product=active_merchant_product(last_seen_in_sync_at=days_ago(60)),
        ips=eligible_ips(last_extracted_at=None, quality_scored_at=None),
    )
    assert trust["freshness_state"] == "expired"


def test_no_timestamps_is_unverified():
    trust = call(
        product=active_merchant_product(last_seen_in_sync_at=None),
        ips=eligible_ips(last_extracted_at=None, quality_scored_at=None),
        merchant_store=active_merchant_store(last_sync=None),
    )
    assert trust["freshness_state"] == "unverified"
    assert REASON_CODES.FRESHNESS_UNVERIFIED in trust["serving_reason_codes"]


# ---- VALIDATION -------------------------------------------------------------


def test_invalid_subject_type_raises():
    with pytest.raises(ValueError, match="invalid subject_type"):
        derive_trust({"subject_type": "banana", "subject_key": "x"})


def test_missing_subject_key_raises():
    with pytest.raises(ValueError, match="subject_key is required"):
        derive_trust({"subject_type": "product", "subject_key": ""})


# ---- SHAPE CONTRACT ---------------------------------------------------------


def test_output_row_has_all_migration_columns_and_bounded_reason_vocab():
    trust = call()
    expected_keys = [
        "subject_type",
        "subject_key",
        "product_key",
        "source_listing_ref",
        "content_key",
        "source_id",
        "source_domain",
        "source_lifecycle_state",
        "source_last_checked_at",
        "identity_status",
        "identity_confidence",
        "matched_product_key",
        "matched_content_key",
        "matched_sellable_item_group_id",
        "freshness_state",
        "last_verified_at",
        "verification_source",
        "serving_decision",
        "serving_reason_codes",
        "manual_override_id",
        "policy_version",
    ]
    for k in expected_keys:
        assert k in trust, f"missing key: {k}"

    # Reason codes must come from the bounded vocabulary.
    from services.catalog_trust_policy import REASON_CODE_VOCABULARY

    for r in trust["serving_reason_codes"]:
        assert r in REASON_CODE_VOCABULARY, f"reason code not in vocabulary: {r}"
