"""Phase 2 — LLM attribute extractor + the groundedness guard.

The guard is the load-bearing piece (a hallucinated attribute becomes a real,
confidently-wrong audit probe), so it gets the bulk of the coverage. The Mojawa
acceptance test proves the extracted, grounded attributes flow into the sidewalk
probe generator.
"""
import json

import pytest

from services import llm_attribute_extractor as ax
from services.sku_sidewalk import build_sku_attribute_graph, generate_sidewalk_query_specs
from services.vertical_profiles import get_profile

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


def _mojawa_source():
    return ax.build_source_text(MOJAWA)


# --------------------------- the groundedness guard --------------------------- #

def test_guard_keeps_attributes_quoted_from_source():
    src = "an open-ear bone conduction headphone with IP68 waterproof rating for swimming"
    items = [
        {"class_name": "format", "value": "open-ear", "span": "an open-ear bone conduction"},
        {"class_name": "certification_constraint", "value": "IP68", "span": "with IP68 waterproof"},
        {"class_name": "use_case", "value": "swimming", "span": "rating for swimming"},
    ]
    grounded = ax.ground_extracted_attributes(items, src)
    assert {g.value for g in grounded} == {"open-ear", "IP68", "swimming"}
    # every grounded attribute retains its substantiating span
    assert all(g.span for g in grounded)


def test_guard_discards_span_not_in_source():
    src = "bone conduction headphone, waterproof, for swimming"
    items = [{"class_name": "certification_constraint", "value": "ANC",
              "span": "active noise cancellation"}]  # span never appears in src
    assert ax.ground_extracted_attributes(items, src) == []


def test_guard_discards_value_drifted_from_its_span():
    src = "IP68 waterproof rating for the pool"
    # span is real, but the value's tokens are not in it -> smuggled invention.
    items = [{"class_name": "ingredient", "value": "wireless charging",
              "span": "IP68 waterproof rating"}]
    assert ax.ground_extracted_attributes(items, src) == []


def test_guard_requires_span_and_value_and_valid_class():
    src = "bone conduction headphone for swimming"
    items = [
        {"class_name": "format", "value": "open-ear", "span": ""},          # no span
        {"class_name": "format", "value": "", "span": "bone conduction"},   # no value
        {"class_name": "not_a_class", "value": "x", "span": "bone conduction"},  # bad class
        {"class_name": "use_case", "value": "for the", "span": "for swimming"},  # all-stopword value
    ]
    assert ax.ground_extracted_attributes(items, src) == []


def test_guard_drops_promo_terms_and_dedupes():
    src = "free shipping bone conduction bone conduction for swimming"
    items = [
        {"class_name": "use_case", "value": "free shipping", "span": "free shipping bone"},
        {"class_name": "format", "value": "bone conduction", "span": "conduction bone conduction"},
        {"class_name": "format", "value": "Bone Conduction", "span": "bone conduction for"},  # dup (case)
    ]
    grounded = ax.ground_extracted_attributes(items, src)
    assert [(g.class_name, g.value) for g in grounded] == [("format", "bone conduction")]


def test_guard_empty_source_returns_empty():
    assert ax.ground_extracted_attributes(
        [{"class_name": "format", "value": "open-ear", "span": "open-ear"}], ""
    ) == []


# ------------------------------- lexicon-first ------------------------------- #

def test_gating_electronics_always_runs():
    assert ax.should_run_extractor(get_profile("electronics"), {"classes": {}}) is True
    # even if the (beauty-lexicon) graph happened to find something.
    assert ax.should_run_extractor(get_profile("electronics"), {"classes": {"category": ["x"]}}) is True


def test_gating_beauty_skips_when_lexicon_found_attributes():
    beauty = get_profile("beauty")
    graph_with_hits = {"classes": {"category": ["serum"], "ingredient": ["niacinamide"]}}
    assert ax.should_run_extractor(beauty, graph_with_hits) is False


def test_gating_beauty_runs_when_lexicon_empty():
    # thin-Korean-SKU case (#1126): lexicon found nothing -> the LLM may help.
    beauty = get_profile("beauty")
    assert ax.should_run_extractor(beauty, {"classes": {name: [] for name in ax._MEANINGFUL_CLASSES}}) is True


# --------------------------- Mojawa acceptance (mocked) --------------------------- #

def _mojawa_llm_attributes():
    """Model output: 5 real (grounded) + 2 hallucinated (must be dropped)."""
    return {
        "attributes": [
            {"class_name": "category", "value": "bone conduction headphone",
             "span": "open-ear bone conduction headphone with IP68"},
            {"class_name": "format", "value": "open-ear",
             "span": "is an open-ear bone conduction"},
            {"class_name": "certification_constraint", "value": "IP68",
             "span": "IP68 waterproof rating"},
            {"class_name": "use_case", "value": "swimming",
             "span": "built for swimming with"},
            {"class_name": "ingredient", "value": "32GB storage",
             "span": "32GB of onboard MP3 storage"},
            # hallucinations:
            {"class_name": "certification_constraint", "value": "ANC",
             "span": "active noise cancellation"},              # span not in source
            {"class_name": "ingredient", "value": "wireless charging",
             "span": "IP68 waterproof rating"},                 # value drift
        ]
    }


@pytest.mark.asyncio
async def test_mojawa_extracts_at_least_four_grounded_attributes():
    async def fake_synthesize(**kwargs):
        # the model is handed the source text and told to quote spans
        assert "TEXT:" in kwargs["user"]
        return {"text": json.dumps(_mojawa_llm_attributes()), "provider": kwargs["provider"],
                "model": kwargs["model"]}

    grounded = await ax.extract_attributes(
        MOJAWA, synthesize=fake_synthesize, provider="deepseek", model="deepseek-chat"
    )
    values = {g.value for g in grounded}
    assert len(grounded) >= 4, values
    assert "IP68" in values and "swimming" in values and "open-ear" in values
    # the two hallucinations were dropped
    assert "ANC" not in values and "wireless charging" not in values


@pytest.mark.asyncio
async def test_mojawa_grounded_attributes_feed_sidewalk_probes():
    async def fake_synthesize(**kwargs):
        return {"text": json.dumps(_mojawa_llm_attributes()), "provider": kwargs["provider"],
                "model": kwargs["model"]}

    grounded = await ax.extract_attributes(
        MOJAWA, synthesize=fake_synthesize, provider="deepseek", model="deepseek-chat"
    )
    # lexicon path finds nothing (beauty lexicons) -> merge the grounded attrs.
    graph = build_sku_attribute_graph(MOJAWA)
    ax.merge_grounded_into_graph(graph, grounded)
    assert graph["classes"]["certification_constraint"]  # IP68 landed
    assert graph["evidence"]["IP68"] == "llm_extracted"
    assert graph["llm_extracted_spans"]["IP68"]          # span retained

    specs = generate_sidewalk_query_specs(
        graph, title="Mojawa Purra Swim", product_type="bone conduction headphones", n=12
    )
    queries = " ".join(str(s.get("query") or "") for s in specs).lower()
    assert specs, "expected sidewalk probes from grounded electronics attributes"
    assert "swimming" in queries or "ip68" in queries or "bone conduction" in queries


@pytest.mark.asyncio
async def test_extractor_transport_error_returns_empty_not_fabricated():
    async def boom(**kwargs):
        raise RuntimeError("provider 503")

    grounded = await ax.extract_attributes(
        MOJAWA, synthesize=boom, provider="deepseek", model="deepseek-chat"
    )
    assert grounded == []
