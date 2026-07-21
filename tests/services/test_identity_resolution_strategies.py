"""ADR-010 D-2 strategy plugins — proposal shapes and review rules (no DB)."""

from __future__ import annotations

from typing import Any, Dict, List

from services.identity_resolution_strategies import (  # noqa: E402
    JUNK_URL_RE,
    base_slug,
    build_all_proposals,
    strategy_campaign_clone,
    strategy_junk_url,
    strategy_multi_seller_observation,
    strategy_same_url_dup,
    strategy_seed_first_party_twin,
)


def _detail(pk: str, url: str = "https://brand.example/products/x",
            **overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "product_key": pk,
        "merchant_id": "external_seed",
        "content_key": "ck_1",
        "platform": "external_seed",
        "canonical_url": url,
        "title": "Collagen Gel Mask",
        "source_ref": f"eps_{pk}",
        "pivota_signature_id": f"sig_{pk}",
        "created_at": "2026-06-01",
        "payload_bytes": 100,
        "group_is_primary": False,
    }
    base.update(overrides)
    return base


def _group(pks: List[str], merchant: str = "external_seed",
           platform: str = "external_seed") -> Dict[str, Any]:
    return {
        "merchant_id": merchant,
        "content_key": "ck_1",
        "rows": [{"product_key": pk, "platform": platform, "merchant_id": merchant,
                  "source_product_id": f"sp_{pk}"} for pk in pks],
    }


class TestSameUrlDup:
    def test_emits_suppress_dup_with_serving_aligned_keeper(self):
        detail = {"a": _detail("a"), "b": _detail("b", pivota_signature_id=None)}
        (p,) = strategy_same_url_dup([_group(["a", "b"])], detail)
        assert p["kind"] == "suppress_dup"
        assert p["keeper_product_key"] == "a"  # signed beats unsigned
        assert p["confidence"] == 0.99

    def test_group_with_missing_detail_is_skipped(self):
        assert strategy_same_url_dup([_group(["a", "b"])], {"a": _detail("a")}) == []


class TestCampaignClone:
    def test_all_campaign_marked_proposes_suppress(self):
        detail = {
            "a": _detail("a", "https://biodance.com/products/0627_cm_a_jhp1"),
            "b": _detail("b", "https://biodance.com/products/0627_cm_b_jhp2"),
        }
        (p,) = strategy_campaign_clone([_group(["a", "b"])], detail)
        assert p["kind"] == "suppress_dup"
        assert p["evidence"]["rule"] == "all_campaign_marked"

    def test_region_suffixes_collapse_to_one_base(self):
        detail = {
            "a": _detail("a", "https://m.example/products/brow-duo"),
            "b": _detail("b", "https://m.example/products/brow-duo-eu"),
        }
        (p,) = strategy_campaign_clone([_group(["a", "b"])], detail)
        assert p["kind"] == "suppress_dup"
        assert p["evidence"]["rule"] == "collapse_to_one_base"

    def test_ambiguous_group_becomes_label_only(self):
        # Distinct clean slugs = the mis-merge trap class -> never auto-suppress.
        detail = {
            "a": _detail("a", "https://m.example/products/luminant-cream"),
            "b": _detail("b", "https://m.example/products/remedy-cream"),
        }
        (p,) = strategy_campaign_clone([_group(["a", "b"])], detail)
        assert p["kind"] == "label_only"
        assert p["strategy"] == "campaign_clone_ambiguous"

    def test_base_slug_strips_region_and_copy(self):
        assert base_slug("brow-duo-eu") == "brow-duo"
        assert base_slug("balm-copy-3") == "balm"
        assert base_slug("balm-99") == "balm"

    def test_unit_number_suffixes_are_identity_not_clone_counters(self):
        # The Tier-3 eval caught two live mis-merges from stripping these
        # (Merit SPF-45 vs SPF-50). spf/size numbers must never collapse.
        assert base_slug("the-uniform-spf-45") != base_slug("the-uniform-spf-50")
        assert base_slug("shampoo-200ml") != base_slug("shampoo-400ml")
        assert base_slug("the-uniform-spf-50-eu") == base_slug("the-uniform-spf-50")

    def test_spf_pair_is_ambiguous_not_clone(self):
        detail = {
            "a": _detail("a", "https://meritbeauty.com/products/the-uniform-spf-50"),
            "b": _detail("b", "https://meritbeauty.com/products/the-uniform-spf-45"),
        }
        (p,) = strategy_campaign_clone([_group(["a", "b"])], detail)
        assert p["kind"] == "label_only"


class TestSeedFirstPartyTwin:
    def test_first_party_sibling_wins(self):
        g = {"merchant_id": "cross", "content_key": "ck_1", "rows": [
            {"product_key": "seed1", "platform": "external_seed",
             "merchant_id": "external_seed", "source_product_id": "s1"},
            {"product_key": "fp1", "platform": "shopify",
             "merchant_id": "merch_x", "source_product_id": "p1"},
        ]}
        (p,) = strategy_seed_first_party_twin([g])
        assert p["keeper_product_key"] == "fp1"
        assert "seed1" in p["subject_product_keys"]

    def test_audit_only_sibling_is_never_a_keeper(self):
        # The migration-139 lesson: an url_audit sibling must not trigger
        # suppression of the seed row.
        g = {"merchant_id": "cross", "content_key": "ck_1", "rows": [
            {"product_key": "seed1", "platform": "external_seed",
             "merchant_id": "external_seed", "source_product_id": "s1"},
            {"product_key": "audit1", "platform": "url_audit",
             "merchant_id": "merch_y", "source_product_id": "a1"},
        ]}
        assert strategy_seed_first_party_twin([g]) == []


class TestJunkUrl:
    def test_redirect_url_matches(self):
        assert JUNK_URL_RE.match(
            "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZ")

    def test_junk_row_suppressed_keeping_real_sibling(self):
        detail = {
            "junk": _detail("junk", "https://vertexaisearch.cloud.google.com/grounding-api-redirect/X"),
            "real": _detail("real", "https://www.bestbuy.com/site/jbl-live-770nc"),
        }
        (p,) = strategy_junk_url([_group(["junk", "real"])], detail)
        assert p["keeper_product_key"] == "real"
        assert p["strategy"] == "junk_url"

    def test_all_junk_group_not_proposed(self):
        detail = {
            "j1": _detail("j1", "https://vertexaisearch.cloud.google.com/grounding-api-redirect/X"),
            "j2": _detail("j2", "https://vertexaisearch.cloud.google.com/grounding-api-redirect/Y"),
        }
        assert strategy_junk_url([_group(["j1", "j2"])], detail) == []


class TestMultiSellerObservation:
    def test_two_real_merchants_get_labeled(self):
        g = {"content_key": "ck_1", "rows": [
            {"product_key": "a", "platform": "shopify", "merchant_id": "m1",
             "source_product_id": "s1"},
            {"product_key": "b", "platform": "shopify", "merchant_id": "m2",
             "source_product_id": "s2"},
        ]}
        (p,) = strategy_multi_seller_observation([g])
        assert p["kind"] == "label_only"
        assert p["evidence"]["merchants"] == ["m1", "m2"]

    def test_duplicate_store_connection_not_labeled(self):
        g = {"content_key": "ck_1", "rows": [
            {"product_key": "a", "platform": "shopify", "merchant_id": "m1",
             "source_product_id": "SHARED"},
            {"product_key": "b", "platform": "shopify", "merchant_id": "m2",
             "source_product_id": "SHARED"},
        ]}
        assert strategy_multi_seller_observation([g]) == []

    def test_multi_domain_seed_groups_get_labeled(self):
        g = {"merchant_id": "external_seed", "content_key": "ck_1", "rows": [
            {"product_key": "a", "platform": "external_seed",
             "merchant_id": "external_seed", "source_product_id": "s1",
             "source_domain": "theordinary.com"},
            {"product_key": "b", "platform": "external_seed",
             "merchant_id": "external_seed", "source_product_id": "s2",
             "source_domain": "ulta.com"},
        ]}
        (p,) = strategy_multi_seller_observation([], [g])
        assert p["kind"] == "label_only"
        assert p["evidence"]["domains"] == ["theordinary.com", "ulta.com"]


class TestBuildAll:
    def test_runs_every_strategy_and_dedupes_nothing_silently(self):
        detail = {"a": _detail("a"), "b": _detail("b")}
        report = {"lanes": {"lane2_same_url": [_group(["a", "b"])]}}
        out = build_all_proposals(report, detail)
        assert set(out) == {"same_url_dup", "campaign_clone",
                            "seed_first_party_twin", "junk_url",
                            "multi_seller_observation"}
        assert len(out["same_url_dup"]) == 1
