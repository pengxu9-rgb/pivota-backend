"""electronics_drone sub-vertical (Phase-1b) — resolver + profile + classifier.

Guards the drone extension of the multi-vertical architecture:

1. drone text resolves to the `electronics` vertical (same persisted column as
   audio — the drone/audio split is a runtime PROFILE choice, not a new column
   value);
2. ``resolve_profile`` splits electronics into drone vs audio from SKU text — a
   drone gets ELECTRONICS_DRONE_PROFILE, a genuine audio SKU still gets
   ELECTRONICS_AUDIO_PROFILE (no regression), beauty stays beauty;
3. the drone profile carries drone content (faa.gov authority, no problem-framed
   prompts, no ingredients) and its type-token filter drops "camera drone"-style
   fake brands while keeping DJI/HoverAir/Autel;
4. ``get_profile("electronics")`` is UNCHANGED (still audio) — the split is
   additive and never disturbs the existing electronics default;
5. the category classifier gives a drone a real electronics category_path.
"""
import pytest

from services.competitor_brand_filter import filter_competitor_brands
from services.pdp_category_classifier import classify
from services.vertical_profiles import (
    ELECTRONICS_AUDIO_PROFILE,
    ELECTRONICS_DRONE_PROFILE,
    get_profile,
    resolve_profile,
    resolve_profile_for_vertical,
    resolve_vertical,
)


# --------------------------- resolver: drone -> electronics --------------------------- #

@pytest.mark.parametrize("product_type,expected", [
    ("Camera Drone", "electronics"),
    ("Self-Flying Camera", "electronics"),      # "camera" keyword + "self flying" phrase
    ("Quadcopter", "electronics"),
    ("Drone", "electronics"),                   # bare "drone" — NEW keyword (DJI-style rows)
    ("FPV Drone", "electronics"),
    ("Follow-Me Drone", "electronics"),
])
def test_drone_text_resolves_electronics(product_type, expected):
    assert resolve_vertical({"product_type": product_type}) == expected


def test_uav_is_whole_word_only():
    # "uav" resolves electronics as a standalone token...
    assert resolve_vertical({"product_type": "Compact UAV"}) == "electronics"
    # ...but must not fire inside an unrelated word.
    assert resolve_vertical({"product_type": "Uavender Body Lotion"}) != "electronics"


def test_drone_tokens_do_not_touch_beauty():
    # None of the drone tokens is a substring of a beauty/supplement word, so a
    # beauty SKU is never pulled to electronics by the drone additions. (Bare
    # "collagen"/"gummies" are title-tier fallback triggers, not category
    # keywords, so a product_type of "Collagen Gummies" resolves "other" — the
    # point here is only that it never becomes electronics.)
    assert resolve_vertical({"product_type": "Vitamin C Serum"}) == "beauty"   # 'vitamin' keyword
    assert resolve_vertical({"product_type": "Beauty Skincare"}) == "beauty"   # 'beauty'/'skin'
    assert resolve_vertical({"product_type": "Collagen Gummies"}) != "electronics"


# --------------------------- profile: drone vs audio split --------------------------- #

def test_electronics_splits_drone_vs_audio():
    drone = resolve_profile({"product_type": "Camera Drone", "title": "HoverAir X1"})
    assert drone.name == "electronics_drone"

    # HoverAir with only a title signal ("self-flying camera") still lands drone.
    drone2 = resolve_profile(
        {"product_type": "Camera", "title": "HoverAir X1 Self-Flying Camera"}
    )
    assert drone2.name == "electronics_drone"

    # A genuine audio SKU (no drone token) still resolves audio — no regression.
    audio = resolve_profile({"product_type": "Wireless Earbuds"})
    assert audio.name == "electronics_audio"

    # Beauty stays beauty.
    assert resolve_profile({"product_type": "Beauty Serum"}).name == "beauty"


def test_get_profile_electronics_unchanged_still_audio():
    # The split is additive: the string-keyed get_profile still defaults
    # electronics -> audio (locked by the phase-0 golden test). Only the
    # product-aware resolve_profile* path can pick drone.
    assert get_profile("electronics").name == "electronics_audio"
    assert resolve_profile_for_vertical("electronics").name == "electronics_audio"  # no product text


# --------------------------- drone profile content --------------------------- #

def test_drone_profile_content_and_is_not_audio():
    prof = ELECTRONICS_DRONE_PROFILE
    assert prof.name == "electronics_drone"
    assert "faa.gov" in prof.authority_hosts          # regulatory = decision lever
    assert "thedronegirl.com" in prof.authority_hosts
    assert prof.problem_framed_prompts is False       # "what helps with a drone" is junk
    assert prof.evidence_bindings == "none"
    assert prof.health_sensitive is False
    assert prof.competitor_ingredient_tokens == frozenset()   # drones have no "ingredients"
    assert prof.brief_rules is not None
    assert "FAA" in prof.brief_rules.claim_rules
    # NOT the audio profile's content.
    assert "rtings.com" not in prof.authority_hosts
    assert "headphones" not in prof.category_head_nouns


def test_drone_competitor_filter_drops_type_names_keeps_brands():
    prof = ELECTRONICS_DRONE_PROFILE
    kept = filter_competitor_brands(
        ["camera drone", "DJI", "mini drone", "HoverAir", "self flying camera",
         "Autel Robotics", "quadcopter"],
        ingredient_tokens=prof.competitor_ingredient_tokens,
        form_tokens=prof.competitor_form_tokens,
    )
    assert kept == ["DJI", "HoverAir", "Autel Robotics"]


# --------------------------- category_path classifier --------------------------- #

@pytest.mark.parametrize("text", [
    "HoverAir X1 Self-Flying Camera",
    "DJI Neo camera drone",
    "quadcopter",
    "Follow-Me Drone for hiking",
])
def test_classifier_gives_drone_electronics_path(text):
    hit = classify(text)
    assert hit == ("Camera Drone", "electronics/drones/camera-drone")


def test_classifier_beauty_unchanged_by_drone_rule():
    # The drone rule is first but drone-specific, so beauty still classifies as
    # before (no bare "camera" in the drone regex).
    assert classify("Vitamin C Brightening Serum") == ("Serum", "beauty/skincare/treat/serum")
    assert classify("Matte Lipstick Ruby")[1].startswith("beauty/makeup/lip")
