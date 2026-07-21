"""Challenger-wedge prompt dimension (founder review of the VODANA pilot).

The v3 audit's generated portfolio was HEAD-TERM ONLY ("best flat iron",
"best flat iron for {concern}") — every query an entrenched incumbent owns, so
a challenger brand scored 0 targets / 5 skip (a give-up list, run 452d9394).
The wedge demo (run 7b345df0, custom_prompts, no code change) proved the
winnable lanes exist: outcome-framed and price/alternative queries produced 4
targets including a beachhead.

These tests pin the engine form of that wedge:

  1. outcome shapes — "{category} that {outcome}" from the profile's
     seed_outcome_terms, emitted right after the two diagnostic head terms so
     they survive a small prompts_per_sku budget;
  2. price-anchored shape — "best {category} under $N" from the SKU's real
     offer price (smallest common band >= price), never guessed;
  3. alternative-seeker shapes — "affordable {incumbent} alternative" from the
     profile's wedge_incumbent_brands (real brands, never type-words);
  4. profiles without the config (and SKUs without a price) are byte-unchanged.
"""

from __future__ import annotations

from services import agent_center_bd_report_service as m
from services.vertical_profiles import (
    BEAUTY_DEVICE_HAIR_PROFILE,
    BEAUTY_PROFILE,
)


def _hair_specs(price_band=None):
    return m._unbranded_category_specs(
        category="flat iron",
        graph={},
        topics=[],
        bullets=[],
        profile=BEAUTY_DEVICE_HAIR_PROFILE,
        price_band_usd=price_band,
    )


# --- 1. outcome shapes, early in the pool -----------------------------------

def test_outcome_shapes_emitted_right_after_head_terms():
    specs = _hair_specs()
    queries = [q for q, _ in specs]
    # The two diagnostic head terms still lead...
    assert queries[0] == "best flat iron"
    assert queries[1] == "what flat iron should I buy"
    # ...and the wedge outcomes come next — NOT after the concern/spec tail,
    # so a 10-prompt budget still probes them.
    assert queries[2] == "flat iron that doesn't snag or pull hair"
    assert queries[3] == "flat iron that also curls hair"
    # outcome shapes are discovery ("category"), not attribute
    axis_by_query = dict(specs)
    assert axis_by_query["flat iron that doesn't snag or pull hair"] == "category"


def test_alternative_seeker_shapes_from_profile_incumbents():
    queries = [q for q, _ in _hair_specs()]
    assert "affordable GHD alternative" in queries
    assert "affordable Dyson Airwrap alternative" in queries
    # capped at two incumbents — the wedge never floods the budget
    assert sum(1 for q in queries if q.startswith("affordable ")) == 2


def test_price_band_shape_only_with_band():
    with_band = [q for q, _ in _hair_specs(price_band=70)]
    without = [q for q, _ in _hair_specs()]
    assert "best flat iron under $70" in with_band
    assert not any("under $" in q for q in without)


def test_outcome_terms_never_tautological_for_sibling_categories():
    # The profile's outcome terms are class-wide; the category is specific.
    # "also curls hair" is a wedge for a flat iron but a tautology for a
    # curling iron — the stem guard drops the colliding pairing only.
    curler = [
        q for q, _ in m._unbranded_category_specs(
            category="curling iron",
            graph={},
            topics=[],
            bullets=[],
            profile=BEAUTY_DEVICE_HAIR_PROFILE,
        )
    ]
    assert "curling iron that also curls hair" not in curler
    # non-colliding outcomes still emit for the sibling category
    assert "curling iron that works abroad with dual voltage" in curler
    assert "curling iron that doesn't snag or pull hair" in curler


# --- 2. price band derivation ------------------------------------------------

def test_wedge_price_band_picks_smallest_band_at_or_above_price():
    sku_ctx = {"offers": [{"merchant_effective_price": 62.95, "currency": "USD"}]}
    assert m._wedge_price_band_usd(sku_ctx) == 70
    assert m._wedge_price_band_usd(
        {"offers": [{"estimated_best_price": 19.0}]}
    ) == 25


def test_wedge_price_band_honest_when_unusable():
    # no offers / no price -> no shape, never a guess
    assert m._wedge_price_band_usd({}) is None
    assert m._wedge_price_band_usd({"offers": [{}]}) is None
    # premium beyond the top band -> not a budget-wedge story
    assert m._wedge_price_band_usd(
        {"offers": [{"merchant_effective_price": 450.0}]}
    ) is None
    # a stated non-USD currency never produces a $-band
    assert m._wedge_price_band_usd(
        {"offers": [{"merchant_effective_price": 89000, "currency": "KRW"}]}
    ) is None


def test_wedge_price_band_uses_best_price_across_offers():
    sku_ctx = {"offers": [
        {"merchant_effective_price": 95.0},
        {"merchant_effective_price": 45.0},
    ]}
    assert m._wedge_price_band_usd(sku_ctx) == 50


# --- 3. unconfigured profiles are byte-unchanged -----------------------------

def test_profile_without_wedge_config_is_unchanged():
    # Byte-unchanged for unconfigured profiles EVEN WITH a priced SKU: probe-set
    # composition is pinned behavior, so the price shape is gated on the
    # profile carrying wedge config, not just on a price existing.
    kwargs = dict(
        category="hair oil",
        graph={"classes": {"use_case": ["damaged hair"]}},
        topics=[],
        bullets=[],
        profile=BEAUTY_PROFILE,
    )
    before = m._unbranded_category_specs(**kwargs)
    with_default_band = m._unbranded_category_specs(**kwargs, price_band_usd=None)
    with_priced_sku = m._unbranded_category_specs(**kwargs, price_band_usd=25)
    assert before == with_default_band == with_priced_sku
    queries = [q for q, _ in before]
    assert not any(" that " in q for q in queries)
    assert not any(q.startswith("affordable ") for q in queries)
    assert not any("under $" in q for q in queries)


# --- 4. end-to-end: base specs for a VODANA-like SKU -------------------------

def test_base_specs_include_wedge_for_priced_device_sku():
    sku_ctx = {
        "sku_key": "vodana-softbar",
        "product": {
            "brand": "VODANA",
            "title": "Professional Softbar Flat Iron",
            "product_type": "Flat Iron",
        },
        "sku": {"title": ""},
        "offers": [{"merchant_effective_price": 62.95, "currency": "USD"}],
    }
    specs, _title, _ptype = m._build_per_sku_base_query_specs(sku_ctx)
    queries = [q for q, _ in specs]
    assert "flat iron that doesn't snag or pull hair" in queries
    assert "best flat iron under $70" in queries
    assert "affordable GHD alternative" in queries
