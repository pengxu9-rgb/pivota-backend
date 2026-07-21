"""Step-5 Lane 3 — slug evidence, reviewer-editable matching (no DB)."""

from __future__ import annotations

from typing import Any, Dict, List

from scripts.step5_lane3_campaign_clone_dedup import (  # noqa: E402
    CAMPAIGN_MARKER_RE,
    SUPPRESSION_REASON,
    build_proposal,
    cleanest_member,
    match_proposal,
    slug_evidence,
    url_slug,
)


def _detail(pk: str, url: str, **overrides: Any) -> Dict[str, Any]:
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


CLEAN = "https://biodance.com/products/collagen-gel-mask"
CAMPAIGN_A = "https://biodance.com/products/0627_cm_2544_pp_aloneimg_jhp1"
CAMPAIGN_B = "https://biodance.com/products/main-el-n-1-en-skincare-99"


class TestSlugEvidence:
    def test_campaign_markers(self):
        for slug in ("0627_cm_2544_pp", "x_cm_y", "promo_jhp1", "clone-99",
                     "0706_cm_ttest_thebudget1"):
            assert CAMPAIGN_MARKER_RE.search(slug), slug

    def test_clean_slugs_not_flagged(self):
        for slug in ("collagen-gel-mask", "balm-dotcom", "airpods-max"):
            assert not CAMPAIGN_MARKER_RE.search(slug), slug

    def test_similarity_prefers_title_matching_slug(self):
        clean = slug_evidence(_detail("a", CLEAN))
        campaign = slug_evidence(_detail("b", CAMPAIGN_A))
        assert clean["title_slug_similarity"] > campaign["title_slug_similarity"]
        assert not clean["campaign_marker"] and campaign["campaign_marker"]

    def test_url_slug_strips_query_noise(self):
        assert url_slug(CLEAN + "?utm_source=x#frag") == "collagen-gel-mask"

    def test_cleanest_member(self):
        rows = [_detail("a", CAMPAIGN_A), _detail("b", CLEAN),
                _detail("c", CAMPAIGN_B)]
        assert cleanest_member(rows)["product_key"] == "b"


class TestBuildProposal:
    def _group(self, pks: List[str]) -> Dict[str, Any]:
        return {"merchant_id": "external_seed", "content_key": "ck_1",
                "rows": [{"product_key": pk} for pk in pks]}

    def test_flags_campaign_keeper_when_cleaner_member_exists(self):
        # 'a' wins pick_canonical (lowest key) but is a campaign URL; 'b' is
        # the clean PDP -> flagged for the reviewer.
        detail = {"a": _detail("a", CAMPAIGN_A), "b": _detail("b", CLEAN)}
        (g,) = build_proposal([self._group(["a", "b"])], detail)["groups"]
        assert g["keeper"]["product_key"] == "a"
        assert g["keeper_url_looks_campaign"] is True
        assert g["cleanest_member_product_key"] == "b"

    def test_clean_keeper_not_flagged(self):
        detail = {"a": _detail("a", CLEAN), "b": _detail("b", CAMPAIGN_A)}
        (g,) = build_proposal([self._group(["a", "b"])], detail)["groups"]
        assert g["keeper"]["product_key"] == "a"
        assert g["keeper_url_looks_campaign"] is False


class TestReviewerEdits:
    def _fresh(self) -> Dict[str, Any]:
        detail = {"a": _detail("a", CAMPAIGN_A), "b": _detail("b", CLEAN)}
        return build_proposal(
            [{"merchant_id": "external_seed", "content_key": "ck_1",
              "rows": [{"product_key": "a"}, {"product_key": "b"}]}],
            detail,
        )

    def test_keeper_override_is_honored(self):
        fresh = self._fresh()
        reviewed = json_roundtrip(fresh)
        # Reviewer flips the keeper from 'a' (default) to 'b' (clean URL).
        g = reviewed["groups"][0]
        g["keeper"], g["losers"] = g["losers"][0], [g["keeper"]]
        to_apply, drifted = match_proposal(fresh, reviewed)
        assert drifted == []
        assert to_apply[0]["keeper"]["product_key"] == "b"
        assert [l["product_key"] for l in to_apply[0]["losers"]] == ["a"]

    def test_deleted_group_is_not_applied(self):
        fresh = self._fresh()
        reviewed = json_roundtrip(fresh)
        reviewed["groups"] = []
        to_apply, drifted = match_proposal(fresh, reviewed)
        assert to_apply == [] and drifted == []

    def test_keeper_edited_to_non_member_drifts(self):
        fresh = self._fresh()
        reviewed = json_roundtrip(fresh)
        reviewed["groups"][0]["keeper"]["product_key"] = "zzz-not-a-member"
        to_apply, drifted = match_proposal(fresh, reviewed)
        assert to_apply == []
        assert drifted == [("external_seed", "ck_1")]

    def test_member_set_change_since_review_drifts(self):
        fresh = self._fresh()
        reviewed = json_roundtrip(fresh)
        detail = {"a": _detail("a", CAMPAIGN_A), "b": _detail("b", CLEAN),
                  "c": _detail("c", CAMPAIGN_B)}
        fresh2 = build_proposal(
            [{"merchant_id": "external_seed", "content_key": "ck_1",
              "rows": [{"product_key": k} for k in ("a", "b", "c")]}],
            detail,
        )
        to_apply, drifted = match_proposal(fresh2, reviewed)
        assert to_apply == [] and drifted == [("external_seed", "ck_1")]


def json_roundtrip(obj: Dict[str, Any]) -> Dict[str, Any]:
    import json

    return json.loads(json.dumps(obj))


class TestConstants:
    def test_reason(self):
        assert SUPPRESSION_REASON == "step5_campaign_clone_dup"
