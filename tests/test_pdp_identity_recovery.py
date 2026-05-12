from services.pdp_identity_recovery import (
    build_exact_legacy_attachment_proposal,
    build_multi_merchant_group_proposals,
    build_singleton_group_proposal,
    build_stale_title_attachment_proposal,
    deterministic_product_group_id,
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
