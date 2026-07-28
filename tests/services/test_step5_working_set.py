"""Step-5 Lane 0 — working-set classification (pure functions, no DB).

Covers the lane assignments from
docs/plans/adr011_step5_catalog_identity_reconciliation.md: duplicate store
connection via shared source_product_id, seed/first-party twins, same-URL
collapse under normalization (querystring/UTM noise), campaign-slug clones,
multi-domain residue, and the demo / orphan-mirror exclusions.
"""

from __future__ import annotations

from typing import Any, Dict

from scripts.step5_working_set import (  # noqa: E402
    DEMO_DOMAIN_PREFIX,
    DEMO_EXCLUSION_SQL,
    DEMO_MERCHANT_IDS,
    build_demo_exclusion_sql,
    build_report,
    classify_cross_merchant_group,
    classify_same_merchant_group,
    is_demo_row,
    is_orphan_mirror_row,
)


def _row(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "merchant_id": "merch_a",
        "content_key": "ck_1",
        "product_key": "prod::merch_a::shopify::1",
        "platform": "shopify",
        "canonical_url": "https://store.example/products/thing",
        "source_domain": "store.example",
        "source_product_id": "sp_1",
        "source_ref": None,
        "pivota_signature_id": None,
        "title": "Thing",
        "created_at": "2026-07-01",
        "seed_status": None,
    }
    base.update(overrides)
    return base


class TestCrossMerchantClassification:
    def test_shared_source_product_id_is_duplicate_store_connection(self):
        rows = [
            _row(merchant_id="merch_a", source_product_id="sp_9"),
            _row(merchant_id="merch_b", source_product_id="sp_9"),
        ]
        assert classify_cross_merchant_group(rows) == "lane1_duplicate_store_connection"

    def test_seed_plus_first_party_is_twin(self):
        rows = [
            _row(merchant_id="external_seed", platform="external_seed",
                 source_product_id="seed_1", seed_status="active"),
            _row(merchant_id="merch_b", source_product_id="sp_2"),
        ]
        assert classify_cross_merchant_group(rows) == "lane4_seed_first_party_twin"

    def test_distinct_source_ids_same_platform_goes_to_review(self):
        rows = [
            _row(merchant_id="merch_a", source_product_id="sp_1"),
            _row(merchant_id="merch_b", source_product_id="sp_2"),
        ]
        assert classify_cross_merchant_group(rows) == "lane4_review_cross_merchant"

    def test_blank_source_product_ids_never_count_as_shared(self):
        rows = [
            _row(merchant_id="merch_a", source_product_id=""),
            _row(merchant_id="merch_b", source_product_id=None),
        ]
        assert classify_cross_merchant_group(rows) == "lane4_review_cross_merchant"


class TestSameMerchantClassification:
    def test_querystring_and_utm_noise_collapses_to_lane2(self):
        url = "https://www.catkin.com/products/lip-balm"
        rows = [
            _row(canonical_url=url),
            _row(canonical_url=url + "?variant=569&utm_source=pivota"),
            _row(canonical_url=url + "/"),
        ]
        assert classify_same_merchant_group(rows) == "lane2_same_url"

    def test_same_domain_distinct_slugs_are_campaign_clones(self):
        rows = [
            _row(canonical_url="https://biodance.com/products/0627_cm_a",
                 source_domain="biodance.com"),
            _row(canonical_url="https://biodance.com/products/collagen-gel-mask",
                 source_domain="biodance.com"),
        ]
        assert classify_same_merchant_group(rows) == "lane3_campaign_clones"

    def test_campaign_clones_domain_derived_from_url_when_source_domain_null(self):
        rows = [
            _row(canonical_url="https://biodance.com/products/0627_cm_a",
                 source_domain=None),
            _row(canonical_url="https://www.biodance.com/products/gel-mask",
                 source_domain=None),
        ]
        assert classify_same_merchant_group(rows) == "lane3_campaign_clones"

    def test_multi_domain_goes_to_its_own_lane(self):
        rows = [
            _row(canonical_url="https://a.example/products/x", source_domain="a.example"),
            _row(canonical_url="https://b.example/products/x", source_domain="b.example"),
        ]
        assert classify_same_merchant_group(rows) == "lane4_multi_domain"

    def test_no_urls_at_all_is_no_url_signal(self):
        rows = [
            _row(canonical_url=None),
            _row(canonical_url=""),
        ]
        assert classify_same_merchant_group(rows) == "lane4_no_url_signal"

    def test_blank_url_row_blocks_mechanical_collapse(self):
        url = "https://brand.example/products/serum"
        rows = [
            _row(canonical_url=url),
            _row(canonical_url=None),
        ]
        assert classify_same_merchant_group(rows) == "lane4_mixed_url_presence"


class TestExclusions:
    def test_demo_row_detection(self):
        assert is_demo_row(_row(source_domain="pivota-review-demo-2.myshopify.com"))
        assert not is_demo_row(_row(source_domain="store.example"))

    def test_demo_merchant_id_leg_catches_rigs_with_no_domain(self):
        """merch_test_ownist_001 has no merchant_stores row and no
        source_domain, so only the id leg can reach it (ADR-018 census)."""
        assert is_demo_row(
            _row(merchant_id="merch_test_ownist_001", source_domain=None)
        )
        assert is_demo_row(
            _row(merchant_id="merch_test_ownist_001", source_domain="store.example")
        )
        assert not is_demo_row(_row(merchant_id="merch_a", source_domain="store.example"))

    def test_demo_exclusion_sql_carries_both_legs(self):
        # SQL twin must stay in step with the Python predicate.
        assert DEMO_DOMAIN_PREFIX in DEMO_EXCLUSION_SQL
        assert DEMO_MERCHANT_IDS, "the id leg is only rendered for a non-empty set"
        for mid in DEMO_MERCHANT_IDS:
            assert "'" + mid + "'" in DEMO_EXCLUSION_SQL
        assert "merchant_id NOT IN (" in DEMO_EXCLUSION_SQL

    def test_demo_exclusion_sql_omits_id_leg_when_set_is_empty(self):
        """`merchant_id NOT IN ()` is a Postgres syntax error, and this is built
        at import time — so an empty set would import fine and only blow up
        later, inside the reconcile-sweep gauges."""
        sql = build_demo_exclusion_sql("pivota-review-demo", frozenset())
        assert "NOT IN" not in sql
        assert sql == "(COALESCE(source_domain, '') NOT LIKE 'pivota-review-demo%')"

    def test_demo_exclusion_sql_escapes_quotes_in_ids(self):
        sql = build_demo_exclusion_sql("d", frozenset({"m') OR ('1'='1"}))
        assert "''" in sql
        assert "'m') OR ('1'='1'" not in sql

    def test_orphan_mirror_detection(self):
        assert is_orphan_mirror_row(_row(platform="external_seed", seed_status="inactive"))
        assert is_orphan_mirror_row(_row(platform="external_seed", seed_status="missing"))
        assert not is_orphan_mirror_row(_row(platform="external_seed", seed_status="active"))
        assert not is_orphan_mirror_row(_row())  # non-seed rows: seed_status None


class TestBuildReport:
    def test_exclusions_shrink_groups_below_dup_threshold(self):
        # Three rows share a key, but one is an orphan mirror -> the group
        # survives as a 2-row lane2 group; the orphan is excluded, not counted.
        url = "https://brand.example/products/serum"
        working = [
            _row(merchant_id="external_seed", platform="external_seed",
                 canonical_url=url, seed_status="active", product_key="p1"),
            _row(merchant_id="external_seed", platform="external_seed",
                 canonical_url=url + "?utm_source=x", seed_status="active",
                 product_key="p2"),
            _row(merchant_id="external_seed", platform="external_seed",
                 canonical_url=url, seed_status="inactive", product_key="p3"),
        ]
        report = build_report(working, orphan_rows=[])
        assert report["summary"]["lane2_same_url"] == {"groups": 1, "rows": 2}
        assert report["summary"]["excluded_orphan_mirrors_in_groups"] == {"rows": 1}

    def test_demo_only_cross_merchant_group_disappears(self):
        working = [
            _row(merchant_id="merch_demo_1",
                 source_domain="pivota-review-demo.myshopify.com"),
            _row(merchant_id="merch_demo_2",
                 source_domain="pivota-review-demo-2.myshopify.com"),
        ]
        report = build_report(working, orphan_rows=[])
        assert report["summary"]["excluded_demo"] == {"rows": 2}
        assert not any(k.startswith("lane") for k in report["summary"]
                       if report["summary"][k].get("groups"))

    def test_pair_becoming_singleton_after_exclusion_is_not_a_group(self):
        working = [
            _row(merchant_id="external_seed", platform="external_seed",
                 seed_status="active", product_key="p1"),
            _row(merchant_id="external_seed", platform="external_seed",
                 seed_status="missing", product_key="p2"),
        ]
        report = build_report(working, orphan_rows=[])
        assert not any(k.startswith("lane") for k in report["summary"]
                       if report["summary"][k].get("groups"))
