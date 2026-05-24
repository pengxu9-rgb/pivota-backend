import inspect

from services.pdp_identity_recovery import (
    IDENTITY_LANE_APPROVED_NOT_LIVE,
    IDENTITY_LANE_LIVE_APPROVED,
    IDENTITY_LANE_MISSING,
    IDENTITY_LANE_REVIEW_REQUIRED,
    build_attached_external_seed_group_member_proposal,
    build_ext_identity_group_member_proposal,
    build_external_seed_catalog_group_member_proposal,
    build_exact_legacy_attachment_proposal,
    build_multi_merchant_group_proposals,
    build_singleton_group_proposal,
    build_stale_title_attachment_proposal,
    classify_identity_lane,
    deterministic_ext_identity_group_id,
    deterministic_product_group_id,
    fetch_identity_review_required_rows,
    make_catalog_product_key,
    parse_legacy_attached_product_key,
)


def test_parse_legacy_attached_product_key_to_catalog_product_key() -> None:
    parsed = parse_legacy_attached_product_key("merch_1|Shopify|100")

    assert parsed == {
        "merchant_id": "merch_1",
        "platform": "shopify",
        "source_product_id": "100",
        "catalog_product_key": "prod::merch_1::shopify::100",
    }


def test_singleton_group_proposal_for_internal_product_with_offer_and_no_group() -> None:
    product_key = make_catalog_product_key("merch_1", "shopify", "100")
    proposal = build_singleton_group_proposal(
        {
            "product_key": product_key,
            "merchant_id": "merch_1",
            "platform": "shopify",
            "source_product_id": "100",
            "offer_count": 1,
            "product_group_id": None,
        }
    )

    assert proposal is not None
    assert proposal.action == "upsert_product_group_member"
    assert proposal.high_confidence is True
    assert proposal.product_group_id == deterministic_product_group_id(product_key)


def test_singleton_group_proposal_skips_products_without_offers() -> None:
    proposal = build_singleton_group_proposal(
        {
            "product_key": make_catalog_product_key("merch_1", "shopify", "100"),
            "merchant_id": "merch_1",
            "platform": "shopify",
            "source_product_id": "100",
            "offer_count": 0,
            "product_group_id": None,
        }
    )

    assert proposal is None


def test_multi_merchant_exact_title_brand_builds_shared_group() -> None:
    proposals = build_multi_merchant_group_proposals(
        [
            {
                "product_key": make_catalog_product_key("merch_1", "shopify", "100"),
                "merchant_id": "merch_1",
                "platform": "shopify",
                "source_product_id": "100",
                "title": "The Ordinary Niacinamide 10% + Zinc 1%",
                "brand": "The Ordinary",
                "offer_count": 1,
                "product_group_id": None,
                "is_primary": False,
            },
            {
                "product_key": make_catalog_product_key("merch_2", "shopify", "200"),
                "merchant_id": "merch_2",
                "platform": "shopify",
                "source_product_id": "200",
                "title": "The Ordinary Niacinamide 10% + Zinc 1%",
                "brand": "The Ordinary",
                "offer_count": 1,
                "product_group_id": None,
                "is_primary": False,
            },
        ]
    )

    assert len(proposals) == 2
    assert {proposal.reason for proposal in proposals} == {"exact_title_brand_multi_merchant"}
    assert {proposal.product_group_id for proposal in proposals} == {proposals[0].product_group_id}
    assert sum(1 for proposal in proposals if proposal.is_primary) == 1
    assert all(proposal.high_confidence for proposal in proposals)


def test_multi_merchant_title_only_without_brand_is_not_high_confidence() -> None:
    proposals = build_multi_merchant_group_proposals(
        [
            {
                "product_key": make_catalog_product_key("merch_1", "shopify", "100"),
                "merchant_id": "merch_1",
                "platform": "shopify",
                "source_product_id": "100",
                "title": "Hydrating Serum",
                "brand": "",
                "offer_count": 1,
            },
            {
                "product_key": make_catalog_product_key("merch_2", "shopify", "200"),
                "merchant_id": "merch_2",
                "platform": "shopify",
                "source_product_id": "200",
                "title": "Hydrating Serum",
                "brand": "",
                "offer_count": 1,
            },
        ]
    )

    assert proposals == []


def test_multi_merchant_merge_preserves_existing_primary_group() -> None:
    existing_group_id = "pg_existing_primary"
    proposals = build_multi_merchant_group_proposals(
        [
            {
                "product_key": make_catalog_product_key("merch_1", "shopify", "100"),
                "merchant_id": "merch_1",
                "platform": "shopify",
                "source_product_id": "100",
                "title": "Brand A Exact Product",
                "brand": "Brand A",
                "offer_count": 1,
                "product_group_id": existing_group_id,
                "is_primary": True,
            },
            {
                "product_key": make_catalog_product_key("merch_2", "shopify", "200"),
                "merchant_id": "merch_2",
                "platform": "shopify",
                "source_product_id": "200",
                "title": "Brand A Exact Product",
                "brand": "Brand A",
                "offer_count": 1,
                "product_group_id": "pg_other_singleton",
                "is_primary": True,
            },
        ]
    )

    assert len(proposals) == 1
    assert proposals[0].product_group_id == existing_group_id
    assert proposals[0].product_key == make_catalog_product_key("merch_2", "shopify", "200")
    assert proposals[0].is_primary is False


def test_exact_legacy_attachment_proposal_updates_to_current_product_key() -> None:
    proposal = build_exact_legacy_attachment_proposal(
        {
            "id": "eps_1",
            "attached_product_key": "merch_1|shopify|100",
            "matched_product_key": "prod::merch_1::shopify::100",
        }
    )

    assert proposal is not None
    assert proposal.action == "repair_external_seed_attachment"
    assert proposal.high_confidence is True
    assert proposal.to_attached_product_key == "prod::merch_1::shopify::100"


def test_stale_title_attachment_proposal_repairs_old_product_id_same_merchant() -> None:
    proposal = build_stale_title_attachment_proposal(
        {
            "id": "eps_ordinary",
            "attached_product_key": "merch_efbc46b4619cfbdf|shopify|9886499864904",
            "seed_title": "The Ordinary Niacinamide 10% + Zinc 1%",
            "seed_brand": "The Ordinary",
            "candidate_product_key": "prod::merch_efbc46b4619cfbdf::shopify::10064558194985",
            "candidate_source_product_id": "10064558194985",
            "candidate_title": "The Ordinary Niacinamide 10% + Zinc 1%",
            "candidate_brand": "The Ordinary",
        }
    )

    assert proposal is not None
    assert proposal.high_confidence is True
    assert proposal.reason == "legacy_attached_key_exact_title_same_merchant"
    assert proposal.to_attached_product_key == "prod::merch_efbc46b4619cfbdf::shopify::10064558194985"


def test_stale_title_attachment_proposal_rejects_title_only_different_brand() -> None:
    proposal = build_stale_title_attachment_proposal(
        {
            "id": "eps_1",
            "attached_product_key": "merch_1|shopify|old",
            "seed_title": "Hydrating Serum",
            "seed_brand": "Brand A",
            "candidate_product_key": "prod::merch_1::shopify::new",
            "candidate_source_product_id": "new",
            "candidate_title": "Hydrating Serum",
            "candidate_brand": "Brand B",
        }
    )

    assert proposal is None


def test_attached_external_seed_group_member_proposal_uses_existing_product_group() -> None:
    proposal = build_attached_external_seed_group_member_proposal(
        {
            "id": "eps_winona",
            "external_product_id": "ext_winona_channel",
            "attached_product_key": "prod::merch_1::shopify::100",
            "product_group_id": "pg_catalog_abc",
            "existing_external_group_id": None,
        }
    )

    assert proposal is not None
    assert proposal.action == "upsert_product_group_member"
    assert proposal.high_confidence is True
    assert proposal.reason == "attached_external_seed_product_group_member"
    assert proposal.product_key == "prod::merch_1::shopify::100"
    assert proposal.product_group_id == "pg_catalog_abc"
    assert proposal.merchant_id == "external_seed"
    assert proposal.platform == "external_seed"
    assert proposal.source_product_id == "ext_winona_channel"
    assert proposal.is_primary is False


def test_attached_external_seed_group_member_proposal_skips_existing_member() -> None:
    proposal = build_attached_external_seed_group_member_proposal(
        {
            "id": "eps_winona",
            "external_product_id": "ext_winona_channel",
            "attached_product_key": "prod::merch_1::shopify::100",
            "product_group_id": "pg_catalog_abc",
            "existing_external_group_id": "pg_catalog_abc",
        }
    )

    assert proposal is None


def test_attached_external_seed_group_member_proposal_requires_current_product_key() -> None:
    proposal = build_attached_external_seed_group_member_proposal(
        {
            "id": "eps_legacy",
            "external_product_id": "ext_legacy",
            "attached_product_key": "merch_1|shopify|100",
            "product_group_id": "pg_catalog_abc",
        }
    )

    assert proposal is None


def test_external_seed_catalog_group_member_proposal_builds_singleton() -> None:
    product_key = make_catalog_product_key("external_seed", "external_seed", "ext_mac")
    proposal = build_external_seed_catalog_group_member_proposal(
        {
            "product_key": product_key,
            "merchant_id": "external_seed",
            "platform": "external_seed",
            "source_product_id": "ext_mac",
            "offer_count": 1,
            "product_group_id": None,
        }
    )

    assert proposal is not None
    assert proposal.action == "upsert_product_group_member"
    assert proposal.reason == "external_seed_catalog_missing_group"
    assert proposal.high_confidence is True
    assert proposal.product_group_id == deterministic_product_group_id(product_key)
    assert proposal.merchant_id == "external_seed"
    assert proposal.platform == "external_seed"
    assert proposal.source_product_id == "ext_mac"
    assert proposal.is_primary is True


def test_external_seed_catalog_group_member_proposal_skips_ext_identity_cluster() -> None:
    proposal = build_external_seed_catalog_group_member_proposal(
        {
            "product_key": make_catalog_product_key("external_seed", "external_seed", "ext_mac"),
            "merchant_id": "external_seed",
            "platform": "external_seed",
            "source_product_id": "ext_mac",
            "offer_count": 1,
            "product_group_id": None,
            "ext_identity_cluster_key": "ext:mac-russian-red-matte-lipstick::e470b265",
        }
    )

    assert proposal is None


def test_ext_identity_group_id_is_stable_and_key_scoped() -> None:
    key = "ext:mac-russian-red-matte-lipstick::e470b265"

    assert deterministic_ext_identity_group_id(key) == deterministic_ext_identity_group_id(key.upper())
    assert deterministic_ext_identity_group_id(key).startswith("pg_ext_")
    assert deterministic_ext_identity_group_id(key) != deterministic_ext_identity_group_id(
        "ext:mac-diva-matte-lipstick::dbee98fb"
    )


def test_ext_identity_group_member_proposal_uses_shared_identity_group() -> None:
    attached_key = "ext:mac-russian-red-matte-lipstick::e470b265"
    proposal = build_ext_identity_group_member_proposal(
        {
            "id": "seed:mac",
            "external_product_id": "mac:62c89320b830814c",
            "attached_product_key": attached_key,
            "product_key": make_catalog_product_key("external_seed", "external_seed", "mac:62c89320b830814c"),
            "product_group_id": None,
            "cluster_external_products": 2,
            "primary_rank": 1,
            "has_offer": True,
        }
    )

    assert proposal is not None
    assert proposal.action == "upsert_product_group_member"
    assert proposal.reason == "ext_identity_attached_key_group_member"
    assert proposal.high_confidence is True
    assert proposal.product_group_id == deterministic_ext_identity_group_id(attached_key)
    assert proposal.merchant_id == "external_seed"
    assert proposal.platform == "external_seed"
    assert proposal.source_product_id == "mac:62c89320b830814c"
    assert proposal.is_primary is True
    assert proposal.from_attached_product_key == attached_key


def test_ext_identity_group_member_proposal_skips_existing_correct_member() -> None:
    attached_key = "ext:mac-russian-red-matte-lipstick::e470b265"
    proposal = build_ext_identity_group_member_proposal(
        {
            "id": "seed:mac",
            "external_product_id": "mac:62c89320b830814c",
            "attached_product_key": attached_key,
            "product_key": make_catalog_product_key("external_seed", "external_seed", "mac:62c89320b830814c"),
            "product_group_id": deterministic_ext_identity_group_id(attached_key),
            "is_primary": True,
            "cluster_external_products": 2,
            "primary_rank": 1,
            "has_offer": True,
        }
    )

    assert proposal is None


def test_classify_identity_lane_missing_when_no_group_member() -> None:
    lane = classify_identity_lane(
        {
            "product_key": "prod::merch::shopify::1",
            "product_group_id": None,
            "sync_status": "live",
            "pdp_lifecycle_stage": "published",
        }
    )

    assert lane["identity_lane"] == IDENTITY_LANE_MISSING
    assert "no product_group_members" in lane["identity_lane_detail"]


def test_classify_identity_lane_approved_not_live_for_stale_catalog() -> None:
    lane = classify_identity_lane(
        {
            "product_group_id": "pg_1",
            "sync_status": "stale",
            "pdp_lifecycle_stage": "published",
        }
    )

    assert lane["identity_lane"] == IDENTITY_LANE_APPROVED_NOT_LIVE
    assert "sync_status" in lane["identity_lane_detail"]


def test_classify_identity_lane_approved_not_live_for_candidate_lifecycle() -> None:
    lane = classify_identity_lane(
        {
            "product_group_id": "pg_1",
            "sync_status": "live",
            "pdp_lifecycle_stage": "candidate",
        }
    )

    assert lane["identity_lane"] == IDENTITY_LANE_APPROVED_NOT_LIVE
    assert "pdp_lifecycle_stage" in lane["identity_lane_detail"]


def test_classify_identity_lane_review_required_takes_precedence() -> None:
    lane = classify_identity_lane(
        {
            "review_required": True,
            "review_reason": "exact_title_brand_multi_domain_review_required",
            "product_group_id": None,
            "sync_status": "live",
            "pdp_lifecycle_stage": "published",
        }
    )

    assert lane["identity_lane"] == IDENTITY_LANE_REVIEW_REQUIRED
    assert lane["identity_lane_detail"] == "exact_title_brand_multi_domain_review_required"


def test_review_required_query_collapses_resolved_ext_identity_groups() -> None:
    source = inspect.getsource(fetch_identity_review_required_rows)

    assert "cp.product_key LIKE 'ext:%' THEN cp.product_key" in source
    assert "seed_identity.attached_product_key LIKE 'ext:%'" in source
    assert "pgm.product_group_id LIKE 'pg_ext_%'" in source
    assert "COUNT(DISTINCT resolved_identity_key)" in source
    assert "COUNT(*) FILTER (WHERE resolved_identity_key IS NULL)" in source


def test_classify_identity_lane_live_approved_when_group_and_live() -> None:
    lane = classify_identity_lane(
        {
            "product_group_id": "pg_1",
            "sync_status": "live",
            "pdp_lifecycle_stage": "validated",
        }
    )

    assert lane["identity_lane"] == IDENTITY_LANE_LIVE_APPROVED
