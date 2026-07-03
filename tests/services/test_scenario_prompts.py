"""Scenario/occasion demand layer (plan P1+P2): scenario attribute class
(direct KR/EN mentions + evidence-edge inference), scenario prompt shapes with
the P0 coverage marker, and the junk-template gate."""
from __future__ import annotations

from services.sku_sidewalk import (
    build_sku_attribute_graph,
    generate_sidewalk_query_specs,
    is_scenario_slug,
)


def _specs(graph, product_type, n=40):
    return generate_sidewalk_query_specs(
        graph, title="t", product_type=product_type, n=n,
    )


def test_direct_scenario_mentions_en_and_kr():
    graph = build_sku_attribute_graph({
        "title": "아누코 본드 앤 리페어 헤어 오일 75ml",
        "product_type": "hair oil",
        "attributes_raw": {
            "tags": ["heat protection", "여행용"],
            "description": "고데기 열기구로부터 모발 보호. 휴대용 사이즈.",
        },
    })
    scenarios = graph["classes"]["scenario"]
    assert "heat-styling" in scenarios
    assert "travel" in scenarios
    # haircare lexicon pack: category + hero ingredient resolve from Korean
    assert "hair oil" in graph["classes"]["category"]


def test_scenario_evidence_edges_infer_with_provenance():
    graph = build_sku_attribute_graph({
        "title": "Good Night Collagen 30 sticks",
        "product_type": "collagen",
        "attributes_raw": {"tags": ["stick", "melatonin-free"]},
    })
    scenarios = graph["classes"]["scenario"]
    assert "travel" in scenarios     # stick format -> travel
    assert "jet-lag" in scenarios    # no melatonin -> jet-lag
    assert graph["evidence"]["jet-lag"].startswith("inferred from")


def test_reef_safe_sunscreen_infers_beach_and_swim_lanes():
    graph = build_sku_attribute_graph({
        "title": "Mineral Sunscreen Stick SPF50 reef-safe",
        "product_type": "sunscreen",
        "attributes_raw": {"tags": ["reef-safe"]},
    })
    scenarios = set(graph["classes"]["scenario"])
    assert {"beach", "swim"} <= scenarios
    queries = [s["query"] for s in _specs(graph, "sunscreen")]
    assert any("for the beach" in q for q in queries)


def test_scenario_shapes_and_coverage_marker():
    graph = build_sku_attribute_graph({
        "title": "아누코 본드 앤 리페어 헤어 오일",
        "product_type": "hair oil",
        "attributes_raw": {"tags": ["heat protection"]},
    })
    specs = _specs(graph, "hair oil")
    scenario_specs = [
        s for s in specs
        if any(str(b).startswith("scenario:") for b in s["attribute_basis"])
    ]
    assert scenario_specs, "expected scenario-framed prompts"
    queries = [s["query"] for s in scenario_specs]
    assert "best hair oil for heat styling" in queries
    # evidence resolves through the scenario: marker (provenance trail intact)
    assert any(s["evidence"] for s in scenario_specs)
    # packable shape only for packable scenarios (heat-styling is not)
    assert not any("to pack for heat styling" in q for q in queries)


def test_packable_shape_for_travel():
    graph = build_sku_attribute_graph({
        "title": "Collagen sticks",
        "product_type": "collagen",
        "attributes_raw": {"tags": ["travel", "stick"]},
    })
    queries = [s["query"] for s in _specs(graph, "collagen")]
    assert "what collagen to pack for travel" in queries


def test_what_helps_with_gated_to_non_scenario_terms():
    """Observed junk ('what helps with travel/summer/heat protection') must not
    generate; genuine routine terms ('before bed') still do."""
    from services.agent_center_bd_report_service import _build_per_sku_audit_query_records

    assert is_scenario_slug("travel") and is_scenario_slug("summer")
    assert not is_scenario_slug("before bed")

    ctx = {
        "sku_key": "s1", "merchant_id": "m1",
        "product": {
            "title": "Good Night Collagen",
            "product_type": "collagen",
            "attributes_raw": {"tags": ["travel", "before bed"]},
        },
    }
    queries = [str(r["query"]).lower() for r in _build_per_sku_audit_query_records(ctx, 40)]
    assert not any(q.startswith("what helps with travel") for q in queries)
    assert not any(q.startswith("what helps with summer") for q in queries)
    # scenario demand shows up through the proper shapes instead
    assert any("scenario:" in str(b) for r in _build_per_sku_audit_query_records(ctx, 40)
               for b in (r.get("attribute_basis") or []))


def test_scenario_shapes_respect_guardrails():
    """No medical/sleep phrasing can ride a scenario shape (guardrail intact)."""
    graph = build_sku_attribute_graph({
        "title": "Collagen sticks",
        "product_type": "collagen",
        "attributes_raw": {"tags": ["overnight", "stick"]},
    })
    for s in _specs(graph, "collagen"):
        assert "sleep" not in s["query"]
