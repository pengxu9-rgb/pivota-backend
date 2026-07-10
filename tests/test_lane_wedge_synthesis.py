"""Lane/wedge synthesis must agree with the spec-matched prompt strategy.

Three defects made the report's ADVICE contradict its own probe set (found
reading the live Mojawa runs 7420c2b5/cc8d3a76 as an operator):

  1. LLM winnable/scenario prompts carry axis="category", so the wedge
     classified them as HEAD-prompt pressure and told the merchant NOT to
     chase exactly the specific queries generated for them to win
     ("bone conduction headphones for swimming without phone" ->
     do_not_chase_yet, reason "Broad category prompts...").
  2. open_lane required NO named competitor + density<=0.45 — structurally
     impossible in any established category (audio: Shokz is name-dropped on
     nearly every query), so open_lanes was always 0 and the sideways-wedge
     promise died. A lane whose ROUTE is weak (fragmented sources, low
     concentration, no first-party competitor) is still winnable.
  3. Win-plan losing queries with no publisher target emitted win_condition
     null — a dead end on precisely the winnable long-tail. No controlling
     publisher means the merchant's OWN content can win the answer.
"""

from services.sku_lane_priority import (
    _is_head_prompt_pressure,
    _is_sideways_wedge_lane,
    build_sideways_wedge,
)
from services.sku_opportunity import _is_open_lane
from services.win_plan_builder import _losing_query_plan


# --- 1. classifier: LLM prompts are sideways, never head pressure ----------

def _row(query, *, axis="category", prompt_source=None, **kw):
    row = {
        "query": query,
        "axis": axis,
        "query_class": kw.pop("query_class", axis),
        "ownership_state": kw.pop("ownership_state", "marketplace-owned"),
        "source_route": kw.pop("source_route", "marketplace"),
        "demand_signal": kw.pop("demand_signal", 1.0),
        "opportunity_score": kw.pop("opportunity_score", 5.0),
        "attribute_basis": kw.pop("attribute_basis", []),
    }
    if prompt_source:
        row["prompt_source"] = prompt_source
    row.update(kw)
    return row


def test_llm_winnable_row_is_sideways_not_head():
    row = _row(
        "bone conduction headphones for swimming without phone",
        prompt_source="llm_winnable",
    )
    assert _is_sideways_wedge_lane(row)
    assert not _is_head_prompt_pressure(row)


def test_llm_scenario_row_is_sideways_not_head():
    row = _row(
        "what headphones are good for lap swimming workouts?",
        prompt_source="llm_scenario",
    )
    assert _is_sideways_wedge_lane(row)
    assert not _is_head_prompt_pressure(row)


def test_plain_category_row_is_still_head_pressure():
    row = _row("best headphones")
    assert _is_head_prompt_pressure(row)
    assert not _is_sideways_wedge_lane(row)


def test_best_prefixed_llm_prompt_not_head_pressure():
    # The _BROAD_HEAD_PHRASES "best " prefix must not override the generator
    # stamp — specificity comes from the generator contract, not the prefix.
    row = _row(
        "best ip68 waterproof headphones for triathletes",
        prompt_source="llm_winnable",
    )
    assert not _is_head_prompt_pressure(row)


# --- 2. wedge integration: winnable prompts never land in do_not_chase -----

def test_winnable_prompt_not_in_do_not_chase():
    rows = [
        _row("best headphones"),  # genuine head term
        _row(
            "bone conduction headphones for swimming without phone",
            prompt_source="llm_winnable",
        ),
        _row(
            "ip68 waterproof bone conduction headphones open-ear",
            axis="sidewalk",
            query_class="sidewalk",
            attribute_basis=["ip68 waterproof", "bone conduction headphones"],
        ),
    ]
    wedge = build_sideways_wedge(rows)
    do_not_queries = {c["query"] for c in wedge["do_not_chase_yet"]}
    sideways_queries = {c["query"] for c in wedge["sideways_wedge_lanes"]}
    assert (
        "bone conduction headphones for swimming without phone"
        not in do_not_queries
    ), "LLM winnable prompt must not be deferred as a broad head prompt"
    assert "bone conduction headphones for swimming without phone" in sideways_queries
    # the genuine head term still gets the head treatment
    assert "best headphones" in do_not_queries or not wedge["do_not_chase_yet"]
    head_queries = {c["query"] for c in wedge["head_prompt_pressure"]}
    assert "best headphones" in head_queries


# --- 3. open lane: weakly-held routes stay open --------------------------

_OPEN_LANE_BASE = dict(
    query_class="category",
    demand_signal=1.0,
    source_route="fragmented",
    density_score=0.57,
    density_features={"source_concentration": 0.2, "first_party_competitor": False},
    attribute_fit=1.0,
    intent_weight=0.98,
    actionability=0.85,
    provider_analysis={"gemini": {}},
)


def test_open_lane_weakly_held_allows_namedropped_competitor():
    # Shokz name-dropped twice but citations fragmented, low concentration,
    # nobody grounded first-party -> the lane is weakly held and stays open.
    assert _is_open_lane(durable_competitor="Shokz", **_OPEN_LANE_BASE)


def test_open_lane_durable_competitor_still_blocks_on_owned_route():
    kw = dict(_OPEN_LANE_BASE)
    kw["source_route"] = "brand"          # not fragmented -> not weakly held
    kw["density_score"] = 0.40
    assert not _is_open_lane(durable_competitor="Shokz", **kw)


def test_open_lane_controlled_route_blocks():
    kw = dict(_OPEN_LANE_BASE)
    kw["source_route"] = "marketplace"    # a controller owns the route
    assert not _is_open_lane(durable_competitor=None, **kw)


def test_open_lane_merchant_mention_still_blocks():
    kw = dict(_OPEN_LANE_BASE)
    kw["provider_analysis"] = {"gemini": {"merchant_mention": True}}
    assert not _is_open_lane(durable_competitor=None, **kw)


def test_open_lane_weakly_held_density_ceiling():
    kw = dict(_OPEN_LANE_BASE)
    kw["density_score"] = 0.70            # above even the relaxed ceiling
    assert not _is_open_lane(durable_competitor=None, **kw)


def test_open_lane_first_party_competitor_defeats_weakly_held():
    kw = dict(_OPEN_LANE_BASE)
    kw["density_features"] = {
        "source_concentration": 0.2,
        "first_party_competitor": True,
    }
    assert not _is_open_lane(durable_competitor="Shokz", **kw)


# --- 4. win plan: own-content win condition instead of a dead end ----------

def test_win_plan_own_content_win_condition_when_no_publisher():
    plan = _losing_query_plan(
        {
            "query": "bone conduction headphones for swimming without phone",
            "axis": "category",
            "grounding_sources": [],
            "competitors_named": [],
        },
        {},
        merchant_name="Mojawa",
        merchant_category="Headphones",
    )
    assert plan["win_path"] == "own_content"
    assert plan["win_condition"], "no-publisher lanes must not dead-end"
    assert "your own" in plan["win_condition"].lower()
    assert plan["grounds_in"] == []
    # the honest limit stays — we still say WHY there's no publisher target
    assert plan["limit"]


def test_win_plan_publisher_condition_unchanged():
    uri_index = {
        "u1": {"host": "techradar.com", "citation_role": "editorial_review"},
    }
    plan = _losing_query_plan(
        {
            "query": "best headphones",
            "axis": "category",
            "grounding_sources": [{"uri": "u1"}],
            "competitors_named": ["Sony", "Bose"],
        },
        uri_index,
        merchant_name="Mojawa",
        merchant_category="Headphones",
    )
    assert plan["win_path"] == "publisher"
    assert "techradar.com" in (plan["win_condition"] or "")
    assert plan["limit"] is None


# --- 5. metadata threading: prompt_source flows to axis_metadata -----------

def test_query_metadata_carries_prompt_source():
    from services.agent_center_bd_report_service import (
        _query_metadata_from_records,
    )

    records = [
        {
            "query": "ip68 waterproof bone conduction headphones",
            "axis": "sidewalk",
            "attribute_basis": ["ip68 waterproof"],
            "evidence": ["tag"],
            "intent_weight": 0.9,
        },
        {
            "query": "bone conduction headphones for golfers",
            "axis": "category",
            "source": "llm_winnable",
        },
        {"query": "best headphones", "axis": "category"},
    ]
    meta = _query_metadata_from_records(records)
    assert "prompt_source" not in meta["ip68 waterproof bone conduction headphones"]
    assert (
        meta["bone conduction headphones for golfers"]["prompt_source"]
        == "llm_winnable"
    )
    assert "best headphones" not in meta  # plain head terms carry no metadata


def test_full_record_builder_stamps_prompt_source_in_metadata():
    from services.agent_center_bd_report_service import (
        _build_per_sku_audit_query_metadata,
    )

    ctx = {
        "sku_key": "test-sku",
        "product": {
            "title": "HaptiFit Terra",
            "brand": "Mojawa",
            "product_type": "Headphones",
            "attributes_raw": {"tags": ["bone conduction", "sports"]},
        },
        "sku": {"title": "Black"},
        "_winnable_prompts": ["bone conduction headphones for golfers"],
    }
    meta = _build_per_sku_audit_query_metadata(ctx, 14)
    entry = meta.get("bone conduction headphones for golfers")
    assert entry, "winnable prompt must appear in the query metadata"
    assert entry["prompt_source"] == "llm_winnable"
