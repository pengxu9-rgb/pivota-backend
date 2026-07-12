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


def external_seed_product(**overrides):
    # Catalog row that mirrors an external_seed source (third-party scrape).
    # Identity gates apply to these because the merchant is NOT the source of
    # truth — see c1.v0.3 first-party carve-out for the contrast.
    base = active_merchant_product(
        product_key="pk_seed_1",
        content_key="ck_seed_1",
        merchant_id="external_seed",
        platform="external_seed",
        source_system="external_product_seeds",
        source_ref="ext_4242",
        source_product_id="ext_4242",
        source_domain="theordinary.com",
    )
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


def call_external_seed(**overrides):
    inputs = {
        "subject_type": "product",
        "subject_key": "pk_seed_1",
        "product": external_seed_product(),
        "identity": approved_identity(source_listing_ref="external_seed:ext_4242"),
        "ips": eligible_ips(),
        "external_seed": active_external_seed(),
        "now": NOW,
    }
    inputs.update(overrides)
    return derive_trust(inputs)


def observed_seller_product(**overrides):
    # ADR-009: an external seed mirrored under its per-brand observed seller
    # (merch_obs_…) instead of the legacy 'external_seed' merchant bucket.
    base = external_seed_product(
        product_key="pk_obs_1",
        content_key="ck_obs_1",
        merchant_id="merch_obs_8887b6c53f029191",
        source_domain="goongbe.us",
    )
    base.update(overrides)
    return base


def call_observed_seller(**overrides):
    inputs = {
        "subject_type": "product",
        "subject_key": "pk_obs_1",
        "product": observed_seller_product(),
        "identity": approved_identity(source_listing_ref="merch_obs_8887b6c53f029191:ext_4242"),
        "ips": eligible_ips(),
        "external_seed": active_external_seed(),
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


def test_retired_test_rig_merchant_store_blocks():
    """retired_test_rig is a decommissioned demo/test store — it must be treated
    as inactive (was falling through to 'unknown' and serving demo junk)."""
    trust = call(
        merchant_store=active_merchant_store(status="retired_test_rig"),
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


# ---- SHADOW (the 580-violation gate, external_seed cohort) ------------------


def test_external_seed_no_identity_row_shadows_with_identity_confidence_null():
    # 504 of audit's 580 — IPS-eligible external mirror rows without identity row.
    trust = call_external_seed(identity=None)
    assert trust["serving_decision"] == "shadow"
    assert trust["identity_status"] == "unknown"
    assert REASON_CODES.IDENTITY_CONFIDENCE_NULL in trust["serving_reason_codes"]


def test_external_seed_review_required_with_live_read_shadows():
    # The 60 cases.
    trust = call_external_seed(
        identity=approved_identity(
            source_listing_ref="external_seed:ext_4242",
            identity_status="review_required",
            live_read_enabled=True,
            review_required=True,
        ),
    )
    assert trust["serving_decision"] == "shadow"
    assert trust["identity_status"] == "review_required"
    assert REASON_CODES.IDENTITY_REVIEW_REQUIRED_LIVE_READ in trust["serving_reason_codes"]


def test_external_seed_approved_with_live_read_disabled_shadows():
    trust = call_external_seed(
        identity=approved_identity(source_listing_ref="external_seed:ext_4242", live_read_enabled=False),
    )
    assert trust["serving_decision"] == "shadow"
    assert REASON_CODES.IDENTITY_LIVE_READ_DISABLED in trust["serving_reason_codes"]


def test_external_seed_approved_with_null_confidence_shadows():
    trust = call_external_seed(
        identity=approved_identity(source_listing_ref="external_seed:ext_4242", identity_confidence=None),
    )
    assert trust["serving_decision"] == "shadow"
    assert REASON_CODES.IDENTITY_CONFIDENCE_NULL in trust["serving_reason_codes"]


def test_external_seed_approved_with_review_required_flag_set_degrades_to_shadow():
    trust = call_external_seed(
        identity=approved_identity(source_listing_ref="external_seed:ext_4242", review_required=True),
    )
    assert trust["serving_decision"] == "shadow"
    assert trust["identity_status"] == "review_required"


# ---- IPS-NULL EXTERNAL_SEED BLOCK (c1.v0.4) ---------------------------------
#
# Phase 3c parity surfaced 80 external_seed catalog rows with public trust but
# no index_pipeline_state row. c1.v0.4 closes this: external_seed catalogs
# require IPS to opine (existence + serving_eligible=True). First-party rows
# keep the legacy "ips=None means OK" behavior because IPS doesn't process
# them by design.


def test_external_seed_no_ips_row_is_blocked_with_index_not_serving_eligible():
    trust = call_external_seed(ips=None)
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE in trust["serving_reason_codes"]


def test_external_seed_ips_present_and_eligible_remains_public():
    trust = call_external_seed()
    assert trust["serving_decision"] == "public"
    assert REASON_CODES.PUBLIC_PASSTHROUGH in trust["serving_reason_codes"]


def test_external_seed_ips_present_but_not_eligible_still_blocks():
    trust = call_external_seed(ips=eligible_ips(serving_eligible=False))
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE in trust["serving_reason_codes"]


def test_first_party_no_ips_row_remains_public_under_c1_v0_4():
    # MOYU/GR/PawStyle case — IPS coverage is sparse for first-party merchants
    # by design. c1.v0.4 only tightens the IPS gate for external_seed.
    trust = call(ips=None)
    assert trust["serving_decision"] == "public"
    assert REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE not in trust["serving_reason_codes"]


def test_first_party_ips_not_eligible_still_blocks_unchanged():
    trust = call(ips=eligible_ips(serving_eligible=False))
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE in trust["serving_reason_codes"]


# ---- FIRST-PARTY CARVE-OUT (c1.v0.3) ----------------------------------------
#
# Internal merchants (anything that's not external_seed) are the source of
# truth for their own products. The identity pipeline exists to verify scraped
# third-party content; first-party merchants get a separate, looser gate.


def test_first_party_no_identity_row_is_public_with_first_party_advisory():
    # Reproduces the MOYU/GR test-merchant case: no pdp_identity_listing row,
    # IPS eligible, sync_status=live. Legacy gates shadowed these; c1.v0.3
    # serves them.
    trust = call(identity=None)
    assert trust["serving_decision"] == "public"
    assert trust["identity_status"] == "unknown"
    assert REASON_CODES.IDENTITY_NOT_APPLICABLE_FIRST_PARTY in trust["serving_reason_codes"]
    assert REASON_CODES.IDENTITY_CONFIDENCE_NULL not in trust["serving_reason_codes"]


def test_first_party_approved_with_null_confidence_is_public():
    trust = call(identity=approved_identity(identity_confidence=None))
    assert trust["serving_decision"] == "public"
    assert REASON_CODES.IDENTITY_NOT_APPLICABLE_FIRST_PARTY in trust["serving_reason_codes"]
    assert REASON_CODES.IDENTITY_CONFIDENCE_NULL not in trust["serving_reason_codes"]


# ---- ADR-009 OBSERVED-SELLER TIER (Option C) --------------------------------
#
# merch_obs_ observed sellers are external-seed CONTENT (subject to the index/
# quality gate, like the legacy 'external_seed' lump) but are the brand's own
# authoritative D2C crawl (exempt from the identity-COVERAGE shadow gates, like
# a first-party merchant). Hard identity gates still apply.


def test_observed_seller_no_ips_row_is_blocked_like_external_seed():
    # Gate 1 applies: external-seed content with no IPS must not serve, even
    # under a merch_obs_ merchant (closes the c1.v0.4 hole for observed sellers).
    trust = call_observed_seller(ips=None)
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE in trust["serving_reason_codes"]


def test_observed_seller_ips_present_and_eligible_remains_public():
    trust = call_observed_seller()
    assert trust["serving_decision"] == "public"
    assert REASON_CODES.PUBLIC_PASSTHROUGH in trust["serving_reason_codes"]


def test_observed_seller_ips_not_eligible_still_blocks():
    trust = call_observed_seller(ips=eligible_ips(serving_eligible=False))
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE in trust["serving_reason_codes"]


def test_observed_seller_no_identity_row_is_public_exempt_from_coverage_gate():
    # The key Option C behavior: unlike the legacy 'external_seed' lump (which
    # shadows with IDENTITY_CONFIDENCE_NULL), an observed seller's own D2C crawl
    # is authoritative and exempt from the identity-coverage shadow gate.
    trust = call_observed_seller(identity=None)
    assert trust["serving_decision"] == "public"
    assert trust["identity_status"] == "unknown"
    assert REASON_CODES.IDENTITY_NOT_APPLICABLE_FIRST_PARTY in trust["serving_reason_codes"]
    assert REASON_CODES.IDENTITY_CONFIDENCE_NULL not in trust["serving_reason_codes"]


def test_observed_seller_approved_with_null_confidence_is_public():
    trust = call_observed_seller(identity=approved_identity(identity_confidence=None))
    assert trust["serving_decision"] == "public"
    assert REASON_CODES.IDENTITY_NOT_APPLICABLE_FIRST_PARTY in trust["serving_reason_codes"]
    assert REASON_CODES.IDENTITY_CONFIDENCE_NULL not in trust["serving_reason_codes"]


def test_observed_seller_identity_conflict_still_blocks():
    # Hard gates are NOT exempted — a real identity conflict still blocks.
    trust = call_observed_seller(identity=approved_identity(identity_status="conflict"))
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.IDENTITY_CONFLICT in trust["serving_reason_codes"]


def test_observed_seller_review_required_still_shadows():
    trust = call_observed_seller(identity=approved_identity(review_required=True))
    assert trust["serving_decision"] != "public"
    assert REASON_CODES.IDENTITY_REVIEW_REQUIRED_LIVE_READ in trust["serving_reason_codes"]


def test_first_party_approved_with_live_read_disabled_is_public():
    trust = call(identity=approved_identity(live_read_enabled=False))
    assert trust["serving_decision"] == "public"
    assert REASON_CODES.IDENTITY_LIVE_READ_DISABLED not in trust["serving_reason_codes"]


def test_first_party_review_required_still_gates_to_shadow():
    trust = call(
        identity=approved_identity(
            identity_status="review_required",
            live_read_enabled=True,
            review_required=True,
        ),
    )
    assert trust["serving_decision"] == "shadow"
    assert REASON_CODES.IDENTITY_REVIEW_REQUIRED_LIVE_READ in trust["serving_reason_codes"]


def test_first_party_identity_conflict_still_blocks():
    trust = call(identity=approved_identity(identity_status="conflict"))
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.IDENTITY_CONFLICT in trust["serving_reason_codes"]


def test_first_party_hard_gates_still_block_regardless_of_identity():
    tombstoned = call(
        product=active_merchant_product(suppression_reason="manual_takedown"),
        identity=None,
    )
    assert tombstoned["serving_decision"] == "blocked"
    assert REASON_CODES.ROW_TOMBSTONED in tombstoned["serving_reason_codes"]

    ips_blocked = call(
        identity=None,
        ips=eligible_ips(serving_eligible=False),
    )
    assert ips_blocked["serving_decision"] == "blocked"
    assert REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE in ips_blocked["serving_reason_codes"]

    archived = call(
        product=active_merchant_product(sync_status="archived"),
        identity=None,
    )
    assert archived["serving_decision"] == "blocked"
    assert REASON_CODES.PUBLISH_STATE_NOT_PUBLIC in archived["serving_reason_codes"]


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


# ---- ADR-008 SLICE 1: INDEX_ELIGIBLE_READ widening --------------------------


def test_index_eligible_read_off_keeps_offer_free_blocked(monkeypatch):
    """Flag OFF (default): a row that is index_eligible but not serving_eligible
    is still blocked (serving-only gate, byte-identical to today)."""
    monkeypatch.delenv("INDEX_ELIGIBLE_READ", raising=False)
    trust = call(ips=eligible_ips(serving_eligible=False, index_eligible=True))
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE in trust["serving_reason_codes"]


def test_index_eligible_read_on_admits_offer_free_index_eligible_row(monkeypatch):
    """Flag ON: an index_eligible (offer-free) row is no longer blocked by the
    index-pipeline gate."""
    monkeypatch.setenv("INDEX_ELIGIBLE_READ", "true")
    trust = call(ips=eligible_ips(serving_eligible=False, index_eligible=True))
    assert trust["serving_decision"] != "blocked"
    assert REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE not in trust["serving_reason_codes"]


def test_index_eligible_read_on_still_blocks_when_neither_eligible(monkeypatch):
    monkeypatch.setenv("INDEX_ELIGIBLE_READ", "on")
    trust = call(ips=eligible_ips(serving_eligible=False, index_eligible=False))
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE in trust["serving_reason_codes"]
