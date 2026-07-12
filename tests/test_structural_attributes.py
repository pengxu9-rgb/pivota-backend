"""Tests for services/structural_attributes.py (Fix Plan G — T1).

Pins the deterministic-first contract:
  - volume/spf/format/flags resolve from regex/lexicon, never an LLM;
  - existing signal (active_ingredients_json, concerns_json, seed INCI) is
    RECONCILED, not regenerated, and wins with the right provenance;
  - the LLM residual pass fills ONLY unresolved judgment fields, is vocabulary-
    clamped (cannot invent or overwrite a deterministic field), and reports a
    typed outcome so truncation/parse-fail is COUNTED not swallowed;
  - the envelope is versioned and deterministic values always win.
"""

from __future__ import annotations

import pytest

from services import structural_attributes as sa


# --------------------------------------------------------------------------- #
# volume — the one new deterministic extractor
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    ("Hydrating Serum 50ml", "50 ml"),
    ("Face Cream 1.7 fl oz", "1.7 fl oz"),
    ("Toner 250 mL", "250 ml"),
    ("Repair Balm 30 g", "30 g"),
    ("Big Bottle 1 L", "1 l"),
    ("No size in this title", None),
    ("Limited 2024 Edition", None),        # a year is not a size
    ("Mega Value 900 oz drum", None),      # implausible for beauty -> rejected
])
def test_extract_volume(text, expected):
    assert sa.extract_volume(text) == expected


def test_extract_volume_first_match_across_texts():
    assert sa.extract_volume(None, "", "Essence 100 ml refill") == "100 ml"


# --------------------------------------------------------------------------- #
# reconcile_key_ingredients — trust order + provenance
# --------------------------------------------------------------------------- #

def test_reconcile_prefers_existing_active_ingredients_json():
    ings, prov = sa.reconcile_key_ingredients(
        active_ingredients_json=[{"label": "Niacinamide", "source": "inci"}],
        raw_inci="Water, Snail Secretion Filtrate",   # would yield Snail Mucin
        fallback_text="hyaluronic acid serum",
    )
    assert prov == "reconciled:beauty_sku_ingredients"
    assert [i["label"] for i in ings] == ["Niacinamide"]


def test_reconcile_active_ingredients_json_as_string():
    """A jsonb column handed back as a JSON string still reconciles."""
    ings, prov = sa.reconcile_key_ingredients(
        active_ingredients_json='[{"label": "Retinol", "source": "inci"}]',
    )
    assert prov == "reconciled:beauty_sku_ingredients"
    assert ings[0]["label"] == "Retinol"


def test_reconcile_falls_back_to_raw_inci_then_seed_then_text():
    ings, prov = sa.reconcile_key_ingredients(
        raw_inci="Aqua, Niacinamide, Glycerin",
    )
    assert prov == "deterministic:raw_inci"
    assert any(i["label"] == "Niacinamide" and i["source"] == "inci" for i in ings)

    ings2, prov2 = sa.reconcile_key_ingredients(seed_inci="Water, Centella Asiatica Extract")
    assert prov2 == "deterministic:seed_inci"
    assert any(i["source"] == "inci" for i in ings2)

    ings3, prov3 = sa.reconcile_key_ingredients(fallback_text="A gentle niacinamide toner")
    assert prov3 == "deterministic:text_fallback"
    assert any(i["source"] == "text" for i in ings3)


def test_reconcile_empty_is_honest_empty():
    ings, prov = sa.reconcile_key_ingredients(fallback_text="a lovely product")
    assert ings == []
    assert prov is None


# --------------------------------------------------------------------------- #
# reconcile_concerns — authored wins over inferred
# --------------------------------------------------------------------------- #

def test_reconcile_concerns_prefers_authored_json():
    concerns, prov = sa.reconcile_concerns(
        concerns_json=["aging", "dryness"],
        category_kind="skincare",
        description="brightening glow serum",  # would infer dullness
    )
    assert prov == "reconciled:beauty_product_profiles"
    assert concerns == ["aging", "dryness"]


def test_reconcile_concerns_infers_from_lexicon_when_unauthored():
    concerns, prov = sa.reconcile_concerns(
        category_kind="skincare",
        title="Hydrating essence",
        description="for dry, dull skin",
    )
    assert prov == "deterministic:concern_lexicon"
    assert "dryness" in concerns


def test_reconcile_concerns_empty_without_category_kind():
    concerns, prov = sa.reconcile_concerns(description="for dry skin")
    assert concerns == []
    assert prov is None


# --------------------------------------------------------------------------- #
# extract_deterministic — end to end, residual identification
# --------------------------------------------------------------------------- #

def test_extract_deterministic_resolves_and_leaves_judgment_residual():
    det = sa.extract_deterministic({
        "title": "COSRX Snail Mucin Essence 100ml",
        "description": "Hydrating essence for dry, dull skin. Fragrance-free.",
        "category_path": "beauty/skincare/essence",
    })
    a = det.attributes
    assert a["volume"] == "100 ml"
    assert a["format"] == "essence"
    assert a["fragrance_free"] is True
    assert "dryness" in a["concerns"]
    assert a["key_ingredients"]                     # snail mucin / HA from text
    assert det.category_kind == "skincare"
    # spf/finish unresolved (not present); skin_type/texture/finish are residual;
    # concerns resolved so NOT residual.
    assert "concerns" not in det.residual_fields
    assert set(det.residual_fields) == {"skin_type", "texture", "finish"}


def test_extract_deterministic_spf_and_finish_reconcile():
    det = sa.extract_deterministic({
        "title": "Daily Sunscreen SPF50 PA++++ 60ml",
        "description": "Lightweight UV protection.",
        "category_path": "beauty/skincare/sunscreen",
        "shade_json": [{"finish": "Matte"}],
    })
    assert det.attributes["spf"] == 50
    assert det.attributes["volume"] == "60 ml"
    assert det.attributes["finish"] == "matte"           # reconciled from shade
    assert "finish" not in det.residual_fields           # so not residual


def test_extract_deterministic_haircare_flags():
    det = sa.extract_deterministic({
        "title": "Repair Shampoo 300ml",
        "description": "Sulfate-free, silicone-free, certified vegan by the Vegan Society.",
        "category_path": "beauty/haircare/shampoo",
    })
    a = det.attributes
    assert a["format"] == "shampoo"
    assert a["sulfate_free"] is True
    assert a["silicone_free"] is True
    assert a["vegan_status"] == "verified"


# --------------------------------------------------------------------------- #
# residual coercion — the model cannot invent or overwrite
# --------------------------------------------------------------------------- #

def test_coerce_residual_clamps_to_vocabulary_and_requested_fields():
    out = sa.coerce_residual(
        {
            "skin_type": ["oily", "unicorn"],       # unicorn dropped
            "texture": "lightweight",
            "finish": "matte",                       # not requested -> dropped
            "spf": 50,                               # not a residual field -> ignored
        },
        residual_fields=["skin_type", "texture"],
    )
    assert out == {"skin_type": ["oily"], "texture": "lightweight"}


def test_coerce_residual_rejects_non_mapping():
    assert sa.coerce_residual(None, ["skin_type"]) == {}
    assert sa.coerce_residual(["oily"], ["skin_type"]) == {}


# --------------------------------------------------------------------------- #
# run_llm_residual — injected synthesize, typed outcomes (no network)
# --------------------------------------------------------------------------- #

def _synth_returning(text, finish_reason=None, usage=None):
    async def _s(**_kw):
        return {"text": text, "finish_reason": finish_reason, "usage": usage or {}}
    return _s


@pytest.mark.asyncio
async def test_run_llm_residual_ok_merges_only_requested():
    out = await sa.run_llm_residual(
        {"title": "Serum", "description": "for oily skin, gel texture"},
        ["skin_type", "texture"],
        synthesize=_synth_returning('{"skin_type":["oily"],"texture":"gel"}',
                                    usage={"input_tokens": 120, "output_tokens": 20}),
        provider="gemini", model="gemini-2.5-flash",
    )
    assert out.outcome == "ok"
    assert out.attributes == {"skin_type": ["oily"], "texture": "gel"}
    assert all(v == "llm:gemini-2.5-flash" for v in out.provenance.values())
    assert out.usage["input_tokens"] == 120


@pytest.mark.asyncio
async def test_run_llm_residual_no_residual_fields_is_empty_noop():
    out = await sa.run_llm_residual(
        {"title": "X", "description": "y"}, [],
        synthesize=_synth_returning("should not be called"),
        provider="gemini", model="m",
    )
    assert out.outcome == "empty"
    assert out.attributes == {}


@pytest.mark.asyncio
async def test_run_llm_residual_truncated_is_counted_not_swallowed():
    # Non-empty response that does not parse, WITH a length finish_reason -> the
    # known truncation failure. Must surface as 'truncated' (a parse failure the
    # runner counts), not a silent empty.
    out = await sa.run_llm_residual(
        {"title": "Serum", "description": "text"}, ["skin_type"],
        synthesize=_synth_returning('{"skin_type": ["oi', finish_reason="length"),
        provider="gemini", model="m",
    )
    assert out.outcome == "truncated"
    assert out.is_parse_failure is True
    assert out.attributes == {}


@pytest.mark.asyncio
async def test_run_llm_residual_parse_fail_vs_empty():
    garbage = await sa.run_llm_residual(
        {"title": "Serum", "description": "text"}, ["skin_type"],
        synthesize=_synth_returning("totally not json", finish_reason="stop"),
        provider="gemini", model="m",
    )
    assert garbage.outcome == "parse_fail"
    assert garbage.is_parse_failure is True

    empty = await sa.run_llm_residual(
        {"title": "Serum", "description": "text"}, ["skin_type"],
        synthesize=_synth_returning('{"skin_type": []}', finish_reason="stop"),
        provider="gemini", model="m",
    )
    assert empty.outcome == "empty"
    assert empty.is_parse_failure is False


@pytest.mark.asyncio
async def test_run_llm_residual_transport_error_is_counted():
    async def _boom(**_kw):
        raise RuntimeError("provider 500")
    out = await sa.run_llm_residual(
        {"title": "Serum", "description": "text"}, ["skin_type"],
        synthesize=_boom, provider="gemini", model="m",
    )
    assert out.outcome == "error"
    assert out.attributes == {}


# --------------------------------------------------------------------------- #
# build_envelope — versioned, deterministic wins
# --------------------------------------------------------------------------- #

def test_build_envelope_deterministic_wins_over_llm():
    det = sa.DeterministicResult(
        attributes={"concerns": ["dryness"], "volume": "50 ml"},
        provenance={"concerns": "reconciled:beauty_product_profiles",
                    "volume": "deterministic:volume_regex"},
        residual_fields=["skin_type", "concerns"],
        category_kind="skincare",
    )
    residual = sa.LLMResidualOutcome(
        attributes={"skin_type": ["oily"], "concerns": ["oiliness"]},  # concerns must NOT overwrite
        provenance={"skin_type": "llm:m", "concerns": "llm:m"},
        outcome="ok", model="m",
    )
    env = sa.build_envelope(det, residual, generated_at="2026-07-12T00:00:00Z")
    assert env["schema_version"] == sa.SCHEMA_VERSION
    assert env["vertical"] == "beauty"
    assert env["category_kind"] == "skincare"
    assert env["model"] == "m"
    # deterministic concerns preserved; llm skin_type added; llm concerns dropped
    assert env["attributes"]["concerns"] == ["dryness"]
    assert env["attributes"]["skin_type"] == ["oily"]
    assert env["provenance"]["concerns"] == "reconciled:beauty_product_profiles"
    assert env["provenance"]["skin_type"] == "llm:m"


def test_build_envelope_deterministic_only():
    det = sa.DeterministicResult(
        attributes={"volume": "50 ml"}, provenance={"volume": "deterministic:volume_regex"},
        residual_fields=[], category_kind=None,
    )
    env = sa.build_envelope(det, None, generated_at="2026-07-12T00:00:00Z")
    assert env["model"] is None
    assert env["attributes"] == {"volume": "50 ml"}
