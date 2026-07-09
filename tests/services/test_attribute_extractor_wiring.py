"""Phase 2b — live wiring of the LLM attribute extractor (flag-gated).

Verifies: flag OFF is a no-op (no LLM, no stash — every existing audit is
unchanged); flag ON extracts for lexicon-thin SKUs and the grounded attributes
merge into the probe-seeding graph; beauty with lexicon hits is skipped (no LLM);
and the merge helper with no stash equals the plain lexicon graph.
"""
import json

import pytest

import services.agent_center_bd_report_service as R
from config.settings import settings

MOJAWA = {
    "title": "Mojawa Purra Swim",
    "product_type": "Bone Conduction Headphones",
    "attributes_raw": {
        "description": (
            "Purra Swim is an open-ear bone conduction headphone with IP68 "
            "waterproof rating, built for swimming with 32GB of onboard MP3 storage."
        ),
    },
}
# Beauty SKU whose lexicon path finds attributes -> extractor must be skipped.
BEAUTY = {
    "title": "COSRX Advanced Snail Mucin Essence",
    "product_type": "Beauty/Skincare/Essence",
    "attributes_raw": {"description": "snail mucin essence", "tags": ["snail", "essence"]},
}

_LLM_OUT = {
    "attributes": [
        {"class_name": "format", "value": "open-ear", "span": "is an open-ear bone conduction"},
        {"class_name": "certification_constraint", "value": "IP68", "span": "IP68 waterproof rating"},
        {"class_name": "use_case", "value": "swimming", "span": "built for swimming with"},
        {"class_name": "ingredient", "value": "32GB storage", "span": "32GB of onboard MP3 storage"},
        {"class_name": "certification_constraint", "value": "ANC", "span": "active noise cancellation"},  # hallucination
    ]
}


def _fake_synth_factory(counter):
    return _fake_synth_out(_LLM_OUT, counter)


def _fake_synth_out(payload, counter):
    async def fake_synth(**kwargs):
        counter["n"] += 1
        return {"text": json.dumps(payload), "provider": kwargs["provider"], "model": kwargs["model"]}
    return fake_synth


@pytest.mark.asyncio
async def test_flag_off_is_a_noop(monkeypatch):
    monkeypatch.setattr(settings, "attribute_extractor_enabled", False)
    counter = {"n": 0}
    monkeypatch.setattr("services.llm_synthesis.synthesize", _fake_synth_factory(counter))
    ctx = {"product": MOJAWA, "vertical": "electronics"}
    await R._maybe_stash_llm_attributes(ctx)
    assert counter["n"] == 0
    assert R._LLM_ATTR_STASH_KEY not in ctx


@pytest.mark.asyncio
async def test_flag_on_electronics_extracts_and_merges(monkeypatch):
    monkeypatch.setattr(settings, "attribute_extractor_enabled", True)
    monkeypatch.setattr(R, "_resolve_extractor_provider", lambda: ("deepseek", "deepseek-chat"))
    counter = {"n": 0}
    monkeypatch.setattr("services.llm_synthesis.synthesize", _fake_synth_factory(counter))

    ctx = {"product": MOJAWA, "vertical": "electronics"}
    await R._maybe_stash_llm_attributes(ctx)

    assert counter["n"] == 1
    stash = ctx[R._LLM_ATTR_STASH_KEY]
    assert len(stash) >= 4                       # hallucinated ANC dropped by the guard
    values = {g.value for g in stash}
    assert "IP68" in values and "swimming" in values and "ANC" not in values

    # grounded attrs merge into the probe-seeding graph
    graph = R._attribute_graph_for_probes(ctx, MOJAWA)
    assert "IP68" in graph["classes"]["certification_constraint"]
    assert graph["evidence"]["IP68"] == "llm_extracted"


@pytest.mark.asyncio
async def test_flag_on_beauty_with_lexicon_skips_llm(monkeypatch):
    monkeypatch.setattr(settings, "attribute_extractor_enabled", True)
    monkeypatch.setattr(R, "_resolve_extractor_provider", lambda: ("deepseek", "deepseek-chat"))
    counter = {"n": 0}
    monkeypatch.setattr("services.llm_synthesis.synthesize", _fake_synth_factory(counter))

    # sanity: the beauty lexicon actually finds something for this SKU
    assert R._stashed_grounded_attributes({}) == []
    from services.sku_sidewalk import build_sku_attribute_graph
    from services.llm_attribute_extractor import should_run_extractor
    from services.vertical_profiles import get_profile
    assert should_run_extractor(get_profile("beauty"), build_sku_attribute_graph(BEAUTY)) is False

    ctx = {"product": BEAUTY, "vertical": "beauty"}
    await R._maybe_stash_llm_attributes(ctx)
    assert counter["n"] == 0                       # lexicon-first: no LLM cost
    assert R._LLM_ATTR_STASH_KEY not in ctx


def test_merge_helper_no_stash_equals_plain_graph():
    from services.sku_sidewalk import build_sku_attribute_graph
    plain = build_sku_attribute_graph(MOJAWA)
    merged = R._attribute_graph_for_probes({"product": MOJAWA}, MOJAWA)
    assert merged == plain                         # flag off / no stash -> identical


# A lexicon-thin own page: title + type only. The rescue material (IP68 / swim)
# lives ONLY in the recycled retailer excerpt, never on the own page.
THIN = {
    "title": "Acme X1",
    "product_type": "Bone Conduction Headphones",
}
_RETAILER_LLM_OUT = {
    "attributes": [
        {
            "class_name": "certification_constraint",
            "value": "IP68",
            "span": "rated IP68 waterproof for swimming",
        },
    ]
}


@pytest.mark.asyncio
async def test_extractor_grounds_in_recycled_retailer_excerpts(monkeypatch):
    """Regression: the extractor runs INSIDE load_sku_context, before the probe
    loop stashes _retailer_excerpts, so it must self-load prior retailer evidence
    or it can never ground attributes that live only on Tier-1 retailer listings —
    the exact rescue path for thin url-wedge pages."""
    monkeypatch.setattr(settings, "attribute_extractor_enabled", True)
    monkeypatch.setattr(R, "_resolve_extractor_provider", lambda: ("deepseek", "deepseek-chat"))
    counter = {"n": 0}
    monkeypatch.setattr(
        "services.llm_synthesis.synthesize", _fake_synth_out(_RETAILER_LLM_OUT, counter)
    )

    async def fake_evidence(*, merchant_id, sku_key, **_kw):
        assert merchant_id == "merch_1" and sku_key == "urlwedge:abc"
        return {
            "excerpts": ["Acme X1 is rated IP68 waterproof for swimming with 32GB storage."],
            "hosts": ["rtings.com"],
        }

    monkeypatch.setattr("services.retailer_evidence.load_prior_retailer_evidence", fake_evidence)

    product = dict(THIN)
    ctx = {
        "product": product,
        "vertical": "electronics",
        "merchant_id": "merch_1",
        "sku_key": "urlwedge:abc",
    }
    await R._maybe_stash_llm_attributes(ctx)

    assert counter["n"] == 1
    values = {g.value for g in (ctx.get(R._LLM_ATTR_STASH_KEY) or [])}
    assert "IP68" in values                       # grounded in the recycled excerpt
    # excerpt now lives on the product for the downstream graph/prompt consumers
    assert product.get("_retailer_excerpts")


@pytest.mark.asyncio
async def test_extractor_drops_retailer_only_span_when_no_evidence(monkeypatch):
    """Control: same LLM output, but no prior retailer evidence — the span isn't
    in the own-page source, so the groundedness guard drops it. Proves it's the
    excerpt (not the own page) that grounds the attribute."""
    monkeypatch.setattr(settings, "attribute_extractor_enabled", True)
    monkeypatch.setattr(R, "_resolve_extractor_provider", lambda: ("deepseek", "deepseek-chat"))
    counter = {"n": 0}
    monkeypatch.setattr(
        "services.llm_synthesis.synthesize", _fake_synth_out(_RETAILER_LLM_OUT, counter)
    )

    async def no_evidence(**_kw):
        return {"excerpts": [], "hosts": []}

    monkeypatch.setattr("services.retailer_evidence.load_prior_retailer_evidence", no_evidence)

    product = dict(THIN)
    ctx = {
        "product": product,
        "vertical": "electronics",
        "merchant_id": "merch_1",
        "sku_key": "urlwedge:abc",
    }
    await R._maybe_stash_llm_attributes(ctx)

    values = {g.value for g in (ctx.get(R._LLM_ATTR_STASH_KEY) or [])}
    assert "IP68" not in values                   # own-page source lacks the span
    assert not product.get("_retailer_excerpts")


@pytest.mark.asyncio
async def test_transport_error_leaves_ctx_untouched(monkeypatch):
    monkeypatch.setattr(settings, "attribute_extractor_enabled", True)
    monkeypatch.setattr(R, "_resolve_extractor_provider", lambda: ("deepseek", "deepseek-chat"))

    async def boom(**kwargs):
        raise RuntimeError("provider 503")

    monkeypatch.setattr("services.llm_synthesis.synthesize", boom)
    ctx = {"product": MOJAWA, "vertical": "electronics"}
    await R._maybe_stash_llm_attributes(ctx)       # must not raise
    assert R._LLM_ATTR_STASH_KEY not in ctx
