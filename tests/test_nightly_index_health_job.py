"""Unit tests for the nightly commerce index health job.

Covers the pure-logic surface of jobs/nightly_index_health_job:
  - _classify_product: stage, blocker, serving_eligible decisions
  - _resolve_seed_title: snapshot vs top-level vs row title shapes
  - _coerce_dict: defensive JSON coercion
  - _make_extraction_content_hash: stable hash invariants
  - Quality threshold scale (0-100, not 0-1)
  - Stale product invalidation in the classifier
  - public_indexed reachable via pdp_lifecycle_stage='published'
  - Scorecard recovery/regression decision logic via the same helpers

DB-touching paths (batch upsert, scorecard SQL) are exercised against the
real database in the deployment smoke test, not here — mocking asyncpg
adds more brittleness than confidence.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from jobs.nightly_index_health_job import (
    MIN_DESCRIPTION_LENGTH,
    QUALITY_SCORE_THRESHOLD,
    _IPS_TOTAL_COUNT_QUERY,
    _ORPHAN_COUNT_QUERY,
    _ORPHAN_DELETE,
    _REGRESSION_BLOCKER_UPDATE,
    _STALE_INVALIDATION_UPDATE,
    _BATCH_QUERY,
    _classify_product,
    _coerce_dict,
    _extract_domain,
    _is_non_core_product,
    _make_extraction_content_hash,
    _resolve_seed_title,
    _select_content_key_state,
)


# ---------------------------------------------------------------------------
# Quality threshold scale (P0)
# ---------------------------------------------------------------------------

def test_quality_threshold_is_on_0_to_100_scale():
    """Regression guard: content_quality_score is 0-100 per services/product_quality_service.py.

    A 0-1 threshold would treat a 1.0 score as passing — that was the P0 bug.
    """
    assert QUALITY_SCORE_THRESHOLD >= 1.0, (
        f"QUALITY_SCORE_THRESHOLD={QUALITY_SCORE_THRESHOLD} looks like 0-1 scale; "
        "content_quality_score is 0-100 (see product_quality_service.py:556)"
    )


def _full_row(**overrides):
    """Baseline 'eligible' product row; tests override specific fields to test branches."""
    base = {
        "content_key": "ck_test_001",
        "product_key": "pk_001",
        "pivota_signature_id": "sig_001",
        "merchant_id": "merch_001",
        "platform": "shopify",
        "source_product_id": "prod_001",
        "pdp_scope": "canonical",
        "sync_status": "live",
        "canonical_url": "https://example.com/p/test",
        "content_quality_score": 80.0,  # 0-100 scale
        "model_readiness_score": 70.0,
        "conversion_potential_score": None,
        "quality_rules_version": "v1",
        "quality_scored_at": datetime(2026, 5, 20, tzinfo=timezone.utc),
        "pdp_title": "Test product",
        "image_url": "https://cdn.example.com/img.jpg",
        "pdp_description": "x" * 200,  # > MIN_DESCRIPTION_LENGTH
        "pdp_sync_status": "live",
        "pdp_lifecycle_stage": "published",
        "refreshed_at": datetime(2026, 5, 20, tzinfo=timezone.utc),
        "seed_data_json": {"title": "Test product", "review_summary": {"review_status": "no_issues_found"}},
        "seed_title": "Test product",
        "has_price": True,
        "has_offer_snapshot": True,
        "product_group_id": "pg_001",
    }
    base.update(overrides)
    return base


def test_baseline_eligible_product_classified_correctly():
    state = _classify_product(_full_row(), regression_domains=set())
    assert state["serving_eligible"] is True
    assert state["pipeline_stage"] == "public_indexed"
    assert state["blocker_code"] == "none"
    assert state["content_quality_score"] == 80.0


def test_connected_merchant_with_no_active_store_blocks_serving():
    """A connected merchant (has ≥1 store) whose stores are ALL non-active
    (retired_test_rig / inactive / disconnected) must not serve, even if the
    product itself is otherwise fully eligible."""
    state = _classify_product(
        _full_row(merchant_has_store=True, merchant_has_active_store=False),
        regression_domains=set(),
    )
    assert state["blocker_code"] == "merchant_store_inactive"
    assert state["serving_eligible"] is False
    assert state["index_eligible"] is False


def test_connected_merchant_with_active_store_still_serves():
    """The store guard must not fire when the merchant has an active store."""
    state = _classify_product(
        _full_row(merchant_has_store=True, merchant_has_active_store=True),
        regression_domains=set(),
    )
    assert state["blocker_code"] == "none"
    assert state["serving_eligible"] is True


def test_external_seed_without_store_row_is_exempt_from_store_guard():
    """CRITICAL: external seeds (and merch_obs_ observed sellers) have NO
    merchant_stores row → merchant_has_store=False → the store guard must NOT
    fire, so the whole external-seed cohort keeps serving."""
    state = _classify_product(
        _full_row(merchant_has_store=False, merchant_has_active_store=False),
        regression_domains=set(),
    )
    assert state["blocker_code"] == "none"
    assert state["serving_eligible"] is True
    assert state["index_eligible"] is True


def test_quality_score_exactly_at_threshold_passes():
    state = _classify_product(
        _full_row(content_quality_score=QUALITY_SCORE_THRESHOLD),
        regression_domains=set(),
    )
    assert state["serving_eligible"] is True
    assert state["blocker_code"] == "none"


def test_quality_score_below_threshold_blocks():
    state = _classify_product(
        _full_row(content_quality_score=QUALITY_SCORE_THRESHOLD - 0.1),
        regression_domains=set(),
    )
    assert state["serving_eligible"] is False
    assert state["blocker_code"] == "low_quality"


def test_quality_score_of_1_point_0_is_blocked_low_quality():
    """Catches the original P0: with 0-1 scale, 1.0 would pass; with 0-100, it's terrible."""
    state = _classify_product(
        _full_row(content_quality_score=1.0),
        regression_domains=set(),
    )
    assert state["serving_eligible"] is False
    assert state["blocker_code"] == "low_quality"
    assert "1.0" in (state["blocker_detail"] or "")


# ---------------------------------------------------------------------------
# Stale product invalidation (P0)
# ---------------------------------------------------------------------------

def test_stale_product_blocked_as_not_live():
    state = _classify_product(_full_row(sync_status="stale"), regression_domains=set())
    assert state["serving_eligible"] is False
    assert state["blocker_code"] == "not_live"
    assert "stale" in (state["blocker_detail"] or "")


def test_archived_product_blocked_as_not_live():
    state = _classify_product(_full_row(sync_status="archived"), regression_domains=set())
    assert state["serving_eligible"] is False
    assert state["blocker_code"] == "not_live"


def test_not_live_takes_precedence_over_other_blockers():
    """Even if quality is high and image present, sync_status!='live' wins."""
    state = _classify_product(
        _full_row(sync_status="archived", content_quality_score=10.0, image_url=""),
        regression_domains=set(),
    )
    assert state["blocker_code"] == "not_live"


def test_non_core_sample_card_blocks_before_seed_or_quality_checks():
    state = _classify_product(
        _full_row(
            seed_data_json=None,
            seed_title=None,
            pdp_title="Find Comfort Sample Card",
            pdp_description="",
            content_quality_score=None,
            has_price=False,
        ),
        regression_domains=set(),
    )
    assert state["serving_eligible"] is False
    assert state["blocker_code"] == "non_core_product"
    assert "not eligible" in (state["blocker_detail"] or "")


def test_non_core_shipping_protection_blocks_even_with_good_content():
    state = _classify_product(
        _full_row(
            seed_data_json={"title": "Shipping Protection"},
            seed_title="Shipping Protection",
            pdp_title="Shipping Protection",
            canonical_url="https://example.com/products/narvardeliveryprotection",
        ),
        regression_domains=set(),
    )
    assert state["serving_eligible"] is False
    assert state["blocker_code"] == "non_core_product"


def test_non_core_detector_stays_narrow_for_regular_products():
    row = _full_row(
        pdp_title="Sampled Iris Eau de Parfum",
        canonical_url="https://example.com/products/sampled-iris-eau-de-parfum",
    )
    assert _is_non_core_product(row, seed_title="Sampled Iris Eau de Parfum") is False


def test_non_core_detector_allows_real_sticker_treatments():
    """Blemish/spot/undereye stickers are legitimate beauty PDPs, not decals."""
    for title in (
        "Clarity Blemish Stickers",
        "Overnight Spot Stickers",
        "Showstopp'r Undereye Sticker",
    ):
        row = _full_row(
            pdp_title=title,
            canonical_url=f"https://example.com/products/{title.lower().replace(' ', '-')}",
        )
        assert _is_non_core_product(row, seed_title=title) is False


def test_non_core_detector_blocks_decal_stickers():
    row = _full_row(
        pdp_title="Rare Beauty Decal Stickers",
        canonical_url="https://example.com/products/rare-beauty-decal-stickers",
    )
    assert _is_non_core_product(row, seed_title="Rare Beauty Decal Stickers") is True


# ---------------------------------------------------------------------------
# Public_indexed mapping (P1)
# ---------------------------------------------------------------------------

def test_public_indexed_requires_pdp_lifecycle_published():
    """apv.sync_status is live/stale/archived, NOT 'public' — public_indexed must use lifecycle_stage."""
    state = _classify_product(
        _full_row(pdp_lifecycle_stage="published"),
        regression_domains=set(),
    )
    assert state["pipeline_stage"] == "public_indexed"


def test_eligible_without_published_lifecycle_is_shadow_indexed():
    state = _classify_product(
        _full_row(pdp_lifecycle_stage="validated"),  # eligible but not published
        regression_domains=set(),
    )
    assert state["serving_eligible"] is True
    assert state["pipeline_stage"] == "shadow_indexed"


def test_apv_sync_status_public_no_longer_promotes_to_public_indexed():
    """Regression guard: a row with apv.sync_status='public' (which never happens
    in practice) must NOT be classified as public_indexed — that was the P1 bug."""
    state = _classify_product(
        _full_row(pdp_sync_status="public", pdp_lifecycle_stage="validated"),
        regression_domains=set(),
    )
    assert state["pipeline_stage"] != "public_indexed"


# ---------------------------------------------------------------------------
# Seed shape compatibility (P1)
# ---------------------------------------------------------------------------

def test_resolve_seed_title_prefers_snapshot_title():
    seed = {"snapshot": {"title": "Snapshot Title"}, "title": "Top Title"}
    assert _resolve_seed_title(seed, {"seed_title": "Row Title"}) == "Snapshot Title"


def test_resolve_seed_title_falls_back_to_row_title():
    seed = {"snapshot": {}}  # no snapshot title
    assert _resolve_seed_title(seed, {"seed_title": "Row Title"}) == "Row Title"


def test_resolve_seed_title_falls_back_to_top_level_title():
    seed = {"title": "Top Title"}
    assert _resolve_seed_title(seed, {"seed_title": None}) == "Top Title"


def test_resolve_seed_title_empty_when_all_blank():
    assert _resolve_seed_title({}, {}) == ""
    assert _resolve_seed_title({"title": "   "}, {"seed_title": ""}) == ""


def test_snapshot_shaped_seed_data_is_extracted_not_no_extraction():
    """Regression guard: rows with seed_data.snapshot.title but no top-level title
    were classified as 'no_extraction'. That was the P1 bug."""
    seed_data = {"snapshot": {"title": "Real product name"}}
    state = _classify_product(
        _full_row(seed_data_json=seed_data, seed_title=None),
        regression_domains=set(),
    )
    assert state["blocker_code"] != "no_extraction"
    # With the seed title resolved, it should reach the stage that other checks allow
    assert state["pipeline_stage"] in ("extracted", "quality_gated", "shadow_indexed", "public_indexed")


def test_seed_data_as_json_string_is_parsed():
    seed_data = '{"snapshot": {"title": "Parsed from string"}}'
    state = _classify_product(
        _full_row(seed_data_json=seed_data, seed_title=None),
        regression_domains=set(),
    )
    assert state["blocker_code"] != "no_seed"
    assert state["blocker_code"] != "no_extraction"


# ---------------------------------------------------------------------------
# Other blockers (regression coverage)
# ---------------------------------------------------------------------------

def test_missing_seed_blocks_as_no_seed():
    state = _classify_product(
        _full_row(seed_data_json=None, seed_title=None, pdp_title="", pdp_description=""),
        regression_domains=set(),
    )
    assert state["blocker_code"] == "no_seed"


def test_no_seed_apv_backed_product_can_be_indexed():
    """Merchant/APV-backed PDPs are valid source documents even without an
    external seed. Regression guard for the production commerce-index gap where
    good APV rows were stuck at no_seed forever."""
    state = _classify_product(
        _full_row(
            seed_data_json=None,
            seed_title=None,
            pdp_scope="merchant_owned",
            product_group_id=None,
            pdp_title="Catalog/APV product",
            pdp_description="A source-backed PDP description with enough detail for the index.",
            pdp_lifecycle_stage="validated",
        ),
        regression_domains=set(),
    )
    assert state["serving_eligible"] is True
    assert state["blocker_code"] == "none"
    assert state["pipeline_stage"] == "shadow_indexed"
    assert state["identity_resolved"] is True
    assert state["seed_audit_status"] == "not_audited"


def test_no_seed_apv_backed_product_without_quality_is_low_quality_not_no_seed():
    state = _classify_product(
        _full_row(
            seed_data_json=None,
            seed_title=None,
            content_quality_score=None,
            pdp_scope="merchant_owned",
            product_group_id=None,
            pdp_title="Catalog/APV product",
            pdp_description="A source-backed PDP description with enough detail for the index.",
        ),
        regression_domains=set(),
    )
    assert state["serving_eligible"] is False
    assert state["blocker_code"] == "low_quality"
    assert state["pipeline_stage"] == "extracted"


def test_content_key_state_selection_prefers_apv_backed_eligible_row():
    """Duplicate live catalog rows for one content_key must not let a weaker
    source row overwrite the content-key PDP state."""
    blocked = _classify_product(
        _full_row(
            seed_data_json=None,
            seed_title=None,
            pdp_title="Catalog title only",
            pdp_description="",
            content_quality_score=57.1,
        ),
        regression_domains=set(),
    )
    eligible = _classify_product(
        _full_row(
            seed_data_json=None,
            seed_title=None,
            pdp_scope="merchant_owned",
            product_group_id=None,
            pdp_title="Catalog/APV product",
            pdp_description="A source-backed PDP description with enough detail for the index.",
            pdp_lifecycle_stage="validated",
            content_quality_score=71.4,
        ),
        regression_domains=set(),
    )

    selected = _select_content_key_state([blocked, eligible])
    assert selected["blocker_code"] == "none"
    assert selected["serving_eligible"] is True
    assert selected["pipeline_stage"] == "shadow_indexed"


def test_no_seed_without_apv_document_still_blocks_as_no_seed():
    state = _classify_product(
        _full_row(
            seed_data_json=None,
            seed_title=None,
            pdp_title="Catalog title only",
            pdp_description="",
        ),
        regression_domains=set(),
    )
    assert state["blocker_code"] == "no_seed"


def test_missing_image_blocks_as_no_image():
    state = _classify_product(_full_row(image_url=""), regression_domains=set())
    assert state["blocker_code"] == "no_image"


def test_missing_price_blocks_as_no_price():
    state = _classify_product(_full_row(has_price=False), regression_domains=set())
    assert state["blocker_code"] == "no_price"


def test_short_description_blocks():
    state = _classify_product(
        _full_row(pdp_description="too short"),
        regression_domains=set(),
    )
    assert state["blocker_code"] == "short_description"


def test_entity_unresolved_blocks():
    state = _classify_product(
        _full_row(product_group_id=None, pdp_scope="unverified"),
        regression_domains=set(),
    )
    assert state["blocker_code"] == "entity_unresolved"


def test_multi_merchant_scope_resolves_identity_without_group_member():
    state = _classify_product(
        _full_row(product_group_id=None, pdp_scope="multi_merchant_canonical"),
        regression_domains=set(),
    )
    assert state["identity_resolved"] is True
    assert state["blocker_code"] == "none"


def test_merchant_owned_scope_resolves_identity_without_group_member():
    state = _classify_product(
        _full_row(product_group_id=None, pdp_scope="merchant_owned"),
        regression_domains=set(),
    )
    assert state["identity_resolved"] is True
    assert state["blocker_code"] == "none"


def test_seed_audit_issues_unresolved_blocks():
    state = _classify_product(
        _full_row(seed_data_json={
            "title": "Test",
            "review_summary": {"review_status": "issues_unresolved"},
        }),
        regression_domains=set(),
    )
    assert state["blocker_code"] == "seed_audit_fail"


def test_retailer_review_status_does_not_block_seed_audit_gate():
    state = _classify_product(
        _full_row(seed_data_json={
            "title": "Test",
            "review_summary": {
                "review_status": "aggregate_only_no_public_preview_text",
                "review_policy": "source_backed_retailer_public_aggregate",
            },
        }),
        regression_domains=set(),
    )
    assert state["seed_audit_status"] == "not_audited"
    assert state["blocker_code"] == "none"
    assert state["serving_eligible"] is True


def test_extractor_regression_blocks_at_domain_level():
    state = _classify_product(
        _full_row(canonical_url="https://bad-domain.com/p/x"),
        regression_domains={"bad-domain.com"},
    )
    assert state["blocker_code"] == "extractor_regression"
    assert state["serving_eligible"] is False


def test_extractor_regression_not_applied_for_unrelated_domain():
    state = _classify_product(
        _full_row(canonical_url="https://good.com/p/x"),
        regression_domains={"bad-domain.com"},
    )
    assert state["blocker_code"] == "none"
    assert state["serving_eligible"] is True


# ---------------------------------------------------------------------------
# Stage progression (no eligibility)
# ---------------------------------------------------------------------------

def test_discovered_when_no_seed_no_offer_snapshot():
    state = _classify_product(
        _full_row(
            seed_data_json=None,
            seed_title=None,
            pdp_title="",
            pdp_description="",
            has_offer_snapshot=None,
        ),
        regression_domains=set(),
    )
    assert state["pipeline_stage"] == "discovered"


def test_crawled_when_offer_snapshot_but_no_seed():
    state = _classify_product(
        _full_row(
            seed_data_json=None,
            seed_title=None,
            pdp_title="",
            pdp_description="",
            has_offer_snapshot=True,
        ),
        regression_domains=set(),
    )
    assert state["pipeline_stage"] == "crawled"


def test_extracted_when_seed_title_but_no_quality():
    state = _classify_product(
        _full_row(content_quality_score=None),
        regression_domains=set(),
    )
    assert state["pipeline_stage"] == "extracted"


def test_quality_gated_when_quality_passes_but_no_pdp_refresh():
    """All eligibility criteria met, but no apv row → stage caps at quality_gated."""
    state = _classify_product(
        _full_row(refreshed_at=None),
        regression_domains=set(),
    )
    # serving_eligible doesn't require an apv row, so it's still True;
    # but pipeline_stage tops out at quality_gated until the next apv backfill.
    assert state["pipeline_stage"] == "quality_gated"
    assert state["serving_eligible"] is True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_coerce_dict_handles_string_json():
    assert _coerce_dict('{"a": 1}') == {"a": 1}


def test_coerce_dict_handles_garbage():
    assert _coerce_dict("not json") == {}
    assert _coerce_dict(None) == {}
    assert _coerce_dict(123) == {}
    assert _coerce_dict('[1,2,3]') == {}  # list, not dict


def test_extract_domain_strips_scheme_and_www():
    assert _extract_domain("https://www.example.com/path") == "example.com"
    assert _extract_domain("http://api.example.com:8080/x?y=1") == "api.example.com:8080"
    assert _extract_domain("") is None
    assert _extract_domain(None) is None


def test_ips_total_count_query_is_simple_count():
    """The pre-delete IPS count drives the orphan-share warning threshold;
    must be an unconditional COUNT(*) so it isn't subject to filter drift."""
    sql = _IPS_TOTAL_COUNT_QUERY.strip().upper()
    assert sql.startswith("SELECT COUNT(*)")
    assert "INDEX_PIPELINE_STATE" in sql
    assert "WHERE" not in sql


def test_orphan_queries_target_index_pipeline_state_via_anti_join():
    """The orphan-handling SQL must DELETE FROM index_pipeline_state and gate
    on absence of a catalog_products.content_key match. Regression guard that
    the catalog prune path's hard-deletes can't leave stale eligible rows."""
    for sql in (_ORPHAN_COUNT_QUERY, _ORPHAN_DELETE):
        assert "index_pipeline_state" in sql
        assert "NOT EXISTS" in sql
        assert "catalog_products" in sql
        assert "content_key" in sql
    assert _ORPHAN_DELETE.lstrip().upper().startswith("DELETE")
    assert _ORPHAN_COUNT_QUERY.lstrip().upper().startswith("SELECT")


def test_batch_query_pages_by_distinct_content_key():
    """IPS is keyed by content_key, so the batch must page keys first and then
    classify all live catalog rows for those keys."""
    sql = _BATCH_QUERY
    assert "WITH content_keys AS" in sql
    assert "SELECT DISTINCT cp.content_key" in sql
    assert "JOIN catalog_products cp" in sql
    assert "ORDER BY cp.content_key, cp.updated_at DESC NULLS LAST" in sql


def test_stale_invalidation_targets_sync_status_not_live():
    """The stale-invalidation UPDATE must guard on sync_status != 'live' and
    flip serving_eligible to FALSE. Regression guard for P0 #2."""
    assert _STALE_INVALIDATION_UPDATE.lstrip().upper().startswith("UPDATE")
    assert "index_pipeline_state" in _STALE_INVALIDATION_UPDATE
    assert "serving_eligible" in _STALE_INVALIDATION_UPDATE
    assert "FALSE" in _STALE_INVALIDATION_UPDATE
    assert "'not_live'" in _STALE_INVALIDATION_UPDATE
    assert "IS DISTINCT FROM 'live'" in _STALE_INVALIDATION_UPDATE
    assert "NOT EXISTS" in _STALE_INVALIDATION_UPDATE
    assert "live_cp.sync_status = 'live'" in _STALE_INVALIDATION_UPDATE


def test_extraction_content_hash_is_deterministic():
    h1 = _make_extraction_content_hash("a", "b", "c")
    h2 = _make_extraction_content_hash("a", "b", "c")
    h3 = _make_extraction_content_hash("a", "b", "d")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# ADR-008 SLICE 1: index_eligible (offer-free citation floor)
# ---------------------------------------------------------------------------

def test_index_eligible_true_for_offer_free_product_meeting_quality_image_identity():
    """A product with quality+image+identity+description but NO price is
    index_eligible (citable) yet NOT serving_eligible (no buyable offer)."""
    state = _classify_product(_full_row(has_price=False), regression_domains=set())
    assert state["index_eligible"] is True
    assert state["serving_eligible"] is False
    # The serving blocker short-circuits at no_price; index_eligible must NOT be
    # derived from blocker_code (it ignores the price predicate).
    assert state["blocker_code"] == "no_price"


def test_serving_eligible_still_requires_has_price_unchanged():
    """Regression assertion: serving_eligible MUST still require has_price.
    ADR-008 is strictly additive — it does not relax the serving gate."""
    with_price = _classify_product(_full_row(has_price=True), regression_domains=set())
    without_price = _classify_product(_full_row(has_price=False), regression_domains=set())
    assert with_price["serving_eligible"] is True
    assert without_price["serving_eligible"] is False
    # The only flipped input is has_price.
    assert with_price["index_eligible"] is True
    assert without_price["index_eligible"] is True


def test_full_product_is_both_serving_and_index_eligible():
    state = _classify_product(_full_row(), regression_domains=set())
    assert state["serving_eligible"] is True
    assert state["index_eligible"] is True


def test_index_eligible_requires_has_image_even_offer_free():
    state = _classify_product(
        _full_row(has_price=False, image_url=""), regression_domains=set()
    )
    assert state["index_eligible"] is False


def test_index_eligible_false_for_low_quality_offer_free():
    state = _classify_product(
        _full_row(has_price=False, content_quality_score=10.0),
        regression_domains=set(),
    )
    assert state["index_eligible"] is False


def test_index_eligible_false_for_unresolved_identity_independent_of_blocker_code():
    """CRITICAL: with has_price False, the blocker ladder short-circuits at
    no_price BEFORE entity_unresolved. index_eligible must still be False
    because it is computed from raw predicates, not blocker_code."""
    state = _classify_product(
        _full_row(has_price=False, product_group_id=None, pdp_scope="unverified"),
        regression_domains=set(),
    )
    assert state["blocker_code"] == "no_price"  # proves the short-circuit
    assert state["identity_resolved"] is False
    assert state["index_eligible"] is False


def test_index_eligible_false_for_short_description_independent_of_blocker_code():
    state = _classify_product(
        _full_row(has_price=False, pdp_description="too short"),
        regression_domains=set(),
    )
    assert state["blocker_code"] == "no_price"  # short-circuit masks short desc
    assert state["index_eligible"] is False


def test_index_eligible_false_when_not_live():
    state = _classify_product(
        _full_row(has_price=False, sync_status="archived"),
        regression_domains=set(),
    )
    assert state["index_eligible"] is False


def test_index_eligible_false_for_non_core_product_offer_free():
    """A sample/gift/protection row must never become citable even with no
    price — non-core is re-derived from the raw signal, not blocker_code."""
    state = _classify_product(
        _full_row(
            has_price=False,
            seed_data_json={"title": "Shipping Protection"},
            seed_title="Shipping Protection",
            pdp_title="Shipping Protection",
            canonical_url="https://example.com/products/narvardeliveryprotection",
        ),
        regression_domains=set(),
    )
    assert state["index_eligible"] is False


def test_index_eligible_false_on_domain_regression():
    state = _classify_product(
        _full_row(has_price=False, canonical_url="https://bad-domain.com/p/x"),
        regression_domains={"bad-domain.com"},
    )
    assert state["index_eligible"] is False


def test_stale_invalidation_also_clears_index_eligible():
    """M6 lifecycle: a delisted product must lose index_eligible too, else it
    stays citable forever."""
    assert "index_eligible       = FALSE" in _STALE_INVALIDATION_UPDATE
    # The guard must also fire on still-index-eligible rows.
    assert "ips.index_eligible = TRUE" in _STALE_INVALIDATION_UPDATE


def test_regression_blocker_update_also_clears_index_eligible():
    """M6 lifecycle: a regressed-domain product must lose index_eligible too."""
    assert "index_eligible   = FALSE" in _REGRESSION_BLOCKER_UPDATE
    assert "serving_eligible = FALSE" in _REGRESSION_BLOCKER_UPDATE
