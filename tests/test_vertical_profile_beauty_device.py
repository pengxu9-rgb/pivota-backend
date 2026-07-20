"""beauty_device sub-vertical (VODANA pilot — beauty x 3C hair-styling tools).

Guards the beauty-device extension of the multi-vertical architecture — the
beauty-side mirror of the electronics audio/drone split:

1. hair-styling-tool text resolves to the `beauty` vertical (same persisted
   column as a topical cosmetic — the topical/device split is a runtime PROFILE
   choice, not a new column value), INCLUDING under-tagged rows whose only
   category signal is "Hair Styling Tools" (no "beauty" word);
2. ``resolve_profile`` splits beauty into device vs topical from SKU text — a
   flat iron gets BEAUTY_DEVICE_PROFILE, a genuine topical SKU still gets
   BEAUTY_PROFILE (no regression), electronics stays electronics;
3. the device profile carries device content (no ingredients / no INCI grounding,
   beauty-tool authority hosts, KEEPS problem-framed prompts — the hybrid) and
   its type-token filter drops "flat iron"-style fake brands while keeping
   Dyson / ghd / VODANA;
4. ``get_profile("beauty")`` is UNCHANGED (still topical) — the split is additive
   and only the product-aware resolve_profile* path can pick device;
5. the category classifier gives a hair tool a real device category_path and does
   NOT steal a makeup brush or a topical hair product.
"""
import pytest

from services.competitor_brand_filter import filter_competitor_brands
from services.pdp_category_classifier import classify
from services.vertical_profiles import (
    BEAUTY_DEVICE_PROFILE,
    BEAUTY_PROFILE,
    get_profile,
    resolve_profile,
    resolve_profile_for_vertical,
    resolve_vertical,
)


# --------------------- resolver: hair tool -> beauty --------------------- #

@pytest.mark.parametrize("product_type,expected", [
    ("Flat Iron", "beauty"),
    ("Hair Straightener", "beauty"),
    ("Ceramic Straightener", "beauty"),        # bare "straightener" token
    ("Curling Iron", "beauty"),
    ("Curling Wand", "beauty"),
    ("Hair Dryer", "beauty"),
    ("Blow Dryer", "beauty"),
    ("Hair Styler", "beauty"),
    ("Straightening Brush", "beauty"),         # device, not a makeup brush
])
def test_hair_tool_text_resolves_beauty(product_type, expected):
    assert resolve_vertical({"product_type": product_type}) == expected


def test_under_tagged_device_row_resolves_beauty_not_other():
    # The real-world VODANA case: a marketplace row whose only category signal is
    # "Hair Styling Tools" (no "beauty"/"skin" word). Before the device keywords
    # it collapsed to `other`; now it resolves `beauty` so the sub-split can fire.
    row = {"product_type": "Flat Iron", "category": "Hair Styling Tools"}
    assert resolve_vertical(row) == "beauty"


def test_device_tokens_do_not_touch_topical_beauty_or_electronics():
    # Device keywords are hair-tool-specific: they never pull a topical cosmetic,
    # a supplement, or an electronics SKU to the wrong vertical.
    assert resolve_vertical({"product_type": "Vitamin C Serum"}) == "beauty"
    assert resolve_vertical({"product_type": "Hair Oil"}) != "electronics"
    assert resolve_vertical({"product_type": "Wireless Earbuds"}) == "electronics"
    # Bare "iron" is a clothes iron / iron supplement, deliberately NOT a device
    # token — it must not alone force the device path (see below).


def test_title_tier_resolves_storeless_hair_tool():
    # A store-less URL audit with only a title still resolves beauty via the
    # device tokens now folded into the title tier.
    assert resolve_vertical({"product_type": ""}, title="VODANA Softbar Flat Iron") == "beauty"


# --------------------- profile: device vs topical split --------------------- #

def test_beauty_splits_device_vs_topical():
    device = resolve_profile(
        {"product_type": "Flat Iron", "title": "VODANA Professional Softbar Flat Iron"}
    )
    assert device.name == "beauty_device"

    # Title-tier path: no category signal, device signal only in the title kwarg
    # (store-less URL audit). Resolves beauty via the title tier, then splits device.
    device2 = resolve_profile({"product_type": ""}, title="VODANA Glassy Hair Dryer")
    assert device2.name == "beauty_device"

    # Sub-split reads product["title"]: vertical comes from product_type, the
    # device signal from the dict title (mirrors the drone dict-title path).
    device3 = resolve_profile(
        {"product_type": "Beauty Personal Care", "title": "VODANA Glassy Hair Dryer"}
    )
    assert device3.name == "beauty_device"

    # A genuine topical SKU (no device token) still resolves topical beauty.
    topical = resolve_profile({"product_type": "Beauty Serum"})
    assert topical.name == "beauty"

    # Electronics is untouched by the beauty split.
    assert resolve_profile({"product_type": "Wireless Earbuds"}).name == "electronics_audio"


def test_get_profile_beauty_unchanged_still_topical():
    # The split is additive: the string-keyed get_profile still defaults
    # beauty -> topical (locked by the phase-0 golden test). Only the
    # product-aware resolve_profile* path can pick device.
    assert get_profile("beauty").name == "beauty"
    assert resolve_profile_for_vertical("beauty").name == "beauty"  # no product text


def test_bare_iron_alone_does_not_force_device_profile():
    # "iron" alone (clothes iron / iron supplement) is NOT a device token — a
    # supplement must not be dragged to the device profile by it.
    prof = resolve_profile({"product_type": "Iron Supplement", "title": "Gentle Iron 25mg"})
    assert prof.name != "beauty_device"


# --------------------- device profile content --------------------- #

def test_device_profile_content_and_is_not_topical():
    prof = BEAUTY_DEVICE_PROFILE
    assert prof.name == "beauty_device"
    assert prof.competitor_ingredient_tokens == frozenset()   # a device has no ingredients
    assert prof.evidence_bindings == "none"                   # NOT inci_grounded
    assert prof.attribute_strategy == "llm_extractor"         # NOT lexicon_first
    assert prof.health_sensitive is False
    # The hybrid: unlike audio/drone, concern-framed prompts stay ON.
    assert prof.problem_framed_prompts is True
    assert "allure.com" in prof.authority_hosts
    assert prof.brief_rules is not None
    assert "voltage" in prof.brief_rules.claim_rules.lower()
    # NOT the topical beauty profile's INCI-grounded content.
    assert prof.evidence_bindings != BEAUTY_PROFILE.evidence_bindings
    assert prof.category_fallbacks == ()                      # never "beauty supplement"
    assert "serum" not in prof.category_head_nouns
    assert "rtings.com" not in prof.authority_hosts           # not the audio set


def test_device_competitor_filter_drops_type_names_keeps_brands():
    prof = BEAUTY_DEVICE_PROFILE
    kept = filter_competitor_brands(
        ["flat iron", "Dyson", "curling iron", "ghd", "hair dryer", "VODANA",
         "T3", "professional straightener"],
        ingredient_tokens=prof.competitor_ingredient_tokens,
        form_tokens=prof.competitor_form_tokens,
    )
    assert kept == ["Dyson", "ghd", "VODANA", "T3"]


# --------------------- category_path classifier --------------------- #

@pytest.mark.parametrize("text", [
    "VODANA Professional Softbar Flat Iron",
    "VODANA Velvetbar Hair Straightener",
    "Ceramic Straightening Brush",
    "Curling Iron 40mm",
    "VODANA Glassy Hair Dryer",
])
def test_classifier_gives_hair_tool_device_path(text):
    label, path = classify(text)
    assert (label, path) == ("Hair Styling Tool", "beauty/tools/hair-styling-tool")


# --------------------- topical look-alikes (adversarial review) --------------------- #

# Topical / makeup products whose NAMES contain a device-ish token but that carry
# a FORM noun (spray/serum/cream/pencil/...) — they must stay on the topical
# BEAUTY profile (INCI-grounded) and must NOT get the device category_path.
@pytest.mark.parametrize("text", [
    "Flat Iron Sleek Spray",            # heat-protectant spray, not an iron
    "Flat Iron Heat Protectant",
    "Blow Dry Primer",                  # styling action, not a dryer
    "Blowout Cream",
    "Curl Styler Cream",                # topical curl cream
    "Bounce Curl Styler",               # bare "styler" is no longer a device token
    "Brow Styler",                      # makeup, not a hair tool
    "Brow Styler Pencil",
    "Brazilian Straightener Serum",     # keratin serum, not an appliance
    "Flatiron District Candle",         # non-beauty place name, not "flat iron"
])
def test_topical_lookalikes_never_get_device_profile_or_path(text):
    prof = resolve_profile({"product_type": text})
    assert prof.name != "beauty_device", f"{text!r} wrongly got the device profile"
    hit = classify(text)
    assert hit is None or hit[1] != "beauty/tools/hair-styling-tool", (
        f"{text!r} wrongly classified as a hair-styling tool"
    )


def test_real_device_with_topical_word_still_device_when_no_form_noun():
    # Guard is FORM-noun-specific: "ceramic straightener" (a material, not a form)
    # is still a device; only a formulation word (serum/spray/cream) vetoes.
    assert resolve_profile({"product_type": "Ceramic Straightener"}).name == "beauty_device"
    assert classify("Ionic Ceramic Straightener") == (
        "Hair Styling Tool", "beauty/tools/hair-styling-tool"
    )


# --------------------- strategic-brief routing (trust-critical path) --------------------- #

def test_strategic_brief_applies_device_split():
    from services.strategic_brief import _brief_profile_for_evidence, _render_system_prompt

    device = _brief_profile_for_evidence(
        {"vertical": "beauty", "title": "VODANA Professional Softbar Flat Iron Ceramic Straightener"}
    )
    assert device.name == "beauty_device"
    device_prompt = _render_system_prompt(device)
    assert "voltage" in device_prompt.lower()          # device claim rules are live

    topical = _brief_profile_for_evidence(
        {"vertical": "beauty", "title": "COSRX Advanced Snail 96 Mucin Power Essence"}
    )
    assert topical.name == "beauty"
    assert "voltage" not in _render_system_prompt(topical).lower()  # incumbent INCI prompt

    # A topical look-alike keeps the incumbent (INCI) prompt — the conservative
    # choice for the trust-critical brief.
    assert _brief_profile_for_evidence(
        {"vertical": "beauty", "title": "Flat Iron Sleek Spray"}
    ).name == "beauty"

    # Electronics routing is unchanged.
    assert _brief_profile_for_evidence(
        {"vertical": "electronics", "title": "HoverAir X1 self-flying camera drone"}
    ).name == "electronics_drone"


def test_classifier_does_not_steal_makeup_brush_or_topical_haircare():
    # A makeup brush is still a makeup brush (device pattern needs a heat/styling
    # qualifier before "brush").
    assert classify("Makeup Brush Pouch") == ("Brush Pouch", "beauty/tools/brush-accessory")
    assert classify("Foundation Brush")[1] == "beauty/tools/brush"
    # Topical hair products stay topical (no device token present).
    assert classify("Argan Hair Oil")[1].startswith("beauty/")
    assert "hair-styling-tool" not in (classify("Argan Hair Oil")[1])
    assert classify("Nourishing Shampoo") == ("Shampoo", "beauty/haircare/shampoo")
