"""beauty_device FAMILY (VODANA pilot + forward-looking) — the beauty-side mirror
of the electronics audio/drone split, generalized to a family of device classes.

Guards:

1. device text resolves to the `beauty` vertical (same persisted column as a
   topical — the topical/device split is a runtime PROFILE choice), INCLUDING
   under-tagged rows whose only category signal is "Hair Styling Tools";
2. ``resolve_profile`` routes a beauty SKU to its device CLASS (hair / skincare
   energy / hair-removal / generic) or to the topical BEAUTY_PROFILE; electronics
   is untouched;
3. class content is correct — devices have no INCI, energy/light + hair-removal
   devices are health_sensitive=True (hair-styling is not), and an UNMODELED class
   falls to the generic device profile (safe, no INCI), never to topical;
4. ``get_profile("beauty")`` is UNCHANGED (still topical); only the product-aware
   path can pick a device;
5. topical look-alikes ("flat iron spray", "hair removal cream") stay topical;
6. the classifier gives each device class a `beauty/devices/*` path and does not
   steal a makeup brush or a topical product.
"""
import pytest

from services.competitor_brand_filter import filter_competitor_brands
from services.pdp_category_classifier import classify
from services.vertical_profiles import (
    BEAUTY_DEVICE_GENERIC_PROFILE,
    BEAUTY_DEVICE_HAIR_PROFILE,
    BEAUTY_DEVICE_HAIR_REMOVAL_PROFILE,
    BEAUTY_DEVICE_SKINCARE_PROFILE,
    BEAUTY_PROFILE,
    get_profile,
    resolve_profile,
    resolve_profile_for_vertical,
    resolve_vertical,
)


# --------------------- resolver: device text -> beauty --------------------- #

@pytest.mark.parametrize("product_type,expected", [
    ("Flat Iron", "beauty"),
    ("Hair Straightener", "beauty"),
    ("Ceramic Straightener", "beauty"),
    ("Curling Iron", "beauty"),
    ("Hair Dryer", "beauty"),
    ("Straightening Brush", "beauty"),      # device, not a makeup brush
    ("LED Face Mask", "beauty"),            # skincare-energy device
    ("Microcurrent Device", "beauty"),
    ("Laser Hair Removal Handset", "beauty"),
    ("Nail Lamp", "beauty"),
])
def test_device_text_resolves_beauty(product_type, expected):
    assert resolve_vertical({"product_type": product_type}) == expected


def test_under_tagged_device_row_resolves_beauty_not_other():
    # A marketplace row whose only category signal is "Hair Styling Tools" (no
    # "beauty" word) still resolves `beauty` so the sub-split can fire.
    assert resolve_vertical({"product_type": "Flat Iron", "category": "Hair Styling Tools"}) == "beauty"


def test_device_tokens_do_not_touch_topical_beauty_or_electronics():
    assert resolve_vertical({"product_type": "Vitamin C Serum"}) == "beauty"
    assert resolve_vertical({"product_type": "Hair Oil"}) != "electronics"
    assert resolve_vertical({"product_type": "Wireless Earbuds"}) == "electronics"


# --------------------- profile: family routing --------------------- #

def test_beauty_routes_hair_styling_device():
    device = resolve_profile(
        {"product_type": "Flat Iron", "title": "VODANA Professional Softbar Flat Iron"}
    )
    assert device.name == "beauty_device_hair"

    # Title-tier path (store-less URL audit) still lands the device profile.
    assert resolve_profile({"product_type": ""}, title="VODANA Glassy Hair Dryer").name == "beauty_device_hair"

    # A genuine topical SKU stays topical beauty; electronics untouched.
    assert resolve_profile({"product_type": "Beauty Serum"}).name == "beauty"
    assert resolve_profile({"product_type": "Wireless Earbuds"}).name == "electronics_audio"


def test_future_device_classes_route_and_are_never_inci_grounded():
    # The whole point of the family: a NON-hair beauty device must never fall back
    # to the topical INCI profile. Each class routes correctly, none is inci_grounded.
    cases = {
        "Omnilux Contour Face LED Mask":        "beauty_device_skincare_energy",
        "NuFACE Trinity Microcurrent Device":   "beauty_device_skincare_energy",
        "Dr Dennis Gross RF Skin Tightening Device": "beauty_device_skincare_energy",
        "Philips Lumea IPL Hair Removal":       "beauty_device_hair_removal",
        "Braun Silk-expert Pro 5 Epilator":     "beauty_device_hair_removal",
        "SUNUV UV LED Nail Lamp 48W":           "beauty_device_generic",   # nail -> generic
        "FOREO LUNA 4 Facial Cleansing Brush":  "beauty_device_generic",
    }
    for title, expected in cases.items():
        prof = resolve_profile({"product_type": ""}, title=title)
        assert prof.name == expected, f"{title!r} -> {prof.name}, expected {expected}"
        assert prof.evidence_bindings == "none", f"{title!r} wrongly inci-grounded"


@pytest.mark.parametrize("title,expected", [
    # A real DEVICE that ships with an accessory SUBSTANCE (gel/serum) must still
    # route to its device class — the form noun is the accessory, not the product.
    ("ZIIP Microcurrent Device + Conductive Gel", "beauty_device_skincare_energy"),
    ("NuFACE Microcurrent Device with Aqua Gel Primer", "beauty_device_skincare_energy"),
    ("Ulike IPL Hair Removal Handset with Cooling Gel", "beauty_device_hair_removal"),
    ("Omnilux LED Face Mask serum compatible", "beauty_device_skincare_energy"),
    ("UV Gel Nail Lamp 48W", "beauty_device_generic"),   # "gel lamp" is the device name
])
def test_device_with_accessory_substance_still_routes_device(title, expected):
    prof = resolve_profile({"product_type": ""}, title=title)
    assert prof.name == expected, f"{title!r} -> {prof.name}"
    assert prof.evidence_bindings == "none"


def test_form_noun_is_the_product_still_vetoes_even_with_device_word():
    # No HARD device head: the form noun IS the product (a topical that names a
    # tool), so it stays topical even though a device phrase is present.
    assert not resolve_profile({"product_type": ""}, title="Flat Iron Spray with Argan Oil").name.startswith("beauty_device")
    assert not resolve_profile({"product_type": ""}, title="Brazilian Straightener Serum").name.startswith("beauty_device")


@pytest.mark.parametrize("title", [
    # Topical AFTERCARE formulations that merely NAME a device technology. The
    # tech token (ipl/laser/epilator/microcurrent) is a modifier, and there is NO
    # unambiguous device NOUN — so the form noun is the product → stay topical.
    "IPL Aftercare Soothing Gel",
    "Post-IPL Calming Serum",
    "Laser Hair Removal Aftercare Cream",
    "Laser Hair Removal Soothing Gel",
    "Epilator Cooling Gel",
    "Microcurrent Conductivity Gel",
])
def test_device_tech_named_topical_aftercare_stays_topical(title):
    prof = resolve_profile({"product_type": ""}, title=title)
    assert not prof.name.startswith("beauty_device"), f"{title!r} -> {prof.name} (should be topical)"


def test_under_tagged_ipl_row_resolves_beauty_then_routes_hair_removal():
    # S2: a bare "IPL" + category "Hair Removal" row (no "beauty" word) must resolve
    # the beauty vertical (via the "hair removal" resolver keyword) so the router
    # reads its "ipl" signal — instead of collapsing to other/electronics.
    row = {"product_type": "IPL", "category": "Hair Removal"}
    assert resolve_vertical(row, title="Braun Silk-expert Pro 5 IPL") == "beauty"
    assert resolve_profile(row, title="Braun Silk-expert Pro 5 IPL").name == "beauty_device_hair_removal"


def test_energy_and_hair_removal_devices_are_health_sensitive():
    # Safety-critical: LED/microcurrent/RF and IPL/laser carry contraindications,
    # so their profiles must flag health_sensitive=True. Hair-styling does NOT.
    assert BEAUTY_DEVICE_SKINCARE_PROFILE.health_sensitive is True
    assert BEAUTY_DEVICE_HAIR_REMOVAL_PROFILE.health_sensitive is True
    assert BEAUTY_DEVICE_HAIR_PROFILE.health_sensitive is False
    # Hair-removal brief must forbid "permanent removal" and center eligibility.
    hr_rules = BEAUTY_DEVICE_HAIR_REMOVAL_PROFILE.brief_rules.claim_rules.lower()
    assert "reduction" in hr_rules and "fitzpatrick" in hr_rules
    # Skincare-energy brief must center FDA clearance / contraindications.
    sk_rules = BEAUTY_DEVICE_SKINCARE_PROFILE.brief_rules.claim_rules.lower()
    assert "fda" in sk_rules and "contraindication" in sk_rules


def test_get_profile_beauty_unchanged_still_topical():
    assert get_profile("beauty").name == "beauty"
    assert resolve_profile_for_vertical("beauty").name == "beauty"  # no product text


def test_bare_iron_alone_does_not_force_device_profile():
    prof = resolve_profile({"product_type": "Iron Supplement", "title": "Gentle Iron 25mg"})
    assert not prof.name.startswith("beauty_device")


# --------------------- device profile content --------------------- #

def test_hair_device_profile_content_and_is_not_topical():
    prof = BEAUTY_DEVICE_HAIR_PROFILE
    assert prof.name == "beauty_device_hair"
    assert prof.competitor_ingredient_tokens == frozenset()   # a device has no ingredients
    assert prof.evidence_bindings == "none"                   # NOT inci_grounded
    assert prof.attribute_strategy == "llm_extractor"         # NOT lexicon_first
    assert prof.health_sensitive is False
    assert prof.problem_framed_prompts is True                # the hybrid
    assert "allure.com" in prof.authority_hosts
    assert "voltage" in prof.brief_rules.claim_rules.lower()
    assert prof.evidence_bindings != BEAUTY_PROFILE.evidence_bindings
    assert "serum" not in prof.category_head_nouns
    assert "rtings.com" not in prof.authority_hosts           # not the audio set


def test_generic_device_profile_is_safe_not_topical():
    prof = BEAUTY_DEVICE_GENERIC_PROFILE
    assert prof.name == "beauty_device_generic"
    assert prof.evidence_bindings == "none"                   # never INCI
    assert prof.competitor_ingredient_tokens == frozenset()
    assert prof.health_sensitive is None                      # unknown class -> heuristic decides
    assert prof.brief_rules is not None                       # non-INCI device prompt, not incumbent


def test_device_competitor_filter_drops_type_names_keeps_brands():
    prof = BEAUTY_DEVICE_HAIR_PROFILE
    kept = filter_competitor_brands(
        ["flat iron", "Dyson", "curling iron", "ghd", "hair dryer", "VODANA",
         "T3", "professional straightener"],
        ingredient_tokens=prof.competitor_ingredient_tokens,
        form_tokens=prof.competitor_form_tokens,
    )
    assert kept == ["Dyson", "ghd", "VODANA", "T3"]


# --------------------- category_path classifier --------------------- #

@pytest.mark.parametrize("text,expected", [
    ("VODANA Professional Softbar Flat Iron", ("Hair Styling Tool", "beauty/devices/hair-styling")),
    ("VODANA Velvetbar Hair Straightener",    ("Hair Styling Tool", "beauty/devices/hair-styling")),
    ("Ceramic Straightening Brush",           ("Hair Styling Tool", "beauty/devices/hair-styling")),
    ("Curling Iron 40mm",                     ("Hair Styling Tool", "beauty/devices/hair-styling")),
    ("Omnilux Contour Face LED Mask",         ("Skincare Device", "beauty/devices/skincare-energy")),
    ("NuFACE Microcurrent Device",            ("Skincare Device", "beauty/devices/skincare-energy")),
    ("Philips Lumea IPL Hair Removal",        ("Hair Removal Device", "beauty/devices/hair-removal")),
    ("Braun Epilator",                        ("Hair Removal Device", "beauty/devices/hair-removal")),
    ("SUNUV UV LED Nail Lamp",                ("Nail Device", "beauty/devices/nail")),
])
def test_classifier_gives_each_device_class_its_path(text, expected):
    assert classify(text) == expected


# --------------------- topical look-alikes (never a device) --------------------- #

# Topical / makeup products whose NAMES contain a device-ish token but carry a
# FORM noun (spray/serum/cream/wax/pencil/...) — they must stay on the topical
# BEAUTY profile (INCI) and must NOT get a beauty/devices/* path.
@pytest.mark.parametrize("text", [
    "Flat Iron Sleek Spray",
    "Flat Iron Heat Protectant",
    "Blow Dry Primer",
    "Blowout Cream",
    "Curl Styler Cream",
    "Bounce Curl Styler",
    "Brow Styler",
    "Brow Styler Pencil",
    "Brazilian Straightener Serum",
    "Flatiron District Candle",
    "Veet Hair Removal Cream",          # depilatory — topical, not an IPL device
    "Nair Hair Removal Wax Strips",
    "Vitamin C LED-Boosting Serum",     # topical serum that name-drops "LED"
])
def test_topical_lookalikes_never_get_device_profile_or_path(text):
    prof = resolve_profile({"product_type": text})
    assert not prof.name.startswith("beauty_device"), f"{text!r} wrongly got a device profile"
    hit = classify(text)
    assert hit is None or not hit[1].startswith("beauty/devices/"), (
        f"{text!r} wrongly classified as a device"
    )


def test_real_device_with_material_word_still_device():
    # Guard is FORM-noun-specific: "ceramic straightener" (a material) is a device;
    # only a formulation word (serum/spray/cream) vetoes.
    assert resolve_profile({"product_type": "Ceramic Straightener"}).name == "beauty_device_hair"
    assert classify("Ionic Ceramic Straightener") == (
        "Hair Styling Tool", "beauty/devices/hair-styling"
    )


# --------------------- strategic-brief routing (trust-critical path) --------------------- #

def test_strategic_brief_applies_device_split():
    from services.strategic_brief import _brief_profile_for_evidence, _render_system_prompt

    device = _brief_profile_for_evidence(
        {"vertical": "beauty", "title": "VODANA Professional Softbar Flat Iron Ceramic Straightener"}
    )
    assert device.name == "beauty_device_hair"
    assert "voltage" in _render_system_prompt(device).lower()   # device claim rules are live

    # A health-sensitive class gets its own (non-INCI) prompt.
    ipl = _brief_profile_for_evidence({"vertical": "beauty", "title": "Philips Lumea IPL Hair Removal"})
    assert ipl.name == "beauty_device_hair_removal"
    assert "reduction" in _render_system_prompt(ipl).lower()

    topical = _brief_profile_for_evidence(
        {"vertical": "beauty", "title": "COSRX Advanced Snail 96 Mucin Power Essence"}
    )
    assert topical.name == "beauty"
    assert "voltage" not in _render_system_prompt(topical).lower()  # incumbent INCI prompt

    # A topical look-alike keeps the incumbent (INCI) prompt.
    assert _brief_profile_for_evidence(
        {"vertical": "beauty", "title": "Flat Iron Sleek Spray"}
    ).name == "beauty"

    # Electronics routing is unchanged.
    assert _brief_profile_for_evidence(
        {"vertical": "electronics", "title": "HoverAir X1 self-flying camera drone"}
    ).name == "electronics_drone"


def test_classifier_does_not_steal_makeup_brush_or_topical_haircare():
    assert classify("Makeup Brush Pouch") == ("Brush Pouch", "beauty/tools/brush-accessory")
    assert classify("Foundation Brush")[1] == "beauty/tools/brush"
    assert classify("Argan Hair Oil")[1].startswith("beauty/")
    assert not classify("Argan Hair Oil")[1].startswith("beauty/devices/")
    assert classify("Nourishing Shampoo") == ("Shampoo", "beauty/haircare/shampoo")
