"""Deep-tier prompt blocks (slice 2 — spec 2026-07-21 §3).

Contracts under test:
  - build_deep_tier_specs emits the market-referential blocks (comparison /
    incumbent-contest / price band / market availability / recency / routine)
    with the spec'd axes, and each block is gated on its inputs (no
    competitors → no comparison; no USD price → no band shape; no korean
    origin signal → no korean shape);
  - standard runs are byte-unchanged: without the _audit_tier stash the base
    spec builder emits zero deep shapes and zero comparison axes;
  - deep records ride the budgeter with a "deep_tier" source stamp and carry
    their axis (incl. "comparison") into axis_metadata;
  - internal-first gate (founder 2026-07-21): comparison-axis rows/runs are
    excluded from every merchant-facing surface fed by build_sku_opportunity's
    per_prompt, from _failing_prompts, from _query_class_coverage, and from
    the category-competitor panel — while raw probe payloads keep them;
  - the prior-run competitor harvest ranks cited names and excludes the
    merchant's own brand.
"""
from __future__ import annotations

from typing import Any, Dict, List

from services import agent_center_bd_report_service as m
from services.deep_tier_prompts import (
    build_deep_tier_specs,
    harvest_competitor_brands,
)
from services.vertical_profiles import BEAUTY_PROFILE


# ---- build_deep_tier_specs (pure) -------------------------------------------

def _full_specs() -> List[tuple]:
    return build_deep_tier_specs(
        title="VODANA Triple Flow Iron",
        category="flat iron",
        competitors=["GHD", "Dyson Airwrap", "BaByliss"],
        price_band_usd=70,
        origin_terms=["korean"],
        market="US",
        year=2026,
        routine_shapes=("is {title} safe for daily use",),
    )


def test_block_a_comparison_shapes_from_seeds():
    axis_by_query = dict(_full_specs())
    assert axis_by_query["VODANA Triple Flow Iron vs GHD"] == "comparison"
    assert axis_by_query["best alternatives to Dyson Airwrap"] == "comparison"
    assert axis_by_query["is VODANA Triple Flow Iron better than GHD"] == "comparison"
    assert axis_by_query["cheaper alternative to GHD"] == "comparison"


def test_block_b_incumbent_contest_shapes():
    axis_by_query = dict(_full_specs())
    assert axis_by_query["is GHD worth it"] == "comparison"
    assert axis_by_query["problems with GHD"] == "comparison"
    # The merchant is absent from the prompt — does AI volunteer them?
    assert axis_by_query["Dyson Airwrap vs BaByliss"] == "comparison"


def test_blocks_c_d_e_f_axes_and_gating():
    axis_by_query = dict(_full_specs())
    assert axis_by_query["best flat iron under $70"] == "category"
    # Branded value question stays in the branded class (intent), never
    # category discovery.
    assert axis_by_query["is VODANA Triple Flow Iron worth the money"] == "intent"
    assert axis_by_query["best korean flat iron available in the US"] == "intent"
    assert axis_by_query["does VODANA Triple Flow Iron ship to the US"] == "intent"
    assert axis_by_query["best flat iron 2026"] == "category"
    assert axis_by_query["is VODANA Triple Flow Iron safe for daily use"] == "attribute"


def test_blocks_gate_on_their_inputs():
    no_extras = build_deep_tier_specs(
        title="X Serum", category="serum", competitors=[],
        price_band_usd=None, origin_terms=[], year=None,
    )
    queries = [q for q, _ in no_extras]
    assert not any(axis == "comparison" for _, axis in no_extras)
    assert not any("under $" in q for q in queries)
    assert not any("korean" in q for q in queries)
    assert not any("2026" in q for q in queries)
    # Ship-to availability shapes are NOT origin-gated.
    assert "does X Serum ship to the US" in queries


def test_routine_template_with_empty_field_is_omitted():
    specs = build_deep_tier_specs(
        title="", category="serum",
        routine_shapes=("what should I pair with {title}",),
    )
    assert not any("pair with" in q for q, _ in specs)


# ---- competitor harvest ------------------------------------------------------

def test_harvest_ranks_cited_competitors_and_excludes_own_brand():
    report = {
        "brand_report": {
            "per_sku_reports": [{
                "sku_key": "sku-1",
                "opportunity": {"per_prompt": [
                    {"competitors": ["GHD", "BaByliss"]},
                    {"competitors": ["GHD", "VODANA"]},
                ]},
            }],
            "category_visibility": {
                "competitor_brands": [
                    {"name": "Dyson Airwrap", "times_cited": 5},
                    {"name": "vodana", "times_cited": 9},
                ],
            },
        },
    }
    counts = harvest_competitor_brands(
        report, sku_key="sku-1", own_brand="VODANA",
    )
    assert counts["Dyson Airwrap"] == 5 and counts["GHD"] == 2
    assert not any("vodana" in name.lower() for name in counts)


# ---- spec-builder integration ------------------------------------------------

def _deep_ctx() -> Dict[str, Any]:
    return {
        "product": {
            "title": "Good Night Collagen",
            "brand": "BB Lab",
            "product_type": "collagen",
            "attributes_raw": {"ingredients": "marine collagen"},
        },
        "sku": {"title": "2 Box"},
        "sku_title": "2 Box",
        "sku_key": "gnc-2box",
        "_audit_tier": "deep",
        "_deep_competitor_seeds": ["Vital Proteins"],
    }


def test_standard_ctx_stays_byte_unchanged():
    ctx = _deep_ctx()
    standard_ctx = {k: v for k, v in ctx.items() if not k.startswith("_")}
    specs, _, _ = m._build_per_sku_base_query_specs(standard_ctx)
    assert not any(axis == "comparison" for _, axis in specs)
    assert "_deep_tier_queries" not in standard_ctx


def test_deep_ctx_appends_blocks_and_stashes_priority_set():
    ctx = _deep_ctx()
    specs, title, _ = m._build_per_sku_base_query_specs(ctx)
    axis_by_query = dict(specs)
    # Probe identity is the resolved SKU identity (brand-prefixed title).
    assert axis_by_query[f"{title} vs Vital Proteins"] == "comparison"
    assert axis_by_query["is Vital Proteins worth it"] == "comparison"
    assert f"{title.lower()} vs vital proteins" in ctx["_deep_tier_queries"]


def test_deep_records_stamped_and_carry_axis_metadata():
    ctx = _deep_ctx()
    records = m._build_per_sku_audit_query_records(ctx, 80)
    deep = [r for r in records if r.get("source") == "deep_tier"]
    assert deep, "deep records must survive the 80-prompt budget"
    comparison = [r for r in deep if r.get("axis") == "comparison"]
    assert comparison, "comparison records must survive the budget"
    metadata = m._query_metadata_from_records(records)
    entry = metadata[comparison[0]["query"]]
    assert entry["axis"] == "comparison"
    assert entry["prompt_source"] == "deep_tier"


def test_standard_records_at_40_have_no_deep_stamps():
    ctx = {k: v for k, v in _deep_ctx().items() if not k.startswith("_")}
    records = m._build_per_sku_audit_query_records(ctx, 40)
    assert not any(r.get("source") == "deep_tier" for r in records)
    assert not any(r.get("axis") == "comparison" for r in records)


def test_first_deep_audit_seeds_from_profile_incumbents():
    # No prior runs → empty harvested seeds. The profile's configured
    # incumbents (GHD / Dyson Airwrap on beauty_device_hair) must still fire
    # Blocks A/B — the pilot's flagship case is a FIRST deep audit.
    ctx = {
        "product": {
            "title": "Professional Softbar Flat Iron",
            "brand": "VODANA",
            "product_type": "Flat Iron",
            "attributes_raw": {"benefits": "smooth styling"},
        },
        "sku": {"title": "Mint"},
        "sku_title": "Mint",
        "sku_key": "vd-softbar",
        "vertical": "beauty",
        "_audit_tier": "deep",
        "_deep_competitor_seeds": [],
    }
    specs, title, _ = m._build_per_sku_base_query_specs(ctx)
    axis_by_query = dict(specs)
    assert axis_by_query.get(f"{title} vs GHD") == "comparison"
    assert axis_by_query.get("is GHD worth it") == "comparison"


def test_loader_filter_strips_internal_comparison_but_keeps_payload_keys():
    payloads = [{
        "provider": "gemini",
        "prompt_basis_meta": {"prompt_set_id": "ps_x"},
        "raw_runs": [_comparison_run(), _category_run()],
    }]
    visible = m._merchant_visible_probe_payloads(payloads)
    assert [r["query"] for r in visible[0]["raw_runs"]] == ["best collagen"]
    # Riding keys (basis meta) survive; the original payload is not mutated.
    assert visible[0]["prompt_basis_meta"] == {"prompt_set_id": "ps_x"}
    assert len(payloads[0]["raw_runs"]) == 2


def test_comparison_records_survive_basis_pinning_with_stamp():
    # W2.1 reprobes the pinned selected set verbatim on re-runs; the deep-tier
    # source must survive that round-trip or the internal-first gate would
    # silently stop firing from the second deep run onward.
    from services.prompt_basis import clean_selected_specs

    pinned = clean_selected_specs([
        {"query": "X vs Y", "axis": "comparison", "source": "deep_tier"},
    ])
    assert pinned[0]["axis"] == "comparison"
    assert pinned[0]["source"] == "deep_tier"


# ---- internal-first gates ----------------------------------------------------

def _comparison_run(query: str = "collagen x vs y") -> Dict[str, Any]:
    return {
        "query": query,
        "_provider": "gemini",
        "raw": "answer text",
        "parsed": {"competitors_appearing": ["Vital Proteins"], "sku_mentioned": False},
        "grounding_sources": [],
        "grounding_chunks": [],
        "url_match": {"in_grounding": False, "llm_self_report": {}},
        # Both keys: the internal-first gate requires the deep-tier stamp AND
        # the comparison axis (a bare axis="comparison" keeps legacy behavior).
        "axis_metadata": {
            "axis": "comparison",
            "prompt_source": "deep_tier",
            "sku_key": "sku-1",
        },
    }


def _category_run(query: str = "best collagen") -> Dict[str, Any]:
    run = _comparison_run(query)
    run["axis_metadata"] = {"axis": "category", "sku_key": "sku-1"}
    run["parsed"] = {"competitors_appearing": ["Sports Research"], "sku_mentioned": False}
    return run


def test_bare_comparison_axis_without_stamp_keeps_legacy_behavior():
    # Pre-existing paths (and stored reports) can carry axis="comparison"
    # WITHOUT the deep-tier stamp — those must keep flowing to every surface.
    bare = _comparison_run("bb lab collagen alternatives")
    bare["axis_metadata"] = {"axis": "comparison", "sku_key": "sku-1"}
    failing = m._failing_prompts(_payload([bare]))
    assert [entry["query"] for entry in failing] == ["bb lab collagen alternatives"]
    counts = m._query_class_coverage(_payload([bare]))
    assert sum(counts.values()) == 1


def _payload(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"provider": "gemini", "probe_run_id": "p1", "raw_runs": runs}]


def test_per_prompt_choke_point_drops_comparison_rows():
    from services.sku_opportunity import build_sku_opportunity

    ctx = {"product": {"title": "Good Night Collagen", "brand": "BB Lab"}}
    opportunity = build_sku_opportunity(
        ctx,
        _payload([_comparison_run(), _category_run()]),
        attribute_graph={},
        vertical_profile=BEAUTY_PROFILE,
    )
    queries = [row["query"] for row in opportunity["per_prompt"]]
    assert "best collagen" in [q.lower() for q in queries]
    assert not any("vs" in q.lower() for q in queries)
    assert not any(
        str(row.get("axis") or "") == "comparison"
        for row in opportunity["per_prompt"]
    )


def test_failing_prompts_skip_comparison_runs():
    failing = m._failing_prompts(_payload([_comparison_run(), _category_run()]))
    queries = [entry["query"].lower() for entry in failing]
    assert "best collagen" in queries
    assert not any("vs" in q for q in queries)


def test_query_class_coverage_excludes_comparison_runs():
    counts = m._query_class_coverage(_payload([_comparison_run(), _category_run()]))
    assert sum(counts.values()) == 1


def test_category_competitor_panel_ignores_comparison_runs():
    brands, _hosts = m.extract_category_competitors(
        [_comparison_run(), _category_run()],
        merchant_host="bblab.shop",
        merchant_brand="BB Lab",
    )
    names = {str(item.get("name") or "") for item in brands}
    # The organic run's competitor counts; the comparison run's must not —
    # naming a competitor in the question guarantees it appears in the answer.
    assert "Sports Research" in names
    assert "Vital Proteins" not in names
