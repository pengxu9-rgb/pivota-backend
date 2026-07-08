"""Phase 1c — run-level competitor panels are vertical-aware.

The per-SKU competitor panel was threaded in Phase 1a; this covers the run-level
sites (merchant dominant-vertical helper + the narrative 'who AI cites instead').
"""
import services.agent_center_bd_report_service as R
import services.merchant_narrative_builder as N
from services.vertical_profiles import get_profile


def test_merchant_profile_is_dominant_vertical():
    assert R._merchant_profile_from_reports(
        [{"product_type": "Headphones", "title": "Purra Swim"},
         {"product_type": "Bone Conduction Earphones", "title": "Aerra"},
         {"product_type": "Supplements", "title": "Collagen"}]
    ).name == "electronics_audio"
    assert R._merchant_profile_from_reports(
        [{"product_type": "Beauty/Skincare", "title": "Snail Essence"}]
    ).name == "beauty"
    assert R._merchant_profile_from_reports([]).name == "beauty"          # default


def _authority_map(names):
    return {"skus": [{"sku_key": "s1", "authority_hosts": [
        {"host": "rtings.com", "competitors_named": names}]}]}


def test_narrative_electronics_drops_type_names_keeps_brands():
    am = _authority_map(["wireless earbuds", "Shokz", "noise cancelling headphones", "Bose"])
    names = {c["name"] for c in N._who_ai_cites_instead(am, vertical_profile=get_profile("electronics"))["competitors"]}
    assert "Shokz" in names and "Bose" in names
    assert "wireless earbuds" not in names and "noise cancelling headphones" not in names


def test_narrative_beauty_default_unchanged():
    # beauty default: electronics type-names are NOT beauty types, so they survive;
    # a beauty type ("Magnesium") is dropped. Proves the default path is untouched.
    am = _authority_map(["wireless earbuds", "Magnesium", "Thorne"])
    names = {c["name"] for c in N._who_ai_cites_instead(am)["competitors"]}
    assert "wireless earbuds" in names and "Thorne" in names
    assert "Magnesium" not in names
