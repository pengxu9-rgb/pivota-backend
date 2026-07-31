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
        "merchant_id": "merch_first_party_seller_1",
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
        "source_listing_ref": "merch_first_party_seller_1:1",
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
        "merchant_id": "merch_first_party_seller_1",
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


def test_retailer_sourced_observed_seller_stays_shadow():
    # A RETAILER-sourced observed seller (seed_kind='cross' — a no-D2C brand
    # crawled from a marketplace, e.g. VODANA→Amazon) is NOT the brand's own
    # crawl, so it does NOT get the observed-seller public-passthrough exemption:
    # with no identity coverage it stays SHADOW, not public. (Incident 2026-07-20.)
    trust = call_observed_seller(
        product=observed_seller_product(seed_kind="cross"), identity=None
    )
    assert trust["serving_decision"] == "shadow"


def test_own_crawl_observed_seller_public_and_no_demotion_on_legacy_seed_kind():
    # A brand's OWN crawl (seed_kind='self') stays exempt → public. A missing /
    # legacy-NULL seed_kind ALSO stays exempt → public, so gating on the explicit
    # 'cross' demotes NO existing public observed-seller row.
    for sk in ("self", None):
        trust = call_observed_seller(
            product=observed_seller_product(seed_kind=sk), identity=None
        )
        assert trust["serving_decision"] == "public", f"seed_kind={sk!r} must stay public"


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


def test_first_party_no_ips_row_is_blocked_under_c1_v0_5():
    """c1.v0.5 inverted c1.v0.4's first-party carve-out, deliberately.

    The carve-out ("IPS coverage is sparse for first-party merchants by
    design") described a corpus where every first-party merchant was a retired
    test rig, already blocked upstream. The first REAL merchant-sync arrival
    (the 2026-07-29 Wix pilot) synced 20 rows with content_key NULL — rows that
    can structurally never have an IPS row — and every one went trust-public
    with no quality gate; `public_not_renderable` went red within the hour, and
    only the gateway's own fail-closed lookup kept them off the wire.

    An unscored row must not be public. The lifecycle for a fresh sync is
    blocked -> scored -> eligible -> public. If this assertion is being flipped
    back to `public`, that lifecycle is being reopened — measure the blast
    radius first (it was exactly 20 rows when the gate closed).
    """
    trust = call(ips=None)
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE in trust["serving_reason_codes"]


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


# ---- c1.v0.5: CATALOG_TRUST_RENDERABLE_GATE ---------------------------------
#
# The gap this closes: 1,825 rows were 'public' while the invariant said their
# PDP could not render. Measured live 2026-07-25 — 449 of them render perfectly
# (the invariant was wrong, fixed in services/pdp_renderability) and 1,376 serve
# a hard HTTP 500, not a shell. Blocking those 1,376 would have darkened 1,011
# products with no renderable sibling row, so it was flag-gated for the founder.
#
# P3 then made 1,375 of the 1,376 RENDER rather than hiding them (PIVOTA-Agent
# taught get_pdp_v2 the attached_product_key lane; the predicate followed). The
# gate now costs exactly ONE row. The tests below are unchanged on purpose:
# they drive pdp_route_resolvable directly, so they pin the GATE MECHANICS,
# which are what must survive the next lane that goes unrenderable.


def test_renderable_gate_off_by_default_leaves_an_unrenderable_row_public(monkeypatch):
    """Default OFF ⇒ byte-identical to c1.v0.4, even with the input present."""
    monkeypatch.delenv("CATALOG_TRUST_RENDERABLE_GATE", raising=False)
    trust = call(pdp_route_resolvable=False)
    assert trust["serving_decision"] == "public"
    assert REASON_CODES.PDP_ROUTE_UNRESOLVABLE not in trust["serving_reason_codes"]


def test_renderable_gate_on_blocks_a_row_with_no_pdp_content_route(monkeypatch):
    monkeypatch.setenv("CATALOG_TRUST_RENDERABLE_GATE", "true")
    trust = call(pdp_route_resolvable=False)
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.PDP_ROUTE_UNRESOLVABLE in trust["serving_reason_codes"]


def test_renderable_gate_on_leaves_a_renderable_row_public(monkeypatch):
    monkeypatch.setenv("CATALOG_TRUST_RENDERABLE_GATE", "on")
    trust = call(pdp_route_resolvable=True)
    assert trust["serving_decision"] == "public"
    assert REASON_CODES.PDP_ROUTE_UNRESOLVABLE not in trust["serving_reason_codes"]


def test_renderable_gate_on_is_inert_when_the_input_is_absent(monkeypatch):
    """Tri-state. A producer that has not been taught to compute the input
    supplies nothing — that must never be read as "not renderable", or every
    caller outside the upserter would mass-demote the catalog."""
    monkeypatch.setenv("CATALOG_TRUST_RENDERABLE_GATE", "1")
    trust = call()
    assert trust["serving_decision"] == "public"
    assert REASON_CODES.PDP_ROUTE_UNRESOLVABLE not in trust["serving_reason_codes"]


def test_renderable_gate_does_not_mask_an_earlier_block_reason(monkeypatch):
    """Ordering: a row already blocked upstream keeps its real reason, so the
    reason-code histogram stays diagnostic rather than collapsing onto the
    newest gate."""
    monkeypatch.setenv("CATALOG_TRUST_RENDERABLE_GATE", "1")
    trust = call(
        ips=eligible_ips(serving_eligible=False),
        pdp_route_resolvable=False,
    )
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE in trust["serving_reason_codes"]
    assert REASON_CODES.PDP_ROUTE_UNRESOLVABLE not in trust["serving_reason_codes"]


# ---- c1.v0.6: OFFER_PRICE_MISSING -------------------------------------------
#
# The gap this closes is GRAIN, not a wrong predicate.
# `index_pipeline_state.has_price` was always right — it asked about an
# unsuppressed, priced catalog_offers row belonging to the catalog_products row
# in front of it. But index_pipeline_state is keyed by CONTENT_KEY and stores
# the best sibling's state, while trust is keyed by PRODUCT_KEY and every
# product_key mints its own pivota_signature_id — its own public PDP. A
# price-less row sharing a content_key with a priced sibling therefore read the
# sibling's serving_eligible=true and published a price-less page.
#
# Measured on prod 2026-07-31: exactly 4 rows, all Tom Ford fragrances, each
# with one unsuppressed offer whose list_price, merchant_effective_price and
# estimated_best_price were ALL NULL — drained by the 2026-07-30 currency
# remediation without being suppressed. 2,535 further rows also lack a priced
# offer of their own and every one is already blocked upstream, which is why
# this gate ships ungated: its entire blast radius is those 4 rows.
#
# The SQL that computes the input is gated separately, on the production
# dialect, in tests/test_priced_offer_gate_postgres.py. These pin the MECHANICS.


def test_price_gate_blocks_a_row_with_no_priced_offer_of_its_own():
    trust = call(row_has_priced_offer=False)
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.OFFER_PRICE_MISSING in trust["serving_reason_codes"]


def test_price_gate_leaves_a_priced_row_public():
    trust = call(row_has_priced_offer=True)
    assert trust["serving_decision"] == "public"
    assert REASON_CODES.OFFER_PRICE_MISSING not in trust["serving_reason_codes"]


def test_price_gate_is_inert_when_the_input_is_absent():
    """Tri-state, and the reason it must be.

    Only ``services/catalog_row_trust_upserter`` computes this input. Reading an
    ABSENT input as "not priced" would mass-demote the catalog the first time
    any other producer called derive_trust.
    """
    trust = call()
    assert trust["serving_decision"] == "public"
    assert REASON_CODES.OFFER_PRICE_MISSING not in trust["serving_reason_codes"]


def test_price_gate_ignores_a_falsy_but_not_false_input():
    """`is False`, not `not row_has_priced_offer`. None must fall through."""
    trust = call(row_has_priced_offer=None)
    assert trust["serving_decision"] == "public"
    assert REASON_CODES.OFFER_PRICE_MISSING not in trust["serving_reason_codes"]


def test_price_gate_does_not_mask_an_earlier_block_reason():
    """Ordering: the index gate answers for the content_key and runs FIRST, so a
    row already blocked there keeps reporting INDEX_NOT_SERVING_ELIGIBLE. If this
    ever flips, the reason-code histogram collapses onto the newest gate and the
    two grains become indistinguishable in the data."""
    trust = call(
        ips=eligible_ips(serving_eligible=False),
        row_has_priced_offer=False,
    )
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE in trust["serving_reason_codes"]
    assert REASON_CODES.OFFER_PRICE_MISSING not in trust["serving_reason_codes"]


def test_price_gate_applies_to_external_seed_supply():
    """The 4 prod rows are external-seed mirror rows, so the lane that actually
    regressed must be covered — not just the first-party fixture."""
    trust = call_external_seed(row_has_priced_offer=False)
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.OFFER_PRICE_MISSING in trust["serving_reason_codes"]


def test_offer_price_missing_is_in_the_reason_vocabulary():
    """The vocabulary is the contract readers validate against."""
    from services.catalog_trust_policy import REASON_CODE_VOCABULARY

    assert REASON_CODES.OFFER_PRICE_MISSING in REASON_CODE_VOCABULARY


def test_policy_version_is_pinned_to_the_node_twin():
    """A version MISMATCH between the twins is the catastrophic failure mode.

    Both repos write the same ``catalog_row_trust`` table against one Postgres,
    and the UPSERT refreshes a row whenever
    ``policy_version <> EXCLUDED.policy_version``. pivota-backend stamps rows
    from a 6h cron; PIVOTA-Agent stamps them from prod RUNTIME (pdpIdentityGraph
    calls upsertCatalogRowTrustForSourceListingRefs on every live-read promotion
    and identity override). A split-brain therefore rewrites ~14k rows forever
    and makes /__trust_health's version_distribution a permanent false alarm.

    Nothing pinned this string before, in either repo — reverting the bump left
    every test green. Bump it here AND in
    PIVOTA-Agent src/services/catalogTrustPolicy.js, and merge the two PRs back
    to back (backend first).

    c1.v0.5 -> c1.v0.6 on 2026-07-31 for the OFFER_PRICE_MISSING gate, which
    flips 4 measured prod rows 'public' -> 'blocked' and so is a real logic
    change by the module's own versioning rule.

    c1.v0.6 -> c1.v0.7 same day for the canonical-election gate
    (NON_CANONICAL_DUPLICATE), which moves 121 measured prod rows
    'public' -> 'shadow'.

    🚨 THE PAIRED PIVOTA-Agent PR IS NOT OPTIONAL AND IS NOT JUST THIS STRING.
    The twin must also (a) select the per-row priced-offer EXISTS in
    src/services/catalogRowTrustUpserter.js — the mirror of
    services/priced_offer_sql.priced_offer_exists_sql('cp.product_key') — and
    (b) add the same tri-state gate to catalogTrustPolicy.js, immediately after
    its index gate. Ship only this bump and the twin keeps re-deriving those 4
    price-less PDPs 'public' on its next identity event for them.
    """
    assert POLICY_VERSION == "c1.v0.7"


# ---- TEST/DEMO MERCHANT GATE (2026-07-27) -----------------------------------
#
# Closes the Regime B gap from the ADR-018 census: before this arm, the only
# thing keeping a rig out of 'public' HERE was suppression data, not policy.


def test_rig_merchant_that_would_otherwise_be_public_is_blocked():
    trust = call(product=active_merchant_product(merchant_id="merch_test_ownist_001"))
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.TEST_MERCHANT_EXCLUDED in trust["serving_reason_codes"]


def test_every_baked_in_rig_id_is_blocked_by_the_gate():
    from services.test_merchant_policy import KNOWN_TEST_MERCHANT_IDS

    for rig_id in KNOWN_TEST_MERCHANT_IDS:
        trust = call(product=active_merchant_product(merchant_id=rig_id))
        assert trust["serving_decision"] == "blocked", f"{rig_id} should be blocked"
        assert REASON_CODES.TEST_MERCHANT_EXCLUDED in trust["serving_reason_codes"], rig_id


def test_already_blocked_rig_keeps_its_real_reason():
    """Pins the no-POLICY_VERSION-bump argument: an already-blocked rig must keep
    reporting its REAL reason, so output stays byte-identical on every row that
    exists in prod today (all 1,561 rig rows are already 'blocked')."""
    trust = call(
        product=active_merchant_product(
            merchant_id="merch_test_ownist_001",
            suppression_reason="demo_retired_2026_07",
        ),
    )
    assert trust["serving_decision"] == "blocked"
    assert trust["source_lifecycle_state"] == "tombstoned"
    assert REASON_CODES.ROW_TOMBSTONED in trust["serving_reason_codes"]
    assert REASON_CODES.TEST_MERCHANT_EXCLUDED not in trust["serving_reason_codes"]


def test_non_rig_merchant_is_unaffected():
    trust = call()
    assert trust["serving_decision"] == "public"
    assert REASON_CODES.TEST_MERCHANT_EXCLUDED not in trust["serving_reason_codes"]


def test_env_hatch_does_not_affect_the_trust_gate(monkeypatch):
    """catalog_row_trust is shared state written by both twins; a per-service env
    var would make them disagree and flap rows public<->blocked."""
    monkeypatch.setenv("PIVOTA_TEST_MERCHANT_IDS", "merch_env_only_rig")
    trust = call(product=active_merchant_product(merchant_id="merch_env_only_rig"))
    assert trust["serving_decision"] == "public"
    assert REASON_CODES.TEST_MERCHANT_EXCLUDED not in trust["serving_reason_codes"]


def test_rig_in_the_shadow_lane_is_blocked():
    """The transition the no-POLICY_VERSION-bump argument assumes is EMPTY in
    prod, made explicit here so the assumption is testable rather than implied.

    identity_status='review_required' shadows every row — there is no
    first-party/observed-seller exemption from it (unlike the identity-COVERAGE
    gates). So it is the one reachable path by which a rig could have been
    'shadow' rather than 'blocked'. Census 2026-07-28, grouped by
    serving_decision (not filtered to 'blocked'): all 1,561 rig rows are
    'blocked', zero public AND zero shadow — which is what makes the output
    byte-identical on every row that exists today. This test pins the behaviour
    for the day one is not."""
    trust = call(
        product=active_merchant_product(merchant_id="merch_test_ownist_001"),
        identity=approved_identity(identity_status="review_required"),
    )
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.TEST_MERCHANT_EXCLUDED in trust["serving_reason_codes"]


# ---- c1.v0.7: NON_CANONICAL_DUPLICATE (the grain bridge) --------------------
#
# index_pipeline_state is keyed by content_key and stores ONE row's state;
# catalog_row_trust is keyed by product_key. content_canonical_election (mig 181)
# elects the ONE sig per content_key that the sitemap advertises and that every
# sibling's PDP names in <link rel="canonical">. Nothing connected the two, so a
# non-elected sibling inherited the content-grained verdict and was promoted as
# though it were the canonical.
#
# Measured on prod 2026-07-31: 121 of 6,814 trust-public rows are not their
# content_key's elected canonical, ALL on multi-row content_keys. The 4 Tom Ford
# rows behind OFFER_PRICE_MISSING were 4 of those 121 — the election had already
# picked the priced tomfordbeauty.com row correctly in every case, which is why
# this gate is the general rule and the price gate is now a backstop beneath it.


def test_non_canonical_duplicate_shadows_rather_than_blocks():
    """SHADOW, not blocked, and the distinction is the whole design.

    The PDP RENDERER gates on index_pipeline_state.serving_eligible (content
    grain); public recall/discovery/feed gate on
    catalog_row_trust.serving_decision='public' (row grain). Shadow drops the
    duplicate out of promotion while its page keeps answering 200 with its
    rel=canonical intact. Blocking would 404 URLs Google may already have
    indexed AND destroy the canonical signal consolidating them onto the winner
    — strictly worse than the duplicate itself.
    """
    trust = call(row_is_elected_canonical=False)
    assert trust["serving_decision"] == "shadow"
    assert REASON_CODES.NON_CANONICAL_DUPLICATE in trust["serving_reason_codes"]


def test_elected_canonical_stays_public():
    trust = call(row_is_elected_canonical=True)
    assert trust["serving_decision"] == "public"
    assert REASON_CODES.NON_CANONICAL_DUPLICATE not in trust["serving_reason_codes"]


def test_absent_election_never_demotes():
    """32 multi-row content_keys still have NO election row, and the join
    legitimately yields NULL for them. Unlike the other tri-states in this
    module, None here is a normal production value, not just a hand-built-test
    artifact — reading it as "not canonical" would shadow every uncovered row."""
    assert call()["serving_decision"] == "public"
    assert call(row_is_elected_canonical=None)["serving_decision"] == "public"


def test_non_canonical_duplicate_does_not_mask_a_hard_block():
    """Ordering: the election gate lives in the SHADOW block, which is only
    reached after every hard block has passed. A tombstoned duplicate keeps
    reporting ROW_TOMBSTONED."""
    trust = call(
        row_is_elected_canonical=False,
        product=active_merchant_product(suppression_reason="demo_retired_2026_07"),
    )
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.ROW_TOMBSTONED in trust["serving_reason_codes"]


def test_price_gate_still_blocks_a_non_elected_duplicate():
    """Defence in depth. OFFER_PRICE_MISSING is a hard block and runs FIRST, so
    a duplicate that is also price-less stays blocked rather than being softened
    to shadow. If this ever inverts, the 4 Tom Ford rows quietly return to a
    rendering state with no price."""
    trust = call(row_is_elected_canonical=False, row_has_priced_offer=False)
    assert trust["serving_decision"] == "blocked"
    assert REASON_CODES.OFFER_PRICE_MISSING in trust["serving_reason_codes"]


def test_non_canonical_duplicate_applies_to_external_seed_supply():
    trust = call_external_seed(row_is_elected_canonical=False)
    assert trust["serving_decision"] == "shadow"
    assert REASON_CODES.NON_CANONICAL_DUPLICATE in trust["serving_reason_codes"]


def test_non_canonical_duplicate_is_in_the_reason_vocabulary():
    from services.catalog_trust_policy import REASON_CODE_VOCABULARY

    assert REASON_CODES.NON_CANONICAL_DUPLICATE in REASON_CODE_VOCABULARY
